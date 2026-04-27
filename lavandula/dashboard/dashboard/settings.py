import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Add project root to sys.path so lavandula.common.secrets is importable
PROJECT_ROOT = BASE_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lavandula.common.secrets import get_secret  # noqa: E402

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_secret("django-secret-key")

DEBUG = False

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
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


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _get_secret_or_env("rds-database"),
        "USER": _get_secret_or_env("rds-dashboard-user"),
        "PASSWORD": _get_secret_or_env("rds-dashboard-password"),
        "HOST": _get_secret_or_env("rds-endpoint"),
        "PORT": _get_secret_or_env("rds-port"),
        "OPTIONS": {
            "options": "-c search_path=lava_dashboard,public",
        },
    },
    "pipeline": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _get_secret_or_env("rds-database"),
        "USER": _get_secret_or_env("rds-app-user"),
        "PASSWORD": _get_secret_or_env("rds-app-password"),
        "HOST": _get_secret_or_env("rds-endpoint"),
        "PORT": _get_secret_or_env("rds-port"),
        "OPTIONS": {
            "options": "-c search_path=lava_impact,public",
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
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Session security
SESSION_COOKIE_AGE = 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = True
LOGOUT_REDIRECT_URL = "/login/"
LOGIN_URL = "/login/"

# S3 bucket for report PDFs
S3_COLLATERAL_BUCKET = "lavandula-nonprofit-collaterals"
