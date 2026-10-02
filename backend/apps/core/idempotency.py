"""
幂等凭证支持

为关键写接口提供按「机构 + 操作域 + 凭证」隔离的请求去重：

* 首次请求先抢占式落库一条 processing 记录（唯一约束保证并发只有一次），
  执行业务后分别保存 success / rejected / error 结果；
* 同凭证、同载荷的重放请求直接返回首次结果，不再产生业务记录；
* 同凭证、不同载荷一律判定为冲突（409），防止凭证复用串单；
* 凭证超过保留期返回 410，需由客户端重新申请，不静默二次执行；
* 业务视图抛异常时回滚业务数据，凭证转为 error，允许同载荷安全重试；
* 服务在 processing 阶段崩溃/重启留下的悬挂锁会被识别为不确定状态，
  返回 409 并提示先核对业务结果，绝不盲目重放或再次执行；
* 不带凭证头的请求（含内部调用）原样透传，行为完全不变。
"""
import functools
import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyRecord

logger = logging.getLogger('apps')

IDEMPOTENCY_HEADER = 'Idempotency-Key'
ORG_HEADER = 'X-Org-Code'
# 未显式传入机构标识时的默认机构，用于单机构部署。
DEFAULT_ORG = 'default'
MAX_KEY_LENGTH = 100
MAX_ORG_LENGTH = 64


class IdempotencyConflict(Exception):
    """同一凭证对应不同处理状态/载荷，调用方应返回冲突响应。"""

    def __init__(self, message, code=409):
        self.message = message
        self.code = code
        super().__init__(message)


class IdempotencyKeyExpired(IdempotencyConflict):
    """凭证已过期，不能再重放也不能复用。"""

    def __init__(self, message='幂等凭证已过期，请重新获取凭证后提交'):
        super().__init__(message, code=410)


def get_config(name, default):
    return getattr(settings, name, default)


def get_org_code(request):
    """提取请求所属机构，实现按机构隔离。"""
    org = request.headers.get(ORG_HEADER, '').strip() or DEFAULT_ORG
    return org[:MAX_ORG_LENGTH]


def _canonical_payload(data):
    """对载荷做规范化序列化，保证字段顺序不同也能判为同一请求。"""
    if data is None:
        data = {}
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def hash_payload(data):
    return hashlib.sha256(_canonical_payload(data).encode('utf-8')).hexdigest()


def _response_body(response):
    """提取统一响应信封内容（data 为 None 时存 None）。"""
    if response is None:
        return None
    try:
        return response.data
    except AttributeError:
        return None


def _replay_response(record):
    """用首次请求冻结下来的响应信封重建响应，保持重放结果可解释、一致。"""
    from apps.core.response import APIResponse

    body = record.response_body or {}
    return APIResponse(
        data=body.get('data'),
        message=body.get('message', '操作成功'),
        code=record.response_code or 200,
        success=bool(body.get('success', True)),
    )


def _conflict_response(exc):
    from apps.core.response import error_response

    return error_response(message=exc.message, code=exc.code)


def _is_business_rejection(code):
    """4xx 表示业务层拒绝（校验不通过、库存不足等），结论确定、可安全重放。"""
    return code is not None and 400 <= code < 500


def acquire_record(org, scope, key, payload_hash, request):
    """抢占幂等凭证。

    返回 (record, created)：created=True 表示本请求拿到首次执行权。
    依赖 (org, scope, idem_key) 唯一约束，并发首次插入只有一个成功。
    """
    ttl_hours = get_config('IDEMPOTENCY_KEY_TTL_HOURS', 72)
    lock_timeout = get_config('IDEMPOTENCY_LOCK_TIMEOUT_SECONDS', 30)
    now = timezone.now()

    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                org=org,
                scope=scope,
                idem_key=key,
                payload_hash=payload_hash,
                status=IdempotencyRecord.STATUS_PROCESSING,
                locked_at=now,
                expires_at=now + timedelta(hours=ttl_hours),
                request_path=request.path[:255],
                created_by=getattr(request, 'user', None)
                if getattr(getattr(request, 'user', None), 'is_authenticated', False)
                else None,
            )
        return record, True
    except IntegrityError:
        # 凭证已存在：在事务内加锁读取后判断应重放、冲突还是接管重试。
        with transaction.atomic():
            record = IdempotencyRecord.objects.select_for_update().get(
                org=org, scope=scope, idem_key=key
            )
            # 过期检查优先：终态凭证过保留期后不再重放/复用，行为可解释。
            if record.expires_at and now >= record.expires_at:
                raise IdempotencyKeyExpired()
            if record.payload_hash != payload_hash:
                raise IdempotencyConflict(
                    '同一幂等凭证对应不同的请求内容，疑似凭证误用或重放攻击'
                )
            if record.is_terminal:
                return record, False
            if record.status == IdempotencyRecord.STATUS_ERROR:
                # 上次事务失败：业务结果未生效，允许同载荷重新抢占执行。
                record.status = IdempotencyRecord.STATUS_PROCESSING
                record.locked_at = now
                record.error_message = ''
                record.save(update_fields=['status', 'locked_at', 'error_message', 'updated_at'])
                return record, True
            # processing：可能是并发同伴，也可能是崩溃/重启后的悬挂锁。
            locked_at = record.locked_at or now
            if (now - locked_at).total_seconds() <= lock_timeout:
                raise IdempotencyConflict('相同请求正在处理中，请勿重复提交', code=409)
            raise IdempotencyConflict(
                '该凭证的首次请求处理中断且结果未知，请先核对业务记录后联系管理员处理',
                code=409,
            )


