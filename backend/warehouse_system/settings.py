"""监管物资保管服务的运行配置。"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-custody-service-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "django_filters",
    "apps.core",
    "apps.authentication",
    "apps.warehouse",
    "apps.personnel",
    "apps.reports",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.logging_middleware.RequestLoggingMiddleware",
    "apps.core.logging_middleware.SecurityLoggingMiddleware",
    "apps.authentication.middleware.JWTAuthenticationMiddleware",
    "apps.authentication.middleware.OperationLogMiddleware",
]
ROOT_URLCONF = "warehouse_system.urls"
TEMPLATES = []
WSGI_APPLICATION = "warehouse_system.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 20},
        # 测试库使用独立文件：内存共享缓存模式下多线程写会立即报
        # “database table is locked”，无法真实模拟并发写竞争。
        "TEST": {"NAME": os.environ.get("SQLITE_TEST_PATH", str(BASE_DIR / "test_db.sqlite3"))},
    }
}
AUTH_PASSWORD_VALIDATORS = []
AUTH_USER_MODEL = "authentication.User"
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.authentication.backends.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "EXCEPTION_HANDLER": "apps.core.exceptions.custom_exception_handler",
}
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "local-custody-jwt-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DELTA = timedelta(hours=24)
# 幂等凭证保留期：期内同凭证请求按重放/冲突处理；过期返回 410。
IDEMPOTENCY_KEY_TTL_HOURS = int(os.environ.get("IDEMPOTENCY_KEY_TTL_HOURS", "72"))
# 处理中凭证的锁超时：超过仍停留在 processing 视为崩溃残留，拒绝自动重放。
IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = int(
    os.environ.get("IDEMPOTENCY_LOCK_TIMEOUT_SECONDS", "30")
)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
from apps.core.logging_config import setup_logging
setup_logging(BASE_DIR, DEBUG)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
