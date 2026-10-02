"""
清理过期幂等凭证记录
"""
from django.core.management.base import BaseCommand

from apps.idempotency import services


class Command(BaseCommand):
    help = '清理已过期的幂等凭证记录'

    def handle(self, *args, **options):
        deleted = services.purge_expired()
        self.stdout.write(self.style.SUCCESS(f'已清理 {deleted} 条过期幂等凭证记录'))
