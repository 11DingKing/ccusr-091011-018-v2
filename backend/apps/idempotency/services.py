"""
幂等凭证服务

状态机（按 机构 + 操作域 + 凭证 定位记录）：

- 记录不存在            -> 占位并执行（首次请求）
- 记录已过期            -> 重置占位并执行（凭证过期后可复用，视为全新请求）
- 请求指纹不一致        -> 冲突（同一凭证不得对应不同载荷）
- processing 且租约有效 -> 处理中（并发首次请求只执行一次，其余拒绝）
- processing 但租约过期 -> 上次执行中断（如服务重启），回收并重新执行
- succeeded / rejected  -> 重放已保存的响应
- failed                -> 上次事务失败未产生业务结果，允许重新执行
"""
import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyRecord

logger = logging.getLogger('apps')

# 占位结果动作
EXECUTE = 'execute'
REPLAY = 'replay'
CONFLICT = 'conflict'
IN_PROGRESS = 'in_progress'

STATUS_PROCESSING = 'processing'
STATUS_SUCCEEDED = 'succeeded'
STATUS_REJECTED = 'rejected'
STATUS_FAILED = 'failed'

DEFAULT_ORGANIZATION = 'default'

# 并发占位竞争的最大重试次数
MAX_ACQUIRE_ATTEMPTS = 3


class AcquireResult:
    """凭证占位结果"""

    def __init__(self, action, record=None):
        self.action = action
        self.record = record


def key_ttl():
    """凭证有效期"""
    return timedelta(hours=getattr(settings, 'IDEMPOTENCY_KEY_TTL_HOURS', 24))


def processing_lease():
    """处理中租约时长，超时视为上次执行中断"""
    return timedelta(seconds=getattr(settings, 'IDEMPOTENCY_PROCESSING_LEASE_SECONDS', 120))


def build_request_fingerprint(request):
    """
    生成请求指纹。

    JSON 请求按解析后的内容规范化（字段顺序无关）；表单/多部分请求按
    字段与文件内容摘要计算（重发时多部分边界随机，不能直接哈希原始报文）。
    """
    parts = {
        'method': request.method,
        'path': request.path,
        'query': {k: sorted(v) for k, v in sorted(request.GET.lists())},
    }
    content_type = (request.content_type or '').split(';')[0].strip().lower()
    if content_type == 'application/json':
        try:
            parts['body'] = json.loads(request.body.decode('utf-8') or 'null')
        except (ValueError, UnicodeDecodeError):
            parts['body_sha256'] = hashlib.sha256(request.body or b'').hexdigest()
    elif content_type in ('application/x-www-form-urlencoded', 'multipart/form-data'):
        parts['fields'] = {k: sorted(request.POST.getlist(k)) for k in sorted(request.POST.keys())}
        parts['files'] = {
            name: _file_digest(uploaded) for name, uploaded in sorted(request.FILES.items())
        }
    else:
        parts['body_sha256'] = hashlib.sha256(request.body or b'').hexdigest()

    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _file_digest(uploaded):
    """计算上传文件内容摘要，读完后复位文件指针供视图继续使用"""
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    return digest.hexdigest()


