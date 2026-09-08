"""Django bootstrap for the doc-hosting API layer.

This module wires the Django ORM (used by the FastAPI application for the
control-plane metadata and the admin site) into the ASGI process:

* :func:`setup` configures Django (idempotent, safe to call from handlers).
* :func:`migrate_database` applies pending migrations, guarded by a
  PostgreSQL advisory lock so concurrent workers cannot double-apply.
* :func:`ensure_database_ready` performs all of the above once per process.

The admin superuser is never created (or configured with a password)
automatically: deployments create it manually with
``manage.py createsuperuser`` inside the running unit.
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


def migrate_database() -> None:
    """Apply pending database migrations.

    On PostgreSQL the migration runs under a session advisory lock so that
    multiple application workers started at the same time serialize their
    migration runs instead of racing each other.
    """
    from django.core.management import call_command
    from django.db import connections

    setup()
    connection = connections["default"]
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [_MIGRATION_ADVISORY_KEY])
        try:
            call_command("migrate", interactive=False, verbosity=0)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [_MIGRATION_ADVISORY_KEY])
            connection.close()
    else:
        call_command("migrate", interactive=False, verbosity=0)


def ensure_database_ready() -> None:
    """Set up Django and apply pending migrations once per process."""
    global _ready
    with _ready_lock:
        if _ready:
            return
        setup()
        migrate_database()
        _ready = True
