"""Django settings for the doc-hosting control plane.

The deployment provides PostgreSQL through the charm's ``postgresql``
integration, which injects ``POSTGRESQL_DB_CONNECT_STRING`` (a plain
``DATABASE_URL`` is honoured as well).  Without a PostgreSQL connection
string the project falls back to a local SQLite database for development
and testing.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[2]


def _sqlite_path() -> str:
    return os.environ.get("DOC_HOSTING_SQLITE_PATH") or str(BASE_DIR / "doc-hosting.sqlite3")


def parse_postgres_url(url: str) -> dict[str, str]:
    """Parse a PostgreSQL connection URL into a Django database config."""
    parsed = urlparse(url.strip())
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }


def _databases_from_env() -> dict[str, dict[str, dict[str, str]]]:
    connect_string = os.environ.get("POSTGRESQL_DB_CONNECT_STRING") or os.environ.get(
        "DATABASE_URL"
    )
    if connect_string:
        return {"default": parse_postgres_url(connect_string)}
    return {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _sqlite_path(),
        }
    }


def _explicit_dev_mode() -> bool:
    """Return whether the operator explicitly opted into dev/test settings."""
    return os.environ.get("DOC_HOSTING_DEV", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _secret_key() -> str:
    """Return the configured secret key, failing closed outside dev/test mode.

    paas-charm injects the generated application secret key as
    ``APP_SECRET_KEY`` (``DJANGO_SECRET_KEY`` is honoured as an
    alternative). Without a key the only permitted fallback is the explicit
    dev/test mode (``DOC_HOSTING_DEV``); a production process without a
    configured key refuses to start instead of signing sessions with a
    publicly known secret.

    Raises:
        ImproperlyConfigured: when no key is configured outside dev/test mode.
    """
    key = (
        os.environ.get("APP_SECRET_KEY")
        or os.environ.get("DJANGO_SECRET_KEY")
        or ""
    ).strip()
    if key:
        return key
    if _explicit_dev_mode():
        return "doc-hosting-insecure-development-secret-key"
    raise ImproperlyConfigured(
        "SECRET_KEY is not configured: set APP_SECRET_KEY (or DJANGO_SECRET_KEY), "
        "or opt into explicit development/test mode with DOC_HOSTING_DEV=1"
    )


def _allowed_hosts() -> list[str]:
    """Configure ``ALLOWED_HOSTS`` from the environment (fail closed).

    Outside explicit dev/test mode no fallback host is trusted: the
    deployment must configure ``APP_ALLOWED_HOSTS`` (``ALLOWED_HOSTS`` is
    honoured as an alternative), and an empty configuration trusts nothing.
    """
    raw = os.environ.get("APP_ALLOWED_HOSTS") or os.environ.get("ALLOWED_HOSTS") or ""
    hosts = [value.strip() for value in raw.split(",") if value.strip()]
    if hosts:
        return hosts
    if _explicit_dev_mode():
        # Explicit local/test fallback.
        return ["*"]
    return []


def _csrf_trusted_origins() -> list[str]:
    raw = os.environ.get("APP_CSRF_TRUSTED_ORIGINS") or os.environ.get(
        "CSRF_TRUSTED_ORIGINS", ""
    )
    return [value.strip() for value in raw.split(",") if value.strip()]


SECRET_KEY = _secret_key()
DEBUG = False
ALLOWED_HOSTS = _allowed_hosts()
CSRF_TRUSTED_ORIGINS = _csrf_trusted_origins()

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "doc_hosting.registry",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "doc_hosting.django_project.urls"

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

WSGI_APPLICATION = "doc_hosting.django_project.wsgi.application"

DATABASES = _databases_from_env()

AUTH_PASSWORD_VALIDATORS: list[dict[str, dict[str, str]]] = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/manage/static/"
STATIC_ROOT = os.environ.get("DOC_HOSTING_STATIC_ROOT") or str(BASE_DIR / "staticfiles")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
