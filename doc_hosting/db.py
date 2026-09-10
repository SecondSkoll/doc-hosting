"""Django bootstrap for the doc-hosting API layer.

This module wires the Django ORM (used by the FastAPI application for the
control-plane metadata and the admin site) into the ASGI process:

* :func:`setup` configures Django (idempotent, safe to call from handlers).
* :func:`ensure_admin_user` provisions the Django admin superuser from the
  ``APP_ADMIN_USERNAME``/``APP_ADMIN_PASSWORD`` environment variables.
* :func:`migrate_database` applies pending migrations, guarded by a
  PostgreSQL advisory lock so concurrent workers cannot double-apply.
* :func:`ensure_database_ready` performs all of the above once per process.

The admin superuser is provisioned automatically at startup from the
``APP_ADMIN_USERNAME``/``APP_ADMIN_PASSWORD`` environment variables (the
charm's ``admin-username``/``admin-password`` config options).  Provisioning
is creation-only: when both variables are unset no user is created, when
exactly one is set the process fails fast, and an existing user is never
modified, so a password changed in the admin interface survives restarts
and redeploys.
"""

from __future__ import annotations

import os
import threading

import django
from django.conf import settings as django_settings

SETTINGS_MODULE = "doc_hosting.django_project.settings"
_MIGRATION_ADVISORY_KEY = 0x64F1C  # arbitrary stable key for pg_advisory_lock

_setup_lock = threading.Lock()
_ready_lock = threading.Lock()
_ready = False


def setup() -> None:
    """Configure Django settings and call ``django.setup()`` (idempotent)."""
    with _setup_lock:
        if django_settings.configured:
            return
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
        django.setup()


def ensure_admin_user() -> None:
    """Provision the admin superuser from the environment (creation-only).

    Reads ``APP_ADMIN_USERNAME`` and ``APP_ADMIN_PASSWORD`` (stripped).
    When both are unset nothing is created, preserving local development
    and test behaviour; when exactly one is set the process fails fast
    with ``ImproperlyConfigured`` instead of silently skipping.  When a
    user with the configured username already exists it is left untouched,
    so a password changed in the admin interface survives restarts and
    redeploys.

    Raises:
        ImproperlyConfigured: when only one of the two variables is set.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ImproperlyConfigured

    setup()
    username = os.environ.get("APP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("APP_ADMIN_PASSWORD", "").strip()
    if not username and not password:
        return
    if not username or not password:
        missing = "APP_ADMIN_USERNAME" if not username else "APP_ADMIN_PASSWORD"
        raise ImproperlyConfigured(
            f"{missing} is not set: configure both APP_ADMIN_USERNAME and "
            "APP_ADMIN_PASSWORD to provision the admin superuser, or unset "
            "both to skip provisioning"
        )
    if get_user_model().objects.filter(username=username).exists():
        return
    get_user_model().objects.create_superuser(username, "", password)


def migrate_database() -> None:
    """Apply pending migrations and provision the admin superuser.

    On PostgreSQL both run under a session advisory lock so that multiple
    application workers started at the same time serialize their migration
    and provisioning runs instead of racing each other.  On SQLite a
    concurrently created user surfaces as an ``IntegrityError`` and is
    treated as already provisioned.
    """
    from django.core.management import call_command
    from django.db import IntegrityError, connections

    setup()
    connection = connections["default"]
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [_MIGRATION_ADVISORY_KEY])
        try:
            call_command("migrate", interactive=False, verbosity=0)
            ensure_admin_user()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_MIGRATION_ADVISORY_KEY])
            connection.close()
    else:
        call_command("migrate", interactive=False, verbosity=0)
        try:
            ensure_admin_user()
        except IntegrityError:
            # A concurrently starting process created the user first.
            pass


def ensure_database_ready() -> None:
    """Set up Django and apply pending migrations once per process."""
    global _ready
    with _ready_lock:
        if _ready:
            return
        setup()
        migrate_database()
        _ready = True
