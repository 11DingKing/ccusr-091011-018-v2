"""
幂等写接口混入
"""
import logging

from rest_framework.response import Response

from apps.core.response import error_response
from . import services

logger = logging.getLogger('apps')

KEY_HEADER = 'Idempotency-Key'
ORG_HEADER = 'X-Organization-Id'
REPLAYED_HEADER = 'Idempotency-Replayed'

MAX_KEY_LENGTH = 128
MAX_ORG_LENGTH = 64

IDEMPOTENT_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


class IdempotentMixin:
    """
    为写接口增加幂等凭证支持。

    - 客户端通过 `Idempotency-Key` 请求头携带凭证，可选 `X-Organization-Id` 指定机构；
    - 凭证按 机构 + 操作域 + 凭证 隔离，操作域由视图的 `idempotency_domain` 声明；
    - 未携带凭证的请求保持原有行为，内部调用不受影响。
    """

    idempotency_domain = None

    def get_idempotency_domain(self):
        return self.idempotency_domain or f'{self.__class__.__module__}.{self.__class__.__name__}'

    def dispatch(self, request, *args, **kwargs):
        if request.method not in IDEMPOTENT_METHODS:
            return super().dispatch(request, *args, **kwargs)

        key = (request.headers.get(KEY_HEADER) or '').strip()
        if not key:
            # 不带凭证的内部调用：直接执行，行为与之前一致
            return super().dispatch(request, *args, **kwargs)

        if len(key) > MAX_KEY_LENGTH:
            return self._early_response(
                request, error_response(message=f'幂等凭证长度不能超过{MAX_KEY_LENGTH}个字符'),
                *args, **kwargs
            )

        organization = (request.headers.get(ORG_HEADER) or '').strip() or services.DEFAULT_ORGANIZATION
        if len(organization) > MAX_ORG_LENGTH:
            return self._early_response(
                request, error_response(message=f'机构标识长度不能超过{MAX_ORG_LENGTH}个字符'),
                *args, **kwargs
            )

        domain = self.get_idempotency_domain()
        request_hash = services.build_request_fingerprint(request)

        result = services.acquire(
            organization=organization, domain=domain, key=key, request_hash=request_hash
        )

        if result.action == services.CONFLICT:
            return self._early_response(
                request, error_response(message='幂等凭证已被使用，且请求内容与首次请求不一致', code=409),
                *args, **kwargs
            )
        if result.action == services.IN_PROGRESS:
            return self._early_response(
                request, error_response(message='相同请求正在处理中，请勿重复提交', code=409),
                *args, **kwargs
            )
        if result.action == services.REPLAY:
            logger.info(f"Idempotency replay: {organization}/{domain}/{key}")
            return self._early_response(request, self._replay_response(result.record), *args, **kwargs)

        record = result.record
        try:
            response = super().dispatch(request, *args, **kwargs)
        except Exception as exc:
            services.finalize_exception(record, exc)
            raise
        services.finalize(record, response)
        return response

    def _early_response(self, request, response, *args, **kwargs):
        """按 APIView.dispatch 的方式完成内容协商，确保响应可正常渲染"""
        self.args = args
        self.kwargs = kwargs
        drf_request = self.initialize_request(request, *args, **kwargs)
        self.request = drf_request
        self.headers = self.default_response_headers
        return self.finalize_response(drf_request, response, *args, **kwargs)

    def _replay_response(self, record):
        response = Response(record.response_body, status=record.response_code)
        response[REPLAYED_HEADER] = 'true'
        return response
