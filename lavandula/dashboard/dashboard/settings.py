import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Add project root to sys.path so lavandula.common.secrets is importable
PROJECT_ROOT = BASE_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lavandula.common.secrets import get_secret  # noqa: E402
from lavandula.common.db import IAMTokenManager  # noqa: E402

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_secret("django-secret-key")

DEBUG = False

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "cloud2.lavandulagroup.com"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "pipeline",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "dashboard.middleware.HtmxLoginRedirectMiddleware",
]

ROOT_URLCONF = "dashboard.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "dashboard.wsgi.application"


def _get_secret_or_env(name):
    """Try env override first, then SSM."""
    env_key = f"LAVANDULA_SECRET_{name.upper().replace('-', '_')}"
    return os.environ.get(env_key) or get_secret(name)


_RDS_HOST = _get_secret_or_env("rds-endpoint")
_RDS_PORT = int(_get_secret_or_env("rds-port"))
_RDS_DATABASE = _get_secret_or_env("rds-database")
_APP_USER = _get_secret_or_env("rds-app-user")

_iam = IAMTokenManager(region="us-east-1", host=_RDS_HOST, port=_RDS_PORT, user=_APP_USER)

DATABASES = {
    "default": {
        "ENGINE": "dashboard.pg_iam_backend",
        "NAME": _RDS_DATABASE,
        "USER": _APP_USER,
        "HOST": _RDS_HOST,
        "PORT": _RDS_PORT,
        "IAM_TOKEN_MANAGER": _iam,
        "OPTIONS": {
            "options": "-c search_path=lava_dashboard,public",
            "sslmode": "require",
        },
    },
    "pipeline": {
        "ENGINE": "dashboard.pg_iam_backend",
        "NAME": _RDS_DATABASE,
        "USER": _APP_USER,
        "HOST": _RDS_HOST,
        "PORT": _RDS_PORT,
        "IAM_TOKEN_MANAGER": _iam,
        "OPTIONS": {
            "options": "-c search_path=lava_corpus,public",
            "sslmode": "require",
        },
    },
}

DATABASE_ROUTERS = ["pipeline.routers.PipelineRouter"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 16}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("LAVANDULA_TIMEZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/dashboard/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
LOGIN_URL = "/dashboard/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/dashboard/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Session security
SESSION_COOKIE_AGE = 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = ["https://cloud2.lavandulagroup.com"]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# S3 bucket for report PDFs
S3_COLLATERAL_BUCKET = "lavandula-nonprofit-collaterals"
