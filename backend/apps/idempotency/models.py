"""
幂等凭证模型
"""
from django.db import models


class IdempotencyRecord(models.Model):
    """
    幂等凭证记录

    按 机构 + 操作域 + 凭证 唯一隔离：同一凭证在不同机构或不同操作域下互不影响。
    记录持久化保存，服务重启后重放行为保持不变。
    """

    STATUS_CHOICES = [
        ('processing', '处理中'),
        ('succeeded', '已成功'),
        ('rejected', '业务拒绝'),
        ('failed', '执行失败'),
    ]

    organization = models.CharField('机构标识', max_length=64)
    domain = models.CharField('操作域', max_length=64)
    key = models.CharField('幂等凭证', max_length=128)
    request_hash = models.CharField('请求指纹', max_length=64)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='processing')
    response_code = models.IntegerField('响应状态码', null=True, blank=True)
    response_body = models.JSONField('响应内容', null=True, blank=True)
    error_detail = models.TextField('失败信息', blank=True)
    locked_until = models.DateTimeField('处理租约截止', null=True, blank=True)
    expires_at = models.DateTimeField('过期时间')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'sys_idempotency_record'
        verbose_name = '幂等凭证记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'domain', 'key'],
                name='uniq_idempotency_scope',
            )
        ]
        indexes = [
            models.Index(fields=['expires_at'], name='idx_idempotency_expires'),
        ]

    def __str__(self):
        return f"{self.organization}/{self.domain}/{self.key} - {self.get_status_display()}"