def finalize_record(record, response):
    """根据首次请求的实际响应落定凭证状态（须在业务事务内调用，随业务一起提交）。"""
    code = getattr(response, 'status_code', 200)
    now = timezone.now()
    record.response_code = code
    record.response_body = _response_body(response)
    record.finished_at = now
    if 200 <= code < 300:
        record.status = IdempotencyRecord.STATUS_SUCCESS
    elif _is_business_rejection(code):
        # 业务拒绝同样是确定结论：重放必须返回同样的拒绝，不能再执行业务。
        record.status = IdempotencyRecord.STATUS_REJECTED
    else:
        record.status = IdempotencyRecord.STATUS_ERROR
        record.error_message = (getattr(response, 'data', {}) or {}).get('message', '')[:500]
    record.save()
    return response


def fail_record(record, error):
    """视图抛异常：业务随外层事务回滚，凭证在独立事务里标记为 error，允许同载荷重试。"""
    record.status = IdempotencyRecord.STATUS_ERROR
    record.response_code = 500
    record.finished_at = timezone.now()
    record.error_message = str(error)[:500]
    # 独立事务提交，确保业务回滚后失败结论仍可查、可解释（服务重启后也认账）。
    with transaction.atomic():
        record.save()
    logger.exception('Idempotent request failed for %s: %s', record, error)


def idempotent(scope):
    """关键写接口幂等装饰器。

    用法：在 APIView 的写方法上叠加 @idempotent(scope='stock-in')。
    请求必须携带 Idempotency-Key 头才会启用幂等控制；缺失时原样放行，
    因此原有不带凭证的内部调用与旧客户端不受影响。
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(self, request, *args, **kwargs):
            key = request.headers.get(IDEMPOTENCY_HEADER, '').strip()
            if not key:
                # 无凭证：保持历史行为。
                return view_func(self, request, *args, **kwargs)
            if len(key) > MAX_KEY_LENGTH:
                return _conflict_response(
                    IdempotencyConflict(
                        f'{IDEMPOTENCY_HEADER} 长度不能超过 {MAX_KEY_LENGTH} 个字符', code=400
                    )
                )

            org = get_org_code(request)
            payload_hash = hash_payload(request.data)

            try:
                record, created = acquire_record(org, scope, key, payload_hash, request)
            except IdempotencyConflict as exc:
                return _conflict_response(exc)

            if not created:
                logger.info('Replaying idempotent result: %s', record)
                return _replay_response(record)

            try:
                # 凭证结论与业务写入同事务提交：成功/拒绝一定可重放，
                # 异常时业务与结论一同回滚，再由 fail_record 单独登记失败。
                with transaction.atomic():
                    response = view_func(self, request, *args, **kwargs)
                    code = getattr(response, 'status_code', 200)
                    if code >= 500:
                        raise RuntimeError(
                            (getattr(response, 'data', {}) or {}).get('message', '服务器内部错误')
                        )
                    finalize_record(record, response)
                return response
            except Exception as exc:  # noqa: BLE001 - 需兜住所有业务异常以落定凭证
                fail_record(record, exc)
                raise

        wrapper.idempotent_scope = scope
        return wrapper

    return decorator


def purge_expired_records(now=None):
    """清理已过期的终态凭证（可由周期任务调用）。"""
    now = now or timezone.now()
    deleted, _ = IdempotencyRecord.objects.filter(
        expires_at__lt=now,
        status__in=[IdempotencyRecord.STATUS_SUCCESS, IdempotencyRecord.STATUS_REJECTED],
    ).delete()
    return deleted
