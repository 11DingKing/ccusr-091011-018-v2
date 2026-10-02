"""
核心公共模型
"""
from django.conf import settings
from django.db import models


class IdempotencyRecord(models.Model):
    """幂等凭证记录

    以 (机构, 操作域, 凭证) 为唯一键，缓存写接口首次请求的处理结果：
    移动端弱网重放时直接返回历史结果，不再产生第二条业务记录。
    """

    STATUS_PROCESSING = 'processing'
    STATUS_SUCCESS = 'success'
    STATUS_REJECTED = 'rejected'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [
        (STATUS_PROCESSING, '处理中'),
        (STATUS_SUCCESS, '成功'),
        (STATUS_REJECTED, '业务拒绝'),
        (STATUS_ERROR, '处理失败'),
    ]

    org = models.CharField('机构标识', max_length=64, default='', blank=True)
    scope = models.CharField('操作域', max_length=64)
    idem_key = models.CharField('幂等凭证', max_length=100)
    payload_hash = models.CharField('载荷摘要', max_length=64)
    status = models.CharField(
        '处理状态', max_length=16, choices=STATUS_CHOICES, default=STATUS_PROCESSING
    )
    response_code = models.PositiveIntegerField('响应状态码', null=True, blank=True)
    response_body = models.JSONField('响应内容', null=True, blank=True)
    error_message = models.CharField('失败原因', max_length=500, default='', blank=True)
    request_path = models.CharField('请求路径', max_length=255, default='', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='idempotency_records',
        verbose_name='发起人',
    )
    locked_at = models.DateTimeField('开始处理时间', null=True, blank=True)
    finished_at = models.DateTimeField('处理完成时间', null=True, blank=True)
    expires_at = models.DateTimeField('凭证过期时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'sys_idempotency_record'
        verbose_name = '幂等凭证记录'
        verbose_name_plural = verbose_name
        unique_together = [('org', 'scope', 'idem_key')]
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='core_idem_status_idx'),
        ]

    def __str__(self):
        return f'{self.org}/{self.scope}/{self.idem_key}@{self.status}'

    @property
    def is_terminal(self):
        """是否已有确定结论（可重放）。"""
        return self.status in (self.STATUS_SUCCESS, self.STATUS_REJECTED)
