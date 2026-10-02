from django.apps import AppConfig


class IdempotencyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.idempotency'
    verbose_name = '幂等控制'