def acquire(*, organization, domain, key, request_hash):
    """
    尝试为请求占位。

    返回 AcquireResult：
    - EXECUTE：调用方负责执行并在结束后调用 finalize / finalize_exception
    - REPLAY / CONFLICT / IN_PROGRESS：调用方直接按结果响应，不得执行
    """
    now = timezone.now()
    ttl = key_ttl()
    lease = processing_lease()
    record = None

    for _ in range(MAX_ACQUIRE_ATTEMPTS):
        record = IdempotencyRecord.objects.filter(
            organization=organization, domain=domain, key=key
        ).first()

        if record is None:
            try:
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        organization=organization,
                        domain=domain,
                        key=key,
                        request_hash=request_hash,
                        status=STATUS_PROCESSING,
                        locked_until=now + lease,
                        expires_at=now + ttl,
                    )
                return AcquireResult(EXECUTE, record)
            except IntegrityError:
                # 并发首次请求：另一条请求已占位，重新读取其状态
                continue

        if record.expires_at <= now:
            # 凭证已过期：重置为全新的首次请求
            updated = IdempotencyRecord.objects.filter(
                pk=record.pk, expires_at__lte=now
            ).update(
                request_hash=request_hash,
                status=STATUS_PROCESSING,
                response_code=None,
                response_body=None,
                error_detail='',
                locked_until=now + lease,
                expires_at=now + ttl,
            )
            if updated:
                record.refresh_from_db()
                return AcquireResult(EXECUTE, record)
            continue

        if record.request_hash != request_hash:
            logger.warning(
                f"Idempotency conflict: {organization}/{domain}/{key} reused with different payload"
            )
            return AcquireResult(CONFLICT, record)

        if record.status == STATUS_PROCESSING:
            if record.locked_until and record.locked_until > now:
                return AcquireResult(IN_PROGRESS, record)
            # 租约过期：上次执行中断（如服务重启），回收后重新执行
            updated = IdempotencyRecord.objects.filter(
                pk=record.pk, status=STATUS_PROCESSING, locked_until__lte=now
            ).update(locked_until=now + lease, expires_at=now + ttl)
            if updated:
                record.refresh_from_db()
                return AcquireResult(EXECUTE, record)
            continue

        if record.status in (STATUS_SUCCEEDED, STATUS_REJECTED) and record.response_code is not None:
            return AcquireResult(REPLAY, record)

        # failed（或缺少响应的异常状态）：上次事务失败，允许重新执行
        updated = IdempotencyRecord.objects.filter(
            pk=record.pk, status=record.status
        ).update(
            status=STATUS_PROCESSING,
            response_code=None,
            response_body=None,
            error_detail='',
            locked_until=now + lease,
            expires_at=now + ttl,
        )
        if updated:
            record.refresh_from_db()
            return AcquireResult(EXECUTE, record)

    logger.warning(
        f"Idempotency acquire contention: {organization}/{domain}/{key}"
    )
    return AcquireResult(IN_PROGRESS, record)


def finalize(record, response):
    """
    首次执行结束后按结果落库：

    - 2xx：保存成功响应，重放原样返回
    - 4xx（除401）：保存业务拒绝响应，重放原样返回
    - 401 / 5xx：标记执行失败，不保存可重放结果，允许客户端重新执行
    """
    status_code = getattr(response, 'status_code', 500)
    body = _json_safe(getattr(response, 'data', None))

    if 200 <= status_code < 300:
        record.status = STATUS_SUCCEEDED
        record.error_detail = ''
    elif status_code != 401 and 400 <= status_code < 500:
        record.status = STATUS_REJECTED
        record.error_detail = ''
    else:
        # 事务失败或认证失效：业务结果未产生，凭证可重新执行
        record.status = STATUS_FAILED
        record.error_detail = _extract_message(body) or f'HTTP {status_code}'

    record.response_code = status_code
    record.response_body = body
    record.locked_until = None
    record.save(update_fields=[
        'status', 'response_code', 'response_body', 'error_detail',
        'locked_until', 'updated_at',
    ])


def finalize_exception(record, exc):
    """执行过程抛出未处理异常时标记失败，允许客户端重新执行"""
    record.status = STATUS_FAILED
    record.response_code = None
    record.response_body = None
    record.error_detail = f'{type(exc).__name__}: {exc}'[:500]
    record.locked_until = None
    record.save(update_fields=[
        'status', 'response_code', 'response_body', 'error_detail',
        'locked_until', 'updated_at',
    ])


def purge_expired(now=None):
    """清理已过期的凭证记录，返回清理条数"""
    now = now or timezone.now()
    deleted, _ = IdempotencyRecord.objects.filter(expires_at__lte=now).delete()
    return deleted


def _json_safe(value):
    """将响应内容转换为可存入 JSONField 的结构"""
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    except (TypeError, ValueError):
        return {'raw': str(value)}


def _extract_message(body):
    if isinstance(body, dict):
        message = body.get('message')
        if message:
            return str(message)[:500]
    if body is None:
        return ''
    return str(body)[:500]
