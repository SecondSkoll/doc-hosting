"""Idempotent importer for legacy S3 ``_registry`` metadata.

Reads the JSON registry objects that the previous file-backed registry kept
under the reserved ``_registry/`` prefix and upserts them into the
PostgreSQL control plane: one ``Project`` per root path (without a secret,
so the first fully authenticated publication claims one) and one mutable
``Publication`` row per (language, version) build.  Language and version
labels are validated before anything is written; invalid records (and whole
registries with unsafe root paths) are skipped and reported in the summary.
Running the importer twice produces the same rows; it never writes anything
back to the bucket.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from django.utils import timezone as django_timezone

from .. import paths
from ..storage import S3Storage
from . import models, services


def _parse_registered_at(value: Any) -> datetime:
    """Parse a legacy ISO timestamp, defaulting to now on anything unusable."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    return django_timezone.now()


def _invalid_record(
    root: str, language: str, version: str, reason: str
) -> dict[str, str]:
    return {
        "root_path": root,
        "language": language,
        "version": version,
        "reason": reason,
    }


def import_legacy_registry(storage: S3Storage) -> dict[str, int | list[dict[str, str]]]:
    """Import legacy registry metadata into the control plane (idempotent).

    Language and version labels are validated as single safe URL segments
    (and reasonable lengths): records carrying invalid labels are skipped
    and reported in the summary instead of entering the control plane, and
    so are whole registries with unsafe root paths or malformed records.

    Returns a summary with the number of projects and publications written
    and the list of skipped invalid records.
    """
    projects = 0
    publications = 0
    skipped: list[dict[str, str]] = []
    for root_path in storage.list_registry_root_paths():
        try:
            root = paths.normalize_root_path(root_path)
        except paths.InvalidPathError:
            skipped.append(_invalid_record(root_path, "", "", "unsafe root path"))
            continue
        registry = storage.get_registry(root_path)
        if registry is None:
            continue
        domain = registry.get("domain") or ""
        project, project_created = models.Project.objects.get_or_create(
            root_path=root, defaults={"domain": domain}
        )
        changed = project_created
        if project.domain != domain:
            project.domain = domain
            project.save(update_fields=["domain", "updated_at"])
            changed = True
        projects += 1
        builds = registry.get("builds") or []
        if not isinstance(builds, list):
            builds = []
        for build in builds:
            if not isinstance(build, dict):
                skipped.append(_invalid_record(root, "", "", "invalid record"))
                continue
            language = str(build.get("language") or "").strip()
            version = str(build.get("version") or "").strip()
            commit_hash = str(build.get("commit_hash") or "").strip()
            if not (language and version):
                skipped.append(
                    _invalid_record(
                        root, language, version, "missing language or version"
                    )
                )
                continue
            if not paths.is_safe_segment(language) or len(language) > 255:
                skipped.append(
                    _invalid_record(root, language, version, "invalid language label")
                )
                continue
            if not paths.is_safe_segment(version) or len(version) > 255:
                skipped.append(
                    _invalid_record(root, language, version, "invalid version label")
                )
                continue
            if len(commit_hash) > 255:
                skipped.append(
                    _invalid_record(root, language, version, "invalid commit hash")
                )
                continue
            _, created = models.Publication.objects.update_or_create(
                project=project,
                language=language,
                version=version,
                defaults={
                    "commit_hash": commit_hash,
                    "registered_at": _parse_registered_at(build.get("registered_at")),
                },
            )
            publications += int(created)
            changed = changed or created
        if changed:
            services.record_audit(
                project,
                "registry.imported",
                {"root_path": root, "domain": domain},
            )
    return {
        "projects": projects,
        "publications": publications,
        "skipped_invalid_records": skipped,
    }
