"""Import legacy S3 ``_registry`` metadata into the PostgreSQL control plane.

Usage: ``python manage.py import_legacy_registry``.  The command is
idempotent: re-running it upserts the same projects and publications
without duplicating anything, and it never writes registry JSON back to
the bucket.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from doc_hosting.registry import importer
from doc_hosting.settings import SettingsError, get_settings
from doc_hosting.storage import S3Storage


class Command(BaseCommand):
    """Import the legacy ``_registry`` JSON objects into PostgreSQL."""

    help = (
        "Idempotently import legacy S3 _registry metadata into the "
        "PostgreSQL control plane (no registry JSON is written)."
    )

    def handle(self, *args, **options) -> None:
        try:
            storage = S3Storage(get_settings())
        except SettingsError as exc:
            self.stderr.write(f"cannot import: S3 settings are unavailable: {exc}")
            raise SystemExit(1) from exc
        summary = importer.import_legacy_registry(storage)
        self.stdout.write(
            f"imported {summary['projects']} project(s) and upserted "
            f"{summary['publications']} new publication(s)"
        )
