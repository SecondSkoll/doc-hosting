"""Control-plane services for doc-hosting.

All metadata mutations go through this module (the API handlers and the
Django admin are thin adapters), so the rules live in exactly one place:

* Ownership: every publish carries two independent credentials, both
  already checked by the API layer: the deployment-wide bearer token
  (``Authorization: Bearer <APP_PUBLISH_TOKEN>``) and the project's shared
  secret in the JSON body. Only the project secret is hashed and stored.
  The first fully authenticated publication claims an unclaimed normalized
  root; a matching secret may publish under a nested root; a foreign
  ancestor secret mismatches with 403; claiming a prefix that shadows an
  existing descendant conflicts with 409. The claim boundary checks are
  serialized with a PostgreSQL advisory transaction lock against
  concurrent bypass (SQLite tests rely on its single-writer model).
* Publications are upserted per (project, language, version) and audited on
  every push.
* Direct uploads share the same authorization and registration rules: the
  API ``begin`` step claims/authenticates the root and issues short-lived
  exact-key presigned PUT URLs (one per declared manifest file, each bound
  to the file's SHA-256); the ``finalize`` step re-authenticates, locks the
  session, verifies every object's existence, size and stored SHA-256 in
  storage and only then transactionally registers the publication and
  completes the session. Failures leave the session pending (retryable
  until it expires) and nothing registered; replaying a completed session
  with an identical manifest is idempotent.
* Redirects are same-site absolute paths only, matched exact-first and then
  by longest prefix with suffix preservation, loop/hop protections, reserved
  namespace and registered-root-shadow protections, a resolution cache with
  explicit invalidation and a finite TTL (bounded multi-process
  convergence), and self-exclusion for downward redirects.
* Layout toggles re-key S3 content, switch flags/labels, rewrite the
  project's redirects and create validated old-layout-to-new-layout prefix
  redirects transactionally, then delete the old keys; every step is
  idempotent, retryable and audited, and ORM collisions surface as
  actionable 409 errors.
* Root migrations validate the generated old-to-new prefix redirect with
  the standard loop/hop rules before the transactional switch, copy, then
  switch the project root, rewrite project redirects, persist the copied
  key mapping and audit, then delete exactly the copied old source keys;
  every step is idempotent and retryable.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import time
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from .. import paths
from ..storage import S3Storage
from . import models

MAX_REDIRECT_HOPS = 10
DEFAULT_LANGUAGE_LABEL = "en"
DEFAULT_VERSION_LABEL = "latest"
DEFAULT_REDIRECT_CACHE_TTL_SECONDS = 30.0
# Arbitrary stable keys for PostgreSQL advisory locks (distinct from the
# migration advisory lock key used by doc_hosting.db).
CLAIM_ADVISORY_LOCK_KEY = 0x64F1C1
MANIFEST_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ServiceError(Exception):
    """A control-plane rule violation mapped onto an HTTP error response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RedirectLoopError(ServiceError):
    """Raised when redirect resolution exceeds the hop budget."""

    def __init__(self, detail: str = "redirect loop detected") -> None:
        super().__init__(508, detail)


def record_audit(
    project: models.Project | None, event_type: str, payload: dict[str, Any]
) -> None:
    """Append an immutable audit event (payloads never contain secrets)."""
    models.AuditEvent.objects.create(
        project=project,
        project_root=project.root_path if project else "",
        event_type=event_type,
        payload=payload,
    )


# --------------------------------------------------------------------------
# Publication / ownership
# --------------------------------------------------------------------------


def _acquire_claim_lock() -> None:
    """Serialize ownership boundary checks against concurrent claims.

    The claim flow is a check-then-act sequence over the project table; on
    PostgreSQL an advisory transaction lock (released with the surrounding
    transaction) makes concurrent publishes wait for the boundary checks of
    the in-flight claim instead of interleaving their own. SQLite tests
    rely on its single-writer serialization instead.
    """
    from django.db import connection

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [CLAIM_ADVISORY_LOCK_KEY])


def _resolve_project_for_publication(
    root: str, project_secret: str
) -> tuple[models.Project, bool, bool]:
    """Claim or authenticate the project for ``root`` (caller's transaction).

    Returns ``(project, claimed, secret_adopted)``. The first fully
    authenticated publication claims an unclaimed root (or adopts the
    secret of a project imported from the legacy registry).

    Raises:
        ServiceError: 403 for secret mismatches on the project or a
            foreign ancestor, 409 when the claim would shadow an existing
            descendant or was raced by a concurrent claim.
    """
    claimed = False
    existing = models.Project.objects.filter(root_path=root).first()
    if existing is not None:
        project = models.Project.objects.select_for_update().get(pk=existing.pk)
    else:
        _claim_root(root=root, project_secret=project_secret)
        project = models.Project.objects.select_for_update().get(root_path=root)
        claimed = True
    secret_adopted = False
    if project.secret_claimed:
        if not project.check_secret(project_secret):
            raise ServiceError(403, "invalid project secret for this root path")
    else:
        # A project imported from the legacy registry has no secret yet;
        # the first fully authenticated publication (the deployment
        # bearer gate was already passed by the API layer) adopts its
        # project secret.
        project.set_secret(project_secret)
        project.save(update_fields=["secret_hash", "updated_at"])
        claimed = True
        secret_adopted = True
    return project, claimed, secret_adopted


def _assert_publication_dimensions(
    project: models.Project, language: str, version: str
) -> None:
    """Reject a publication that violates a disabled dimension's sole label."""
    if not project.language_enabled and language != project.language_label:
        raise ServiceError(
            409,
            "language dimension is disabled for this project; "
            f"only {project.language_label!r} can be published",
        )
    if not project.version_enabled and version != project.version_label:
        raise ServiceError(
            409,
            "version dimension is disabled for this project; "
            f"only {project.version_label!r} can be published",
        )


def _apply_publication_domain(project: models.Project, domain: str | None) -> None:
    """Update the project's serving domain when the publication carries one."""
    if domain is not None and domain != project.domain:
        project.domain = domain
        project.save(update_fields=["domain", "updated_at"])


def _register_publication(
    project: models.Project, language: str, version: str, commit_hash: str
) -> models.Publication:
    """Upsert the publication row for (project, language, version)."""
    publication, _ = models.Publication.objects.update_or_create(
        project=project,
        language=language,
        version=version,
        defaults={
            "commit_hash": commit_hash,
            "registered_at": timezone.now(),
        },
    )
    return publication


def publish_build(
    *,
    root_path: str,
    language: str,
    version: str,
    commit_hash: str,
    domain: str | None,
    project_secret: str,
) -> dict[str, Any]:
    """Upsert a publication, claiming the root on the first publication.

    The caller (the ingestion API) has already passed both gates: the
    deployment-wide bearer token and the presence of a non-empty project
    secret in the request body. Only the project secret is hashed/stored.

    Raises:
        ServiceError: 422 for an empty secret or unsafe paths, 403 for
            secret mismatches on the project or a foreign ancestor, 409 when
            the claim would shadow an existing descendant, was raced by a
            concurrent claim, or violates a disabled dimension's sole label.
    """
    if not project_secret or not project_secret.strip():
        raise ServiceError(422, "project_secret must not be empty")
    try:
        root = paths.normalize_root_path(root_path)
    except paths.InvalidPathError as exc:
        raise ServiceError(422, str(exc)) from exc
    with transaction.atomic():
        project, claimed, secret_adopted = _resolve_project_for_publication(
            root, project_secret
        )
        _assert_publication_dimensions(project, language, version)
        _apply_publication_domain(project, domain)
        publication = _register_publication(project, language, version, commit_hash)
        if claimed:
            record_audit(
                project,
                "project.claimed",
                {
                    "root_path": root,
                    "domain": project.domain,
                    "secret_adopted": secret_adopted,
                },
            )
        record_audit(
            project,
            "publication.upserted",
            {
                "language": language,
                "version": version,
                "commit_hash": commit_hash,
                "domain": project.domain,
            },
        )
    return {
        "root_path": root,
        "domain": project.domain,
        "language": language,
        "version": version,
        "commit_hash": commit_hash,
        "registered_at": publication.registered_at.isoformat(),
        "claimed": claimed,
    }


def _claim_root(*, root: str, project_secret: str) -> None:
    """Create the project row for an unclaimed root, enforcing ownership.

    Raises:
        ServiceError: 409 when a registered descendant would be shadowed (or
            the root was claimed concurrently), 403 when a registered
            foreign ancestor does not accept the project secret.
    """
    _acquire_claim_lock()
    projects = list(models.Project.objects.all())
    for candidate in projects:
        if paths.is_ancestor_root(root, candidate.root_path):
            raise ServiceError(
                409,
                f"root path {root!r} shadows the existing root "
                f"{candidate.root_path!r}; migrate it instead",
            )
    ancestors = [
        candidate
        for candidate in projects
        if paths.is_ancestor_root(candidate.root_path, root)
    ]
    if ancestors and not any(
        candidate.secret_claimed and candidate.check_secret(project_secret)
        for candidate in ancestors
    ):
        raise ServiceError(
            403,
            f"root path {root!r} is nested under an existing project; "
            "its shared project secret is required",
        )
    project = models.Project(root_path=root)
    project.set_secret(project_secret)
    try:
        with transaction.atomic():
            project.save()
    except IntegrityError as exc:
        raise ServiceError(
            409, f"root path {root!r} was claimed concurrently; retry the publication"
        ) from exc


# --------------------------------------------------------------------------
# Direct uploads (begin -> presigned PUTs -> verified finalize)
# --------------------------------------------------------------------------


def _checksum_b64(hex_digest: str) -> str:
    """Return the base64-encoded digest for a lowercase hex SHA-256."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


def _validate_manifest_path(path: str) -> None:
    """Reject anything that is not a safe relative path below the prefix."""
    if path.startswith("/") or "\\" in path:
        raise ServiceError(422, f"manifest path must be a relative path: {path!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise ServiceError(422, f"manifest path contains control characters: {path!r}")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ServiceError(422, f"unsafe manifest path: {path!r}")


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    """Validate a build manifest and return it normalized (sorted by path).

    Every entry must declare a safe unique relative ``path``, a lowercase
    hex SHA-256 ``sha256`` and a non-negative integer ``size``.

    Raises:
        ServiceError: 422 for any invalid or empty manifest.
    """
    if not isinstance(manifest, list) or not manifest:
        raise ServiceError(422, "manifest must be a non-empty list of file entries")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict):
            raise ServiceError(422, "manifest entries must be objects")
        path = item.get("path")
        sha256 = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not path:
            raise ServiceError(422, "manifest entry path must be a non-empty string")
        _validate_manifest_path(path)
        if not isinstance(sha256, str) or not MANIFEST_SHA256_RE.fullmatch(sha256):
            raise ServiceError(
                422,
                f"manifest entry sha256 must be a lowercase hex SHA-256 digest: {path!r}",
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ServiceError(
                422, f"manifest entry size must be a non-negative integer: {path!r}"
            )
        if path in seen:
            raise ServiceError(422, f"duplicate manifest path: {path!r}")
        seen.add(path)
        entries.append({"path": path, "sha256": sha256, "size": size})
    return sorted(entries, key=lambda entry: entry["path"])


def _publication_key_prefix(
    project: models.Project, language: str, version: str
) -> str:
    """Return the logical S3 key prefix for the project's active layout."""
    segments = [project.root_path]
    if project.language_enabled:
        segments.append(language)
    if project.version_enabled:
        segments.append(version)
    return "/".join(segments)


def begin_upload(
    *,
    root_path: str,
    language: str,
    version: str,
    commit_hash: str,
    domain: str | None,
    project_secret: str,
    manifest: Any,
    storage: S3Storage,
    url_ttl: int,
) -> dict[str, Any]:
    """Authorize a direct upload and create its (pending) upload session.

    Shares the publish authorization rules (root claim, project secret,
    disabled dimensions), stores the declared manifest on the session and
    returns one short-lived exact-key presigned PUT URL per manifest file,
    each bound to the file's SHA-256 so the storage rejects a mismatching
    body at PUT time.

    Raises:
        ServiceError: 422 for an empty secret, unsafe paths or an invalid
            manifest; 403/409 with the ownership rules of publish_build.
    """
    if not project_secret or not project_secret.strip():
        raise ServiceError(422, "project_secret must not be empty")
    try:
        root = paths.normalize_root_path(root_path)
    except paths.InvalidPathError as exc:
        raise ServiceError(422, str(exc)) from exc
    entries = validate_manifest(manifest)
    expires_at = timezone.now() + timedelta(seconds=url_ttl)
    with transaction.atomic():
        project, claimed, secret_adopted = _resolve_project_for_publication(
            root, project_secret
        )
        _assert_publication_dimensions(project, language, version)
        session = models.UploadSession.objects.create(
            project=project,
            language=language,
            version=version,
            commit_hash=commit_hash,
            domain=domain if domain is not None else "",
            key_prefix=_publication_key_prefix(project, language, version),
            manifest=entries,
            status=models.UploadSession.STATUS_PENDING,
            expires_at=expires_at,
        )
        if claimed:
            record_audit(
                project,
                "project.claimed",
                {
                    "root_path": root,
                    "domain": project.domain,
                    "secret_adopted": secret_adopted,
                },
            )
        record_audit(
            project,
            "upload.begun",
            {
                "upload_id": session.pk,
                "language": language,
                "version": version,
                "commit_hash": commit_hash,
                "key_prefix": session.key_prefix,
                "files": len(entries),
                "expires_at": expires_at.isoformat(),
            },
        )
    uploads = [
        {
            "path": entry["path"],
            **storage.presign_put(
                f"{session.key_prefix}/{entry['path']}",
                expires_in=url_ttl,
                checksum_sha256_b64=_checksum_b64(entry["sha256"]),
            ),
        }
        for entry in entries
    ]
    return {
        "upload_id": session.pk,
        "root_path": root,
        "key_prefix": session.key_prefix,
        "expires_at": expires_at.isoformat(),
        "url_ttl": url_ttl,
        "uploads": uploads,
    }


def _verify_upload_object(
    storage: S3Storage, key_prefix: str, entry: dict[str, Any]
) -> None:
    """Verify one manifest object's existence, size and stored SHA-256.

    The stored checksum comes from HEAD (``ChecksumMode=ENABLED``); storage
    implementations that do not report it fall back to hashing the object
    bytes, so verification never trusts an unverified body.

    Raises:
        ServiceError: 409 when the object is missing or its size or
            SHA-256 does not match the manifest.
    """
    key = f"{key_prefix}/{entry['path']}"
    info = storage.head_object_info(key)
    if info is None:
        raise ServiceError(
            409,
            f"the uploaded object for manifest path {entry['path']!r} is "
            "missing in storage",
        )
    if info["size"] != entry["size"]:
        raise ServiceError(
            409,
            f"size mismatch for manifest path {entry['path']!r}: storage "
            f"holds {info['size']} byte(s), the manifest declares {entry['size']}",
        )
    checksum = info.get("checksum_sha256")
    if not checksum:
        data = storage.get_bytes(key)
        if data is None:
            raise ServiceError(
                409,
                f"the uploaded object for manifest path {entry['path']!r} is "
                "missing in storage",
            )
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
    if checksum != _checksum_b64(entry["sha256"]):
        raise ServiceError(
            409,
            f"sha256 mismatch for manifest path {entry['path']!r}: storage "
            "holds different content than the manifest declares",
        )


def _upload_result(
    session: models.UploadSession,
    project: models.Project,
    publication: models.Publication | None,
    *,
    replay: bool,
) -> dict[str, Any]:
    registered_at = (
        publication.registered_at if publication is not None else session.completed_at
    )
    return {
        "upload_id": session.pk,
        "root_path": project.root_path,
        "domain": project.domain,
        "language": session.language,
        "version": session.version,
        "commit_hash": session.commit_hash,
        "registered_at": registered_at.isoformat() if registered_at else None,
        "replay": replay,
    }


def finalize_upload(
    upload_id: Any,
    *,
    project_secret: str,
    manifest: Any,
    storage: S3Storage,
) -> dict[str, Any]:
    """Verify a direct upload and atomically register its publication.

    Re-authenticates the project secret, locks the session row (replays of
    a completed session are idempotent for an identical manifest and
    conflict for a differing one), rejects expired sessions and manifest
    drift, re-asserts the current dimension flags and verifies every
    object's existence, size and stored SHA-256 before the single
    transaction registers the publication and completes/audits the session.
    Any failure rolls back and leaves the session pending (retryable until
    it expires) with nothing registered.

    Raises:
        ServiceError: 404 for an unknown upload ID, 403 for a wrong
            project secret, 422 for an invalid manifest and 409 for
            replays with a differing manifest, expiry, manifest drift,
            disabled dimensions or object verification failures.
    """
    if not project_secret or not project_secret.strip():
        raise ServiceError(422, "project_secret must not be empty")
    entries = validate_manifest(manifest)
    with transaction.atomic():
        try:
            session = models.UploadSession.objects.select_for_update().get(
                pk=upload_id
            )
        except (models.UploadSession.DoesNotExist, ValueError, TypeError):
            raise ServiceError(404, f"unknown upload session: {upload_id!r}") from None
        project = models.Project.objects.select_for_update().get(pk=session.project_id)
        if project.secret_claimed:
            if not project.check_secret(project_secret):
                raise ServiceError(403, "invalid project secret for this root path")
        else:
            # Unreachable through the API (begin claims or adopts the
            # secret); kept for parity with publish_build so finalize can
            # never authenticate against an unclaimed project.
            project.set_secret(project_secret)
            project.save(update_fields=["secret_hash", "updated_at"])
        if session.status == models.UploadSession.STATUS_COMPLETED:
            if entries != (session.manifest or []):
                raise ServiceError(
                    409,
                    "this upload session is already completed with a "
                    "different manifest",
                )
            publication = project.publications.filter(
                language=session.language, version=session.version
            ).first()
            return _upload_result(session, project, publication, replay=True)
        if timezone.now() > session.expires_at:
            raise ServiceError(
                409,
                "this upload session has expired; begin a new upload session",
            )
        if entries != (session.manifest or []):
            raise ServiceError(
                409, "the manifest does not match the session's declared manifest"
            )
        _assert_publication_dimensions(project, session.language, session.version)
        for entry in entries:
            _verify_upload_object(storage, session.key_prefix, entry)
        _apply_publication_domain(
            project, session.domain if session.domain else None
        )
        publication = _register_publication(
            project, session.language, session.version, session.commit_hash
        )
        session.status = models.UploadSession.STATUS_COMPLETED
        session.completed_at = timezone.now()
        session.save(update_fields=["status", "completed_at", "updated_at"])
        record_audit(
            project,
            "publication.upserted",
            {
                "language": session.language,
                "version": session.version,
                "commit_hash": session.commit_hash,
                "domain": project.domain,
            },
        )
        record_audit(
            project,
            "upload.completed",
            {
                "upload_id": session.pk,
                "language": session.language,
                "version": session.version,
                "commit_hash": session.commit_hash,
                "files": len(entries),
            },
        )
    return _upload_result(session, project, publication, replay=False)


def grouped_versions(
    project: models.Project, language: str | None = None
) -> list[dict[str, Any]]:
    """Group the project's publications by version (optionally language-filtered)."""
    publications = project.publications.all()
    if language is not None:
        publications = publications.filter(language=language)
    grouped: dict[str, dict[str, Any]] = {}
    for publication in publications.order_by("registered_at", "id"):
        entry = grouped.setdefault(
            publication.version,
            {"version": publication.version, "languages": [], "commit_hash": ""},
        )
        if publication.language not in entry["languages"]:
            entry["languages"].append(publication.language)
        entry["commit_hash"] = publication.commit_hash
    for entry in grouped.values():
        entry["languages"] = sorted(entry["languages"])
    return list(grouped.values())


def project_layout(project: models.Project) -> dict[str, Any]:
    """Return the project's URL layout description (used by the version API)."""
    return {
        "language_enabled": project.language_enabled,
        "version_enabled": project.version_enabled,
        "language_label": project.language_label,
        "version_label": project.version_label,
    }


def _distinct_labels(
    publications: QuerySet[models.Publication], field: str
) -> list[str]:
    """Return the sorted distinct labels used by the publications."""
    return sorted(set(publications.values_list(field, flat=True)))


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------

_redirect_cache: dict[str, Any] = {
    "loaded": False,
    "loaded_at": 0.0,
    "exact": {},
    "prefixes": [],
}


def _redirect_cache_ttl() -> float:
    """Return the finite redirect cache TTL in seconds.

    Explicit mutations invalidate the cache immediately; the finite TTL
    bounds how long an in-process cache may serve stale redirect state
    written by another process (multi-process convergence). Invalid or
    non-finite values fall back to the default.
    """
    raw = os.environ.get("DOC_HOSTING_REDIRECT_CACHE_TTL")
    if raw is None:
        return DEFAULT_REDIRECT_CACHE_TTL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REDIRECT_CACHE_TTL_SECONDS
    if not math.isfinite(value) or value < 0:
        return DEFAULT_REDIRECT_CACHE_TTL_SECONDS
    return value


def invalidate_redirect_cache() -> None:
    """Drop the in-process redirect resolution cache (call after mutations)."""
    _redirect_cache["loaded"] = False
    _redirect_cache["loaded_at"] = 0.0


def _load_redirect_cache() -> None:
    ttl = _redirect_cache_ttl()
    if _redirect_cache["loaded"] and (
        time.monotonic() - _redirect_cache["loaded_at"]
    ) < ttl:
        return
    exact: dict[str, str] = {}
    prefixes: list[tuple[str, str]] = []
    for redirect in models.Redirect.objects.filter(enabled=True):
        if redirect.match_type == models.Redirect.MATCH_EXACT:
            exact[redirect.from_path] = redirect.to_path
        else:
            prefixes.append((redirect.from_path, redirect.to_path))
    _redirect_cache["exact"] = exact
    _redirect_cache["prefixes"] = sorted(prefixes, key=lambda item: -len(item[0]))
    _redirect_cache["loaded_at"] = time.monotonic()
    _redirect_cache["loaded"] = True


def _find_redirect_match(view: str) -> tuple[str, str, bool] | None:
    """Return ``(from_path, to_path, is_exact)`` for the winning match.

    Exact matches win; among prefix redirects the longest source path wins.
    """
    _load_redirect_cache()
    target = _redirect_cache["exact"].get(view)
    if target is not None:
        return view, target, True
    for from_path, to_path in _redirect_cache["prefixes"]:
        if view == from_path or view.startswith(from_path + "/"):
            return from_path, to_path, False
    return None


def _find_redirect_match_db(view: str) -> tuple[str, str, bool] | None:
    """Return the winning match for ``view`` from fresh database rows.

    Unlike :func:`_find_redirect_match` this never consults the TTL
    resolution cache, so validation always reasons about the redirect
    table as it exists right now (including uncommitted rows of the
    surrounding transaction).
    """
    exact = models.Redirect.objects.filter(
        from_path=view, match_type=models.Redirect.MATCH_EXACT, enabled=True
    ).first()
    if exact is not None:
        return view, exact.to_path, True
    best: models.Redirect | None = None
    for candidate in models.Redirect.objects.filter(
        match_type=models.Redirect.MATCH_PREFIX, enabled=True
    ):
        if view == candidate.from_path or view.startswith(candidate.from_path + "/"):
            if best is None or len(candidate.from_path) > len(best.from_path):
                best = candidate
    if best is None:
        return None
    return best.from_path, best.to_path, False


def _single_hop_db(view: str) -> str | None:
    """Resolve one hop of a normalized path using fresh database rows."""
    match = _find_redirect_match_db(view)
    if match is None:
        return None
    from_path, to_path, is_exact = match
    if is_exact:
        return to_path
    if _is_downward(from_path, to_path) and (
        view == to_path or view.startswith(to_path + "/")
    ):
        return None
    return to_path + view[len(from_path):]


def _is_downward(from_path: str, to_path: str) -> bool:
    """Return whether ``to_path`` lies inside the ``from_path`` subtree."""
    return to_path == from_path or to_path.startswith(from_path + "/")


def _single_hop(view: str) -> str | None:
    """Resolve one hop of an already-normalized path."""
    match = _find_redirect_match(view)
    if match is None:
        return None
    from_path, to_path, is_exact = match
    if is_exact:
        return to_path
    # Self-exclusion for downward redirects: a prefix redirect whose
    # destination lies inside its own source subtree must not fire for
    # requests that are already at or below the destination.
    if _is_downward(from_path, to_path) and (
        view == to_path or view.startswith(to_path + "/")
    ):
        return None
    return to_path + view[len(from_path):]


def resolve_redirect(raw_path: str) -> str | None:
    """Follow the redirect chain for a request path, or return ``None``.

    The first hop preserves the original request case for the suffix;
    subsequent hops operate on already-normalized targets.

    Raises:
        RedirectLoopError: when the chain revisits an already-seen path (a
            deterministic loop) or exceeds ``MAX_REDIRECT_HOPS``.
    """
    view = paths.normalize_request_path(raw_path)
    match = _find_redirect_match(view)
    if match is None:
        return None
    from_path, to_path, is_exact = match
    if is_exact:
        current = to_path
    else:
        if _is_downward(from_path, to_path) and (
            view == to_path or view.startswith(to_path + "/")
        ):
            return None
        suffix = paths.split_request_path(raw_path, from_path.strip("/").split("/"))
        if suffix is None:
            suffix = view[len(from_path):]
        current = to_path + suffix
    seen = {view}
    for _ in range(MAX_REDIRECT_HOPS - 1):
        if current in seen:
            # A revisit can only be a cycle: report the loop instead of
            # spinning through the remaining hop budget.
            raise RedirectLoopError()
        seen.add(current)
        target = _single_hop(current)
        if target is None:
            return current
        current = target
    # The budget is exhausted: only resolve when the last target is final.
    if _single_hop(current) is None:
        return current
    raise RedirectLoopError()


def _validate_redirect_paths(
    from_path: str, to_path: str, match_type: str
) -> tuple[str, str]:
    """Normalize and validate a redirect's paths and match type."""
    if match_type not in (models.Redirect.MATCH_EXACT, models.Redirect.MATCH_PREFIX):
        raise ServiceError(422, f"invalid match type: {match_type!r}")
    try:
        from_norm = paths.normalize_site_path(from_path)
        to_norm = paths.normalize_site_path(to_path)
    except paths.InvalidPathError as exc:
        raise ServiceError(422, str(exc)) from exc
    first_segment = from_norm.strip("/").split("/")[0] if from_norm != "/" else ""
    if first_segment in paths.RESERVED_SEGMENTS:
        raise ServiceError(422, f"redirect source {from_path!r} lives in a reserved namespace")
    if match_type == models.Redirect.MATCH_PREFIX and from_norm == "/":
        raise ServiceError(422, "a prefix redirect from the site root is not allowed")
    if from_norm == to_norm:
        raise ServiceError(422, "redirect source and destination must differ")
    _ensure_no_root_shadow(from_norm, match_type)
    return from_norm, to_norm


def _ensure_no_root_shadow(
    from_norm: str,
    match_type: str,
    exclude_roots: Iterable[str] = (),
) -> None:
    """Reject redirects that would shadow a registered project root."""
    source = from_norm.strip("/")
    excluded = set(exclude_roots)
    for root in models.Project.objects.values_list("root_path", flat=True):
        if root in excluded:
            continue
        shadows = (
            source == root
            if match_type == models.Redirect.MATCH_EXACT
            else paths.is_segment_prefix(source, root)
        )
        if shadows:
            raise ServiceError(
                409, f"redirect source {from_norm!r} shadows the registered root {root!r}"
            )


def _ensure_no_redirect_loop(from_norm: str, to_norm: str) -> None:
    """Walk the existing chain from the target and reject loops or hop overload.

    Validation always follows fresh database rows (never the TTL
    resolution cache), so a redirect written by another process cannot be
    missed by a recently cached snapshot.
    """
    seen = {from_norm}
    current = to_norm
    for _ in range(MAX_REDIRECT_HOPS):
        if current in seen:
            raise ServiceError(409, "this redirect would create a redirect loop")
        seen.add(current)
        target = _single_hop_db(current)
        if target is None:
            return
        current = target
    raise ServiceError(409, "this redirect would exceed the redirect hop budget")


def create_redirect(
    from_path: str,
    to_path: str,
    *,
    match_type: str = models.Redirect.MATCH_EXACT,
    project: models.Project | None = None,
    enabled: bool = True,
) -> models.Redirect:
    """Create a redirect through the validation and audit rules."""
    from_norm, to_norm = _validate_redirect_paths(from_path, to_path, match_type)
    _ensure_no_redirect_loop(from_norm, to_norm)
    try:
        with transaction.atomic():
            redirect = models.Redirect.objects.create(
                project=project,
                match_type=match_type,
                from_path=from_norm,
                to_path=to_norm,
                enabled=enabled,
            )
            record_audit(
                redirect.project,
                "redirect.created",
                {
                    "from_path": from_norm,
                    "to_path": to_norm,
                    "match_type": match_type,
                    "enabled": enabled,
                },
            )
    except IntegrityError as exc:
        raise ServiceError(
            409, f"a {match_type} redirect from {from_norm!r} already exists"
        ) from exc
    invalidate_redirect_cache()
    return redirect


def update_redirect(redirect: models.Redirect, **fields: Any) -> models.Redirect:
    """Update a redirect through the validation and audit rules."""
    from_path = fields.pop("from_path", redirect.from_path)
    to_path = fields.pop("to_path", redirect.to_path)
    match_type = fields.pop("match_type", redirect.match_type)
    enabled = fields.pop("enabled", redirect.enabled)
    from_norm, to_norm = _validate_redirect_paths(from_path, to_path, match_type)
    redirect.from_path = from_norm
    redirect.to_path = to_norm
    redirect.match_type = match_type
    redirect.enabled = enabled
    for key, value in fields.items():
        setattr(redirect, key, value)
    _ensure_no_redirect_loop(from_norm, to_norm)
    with transaction.atomic():
        redirect.save()
        record_audit(
            redirect.project,
            "redirect.updated",
            {"from_path": from_norm, "to_path": to_norm, "match_type": match_type},
        )
    invalidate_redirect_cache()
    return redirect


def delete_redirect(redirect: models.Redirect) -> None:
    """Delete a redirect, invalidating the cache and auditing the removal."""
    project = redirect.project
    snapshot = {
        "from_path": redirect.from_path,
        "to_path": redirect.to_path,
        "match_type": redirect.match_type,
    }
    with transaction.atomic():
        redirect.delete()
        record_audit(project, "redirect.deleted", snapshot)
    invalidate_redirect_cache()


# --------------------------------------------------------------------------
# URL layouts
# --------------------------------------------------------------------------


def _layout(language_enabled: bool, version_enabled: bool) -> tuple[bool, bool]:
    return bool(language_enabled), bool(version_enabled)


def _layout_name(layout: tuple[bool, bool]) -> str:
    language, version = layout
    if language and version:
        return "language+version"
    if language:
        return "language"
    if version:
        return "version"
    return "root-only"


def _remap_key(
    remainder: str,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
    language_label: str,
    version_label: str,
) -> str:
    """Re-map the part of a key/URL after the project root to a new layout.

    Dimensions newly enabled by ``new_layout`` are filled from the labels;
    dimensions being dropped disappear from the path.  Paths missing some
    of the old dimension segments are transformed best-effort (missing
    segments come from the labels), which also keeps partial redirect
    targets usable.
    """
    segments = [segment for segment in remainder.split("/") if segment]
    trailing = remainder.endswith("/")
    language: str | None = None
    version: str | None = None
    index = 0
    if old_layout[0] and index < len(segments):
        language = segments[index]
        index += 1
    if old_layout[1] and index < len(segments):
        version = segments[index]
        index += 1
    rest = segments[index:]
    out: list[str] = []
    if new_layout[0]:
        out.append(language or language_label)
    if new_layout[1]:
        out.append(version or version_label)
    out.extend(rest)
    return "/".join(out) + ("/" if trailing and out else "")


def _remap_site_path(
    site_path: str,
    root: str,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
    language_label: str,
    version_label: str,
) -> str:
    """Re-map a site-absolute path such as ``/docs/en/latest/x`` to a layout."""
    normalized = paths.normalize_site_path(site_path)
    remainder = paths.match_root_prefix(normalized, root)
    if remainder is None:
        return normalized
    remapped = _remap_key(
        remainder.strip("/"), old_layout, new_layout, language_label, version_label
    )
    root_prefix = "/" + root
    return root_prefix + ("/" + remapped if remapped else "")


def _publication_lineage_keys(project: models.Project) -> set[str]:
    """Return the keys provably belonging to the project's publications.

    The control plane never sees the publisher's S3 uploads, so no manifest
    maps a publication to its objects. The only authoritative membership
    record is the re-keying lineage of the project's own state machines:
    every destination key of a layout change was derived from a source key
    whose enabled-dimension labels were validated against the project's
    publications when the change was requested. Root migrations carry that
    proof across root moves (a copied destination inherits the membership
    of its recorded source), so the closure chains both mappings.
    """
    proven: set[str] = set()
    for change in models.LayoutChange.objects.filter(project_id=project.pk):
        proven.update((change.key_mapping or {}).values())
    migrations = [
        migration.key_mapping or {}
        for migration in models.PathMigration.objects.filter(project_id=project.pk)
    ]
    changed = True
    while changed:
        changed = False
        for mapping in migrations:
            for source, destination in mapping.items():
                if source in proven and destination not in proven:
                    proven.add(destination)
                    changed = True
    return proven


def _layout_change_mapping(
    project: models.Project,
    new_layout: tuple[bool, bool],
    language_label: str,
    version_label: str,
    storage: S3Storage,
) -> dict[str, str]:
    """Compute the old-to-new key mapping and reject stray conflicts.

    Only keys whose old dimension segments belong to a publication of the
    project are re-keyed; other content under the root is stray. Any stray
    key in the affected old layout (even one that does not sit at a
    re-keying destination) rejects the toggle, whether a dimension is being
    disabled (nothing would serve or re-key it afterwards) or enabled (it
    would be left unreachable under the new layout); the stray objects are
    never moved or deleted. A key that already sits at a destination of
    the re-keying disables the toggle as well.

    Enabling a dimension that the old layout had disabled opens a blind
    slot: the dimension's segment is absent from old-layout paths, so label
    checks cannot validate it and arbitrary objects under the root would be
    re-keyed into the publication tree. Exact per-publication membership is
    not derivable from doc-path shape, so such transitions demand recorded
    lineage instead: every object must be traceable (through prior layout
    change and root migration key mappings) to label-validated publication
    content. Unprovable objects are reported as stray; an empty root is
    trivially provable.
    """
    root = project.root_path
    old_layout = _layout(project.language_enabled, project.version_enabled)
    languages = set(project.publications.values_list("language", flat=True))
    versions = set(project.publications.values_list("version", flat=True))
    opening_blind_slot = (new_layout[0] and not old_layout[0]) or (
        new_layout[1] and not old_layout[1]
    )
    lineage: set[str] | None = None
    if opening_blind_slot:
        lineage = _publication_lineage_keys(project)
    mapping: dict[str, str] = {}
    strays: list[str] = []
    for key in storage.list_keys(f"{root}/"):
        remainder = key[len(root) + 1:]
        segments = [segment for segment in remainder.split("/") if segment]
        index = 0
        language: str | None = None
        version: str | None = None
        if old_layout[0] and index < len(segments):
            language = segments[index]
            index += 1
        if old_layout[1] and index < len(segments):
            version = segments[index]
            index += 1
        if (
            old_layout[0]
            and language not in languages
            or old_layout[1]
            and version not in versions
        ):
            strays.append(key)
            continue
        if lineage is not None and key not in lineage:
            strays.append(key)
            continue
        out: list[str] = []
        if new_layout[0]:
            out.append(language or language_label)
        if new_layout[1]:
            out.append(version or version_label)
        out.extend(segments[index:])
        mapped = "/".join(out) + ("/" if remainder.endswith("/") and out else "")
        mapping[key] = f"{root}/{mapped}" if mapped else root
    if strays:
        sample = ", ".join(repr(key) for key in sorted(strays)[:5])
        raise ServiceError(
            409,
            f"cannot change the URL layout: {len(strays)} object(s) under "
            f"the old layout cannot be unambiguously assigned to an "
            f"existing publication ({sample}); remove or re-publish them "
            "and retry the change",
        )
    sources = set(mapping)
    for destination in sorted(mapping.values()):
        if destination in sources:
            continue
        if storage.exists(destination):
            raise ServiceError(
                409,
                f"cannot re-key: destination key {destination!r} already "
                "holds unrelated content",
            )
    return mapping


def toggle_project_layout(
    project: models.Project,
    *,
    language_enabled: bool,
    version_enabled: bool,
    language_label: str | None = None,
    version_label: str | None = None,
    storage: S3Storage,
) -> models.LayoutChange:
    """Request and execute a layout toggle (dimensions, not aliases).

    Raises:
        ServiceError: 409 when the layout already matches, when another
            non-completed layout change exists for the project, when a
            dimension cannot be disabled because not exactly one distinct
            label survives, or when the storage holds a stray conflicting
            subtree; 422 for invalid labels.
    """
    project = models.Project.objects.get(pk=project.pk)
    if models.LayoutChange.objects.filter(project=project).exclude(
        status=models.LayoutChange.STATUS_COMPLETED
    ).exists():
        raise ServiceError(
            409,
            "a layout change is already pending or switched for this "
            "project; resume or complete it first",
        )
    old_layout = _layout(project.language_enabled, project.version_enabled)
    new_layout = _layout(language_enabled, version_enabled)
    if old_layout == new_layout:
        raise ServiceError(409, "project already uses this URL layout")

    resolved_language_label = ""
    resolved_version_label = ""
    if not new_layout[0]:
        resolved_language_label = _sole_surviving_label(
            project, "language", language_label
        )
    elif not old_layout[0]:
        resolved_language_label = (
            (language_label or "").strip()
            or project.language_label
            or DEFAULT_LANGUAGE_LABEL
        )
    if not new_layout[1]:
        resolved_version_label = _sole_surviving_label(
            project, "version", version_label
        )
    elif not old_layout[1]:
        resolved_version_label = (
            (version_label or "").strip()
            or project.version_label
            or DEFAULT_VERSION_LABEL
        )
    for label, name in (
        (resolved_language_label, "language"),
        (resolved_version_label, "version"),
    ):
        if label and not paths.is_safe_segment(label):
            raise ServiceError(422, f"invalid {name} label: {label!r}")

    mapping = _layout_change_mapping(
        project, new_layout, resolved_language_label, resolved_version_label, storage
    )
    with transaction.atomic():
        locked = models.Project.objects.select_for_update().get(pk=project.pk)
        if models.LayoutChange.objects.filter(project=locked).exclude(
            status=models.LayoutChange.STATUS_COMPLETED
        ).exists():
            raise ServiceError(
                409,
                "a competing layout change was started concurrently; retry "
                "the toggle",
            )
        change = models.LayoutChange.objects.create(
            project=project,
            old_language_enabled=old_layout[0],
            old_version_enabled=old_layout[1],
            new_language_enabled=new_layout[0],
            new_version_enabled=new_layout[1],
            language_label=resolved_language_label,
            version_label=resolved_version_label,
            key_mapping=mapping,
            status=models.LayoutChange.STATUS_PENDING,
        )
        record_audit(
            project,
            "layout.change_requested",
            {
                "old": _layout_name(old_layout),
                "new": _layout_name(new_layout),
                "language_label": resolved_language_label,
                "version_label": resolved_version_label,
                "keys": len(mapping),
            },
        )
    return run_layout_change(change, storage)


def _sole_surviving_label(
    project: models.Project, field: str, operator_label: str | None
) -> str:
    """Return the single label surviving a dimension disable.

    The dimension may only be disabled when exactly one distinct label
    survives across the project's publications; with no publications at all
    the operator-provided label is required instead.
    """
    labels = _distinct_labels(project.publications, field)
    if len(labels) > 1:
        raise ServiceError(
            409,
            f"cannot disable the {field} dimension: {len(labels)} distinct "
            f"{field} labels exist",
        )
    provided = (operator_label or "").strip()
    if labels:
        sole = labels[0]
        if provided and provided != sole:
            raise ServiceError(
                409, f"the only surviving {field} label is {sole!r}, not {provided!r}"
            )
        return sole
    if not provided:
        raise ServiceError(
            409, f"cannot disable the {field} dimension: no {field} label survives"
        )
    return provided


def _assert_layout_change_current(
    change: models.LayoutChange, project: models.Project
) -> None:
    """Reject a layout change whose recorded old layout no longer applies."""
    recorded_old = (
        change.old_language_enabled,
        change.old_version_enabled,
    )
    current = _layout(project.language_enabled, project.version_enabled)
    if current != recorded_old:
        raise ServiceError(
            409,
            "stale layout change: the project layout has changed since the "
            "change was requested; remove it and request a new one",
        )


def run_layout_change(change: models.LayoutChange, storage: S3Storage) -> models.LayoutChange:
    """Execute (or resume) a layout change: copy -> switch -> delete.

    Raises:
        ServiceError: 409 when the change is stale (the project layout has
            moved on since it was requested), a competing non-completed
            change exists, or a recorded mapping source key no longer
            exists in storage; nothing is switched or deleted then.
    """
    mapping: dict[str, str] = change.key_mapping or {}
    if change.status == models.LayoutChange.STATUS_PENDING:
        _assert_layout_change_current(
            change, models.Project.objects.get(pk=change.project_id)
        )
        for source in sorted(mapping):
            destination = mapping[source]
            if source != destination and not storage.exists(source):
                raise ServiceError(
                    409,
                    f"cannot resume the layout change: the source key "
                    f"{source!r} no longer exists in storage; restore it "
                    "(or delete the change) and retry",
                )
        for source in sorted(mapping):
            destination = mapping[source]
            if source != destination:
                storage.copy_object(source, destination)
        try:
            with transaction.atomic():
                project = models.Project.objects.select_for_update().get(pk=change.project_id)
                if models.LayoutChange.objects.filter(
                    project_id=project.pk
                ).exclude(status=models.LayoutChange.STATUS_COMPLETED).exclude(
                    pk=change.pk
                ).exists():
                    raise ServiceError(
                        409,
                        "a competing layout change exists for this project; "
                        "resume or complete it first",
                    )
                _assert_layout_change_current(change, project)
                old_layout = _layout(project.language_enabled, project.version_enabled)
                new_layout = _layout(
                    change.new_language_enabled, change.new_version_enabled
                )
                project.language_enabled = new_layout[0]
                project.version_enabled = new_layout[1]
                project.language_label = change.language_label if not new_layout[0] else ""
                project.version_label = change.version_label if not new_layout[1] else ""
                project.save()
                _rewrite_redirects_for_layout(
                    project,
                    old_layout,
                    new_layout,
                    change.language_label,
                    change.version_label,
                )
                created_ids = _create_layout_redirects(
                    change, project, old_layout, new_layout
                )
                change.created_redirect_ids = created_ids
                change.status = models.LayoutChange.STATUS_SWITCHED
                change.save(
                    update_fields=["status", "created_redirect_ids", "updated_at"]
                )
                record_audit(
                    project,
                    "layout.changed",
                    {
                        "old": _layout_name(old_layout),
                        "new": _layout_name(new_layout),
                        "language_label": change.language_label,
                        "version_label": change.version_label,
                        "layout_redirects": len(created_ids),
                    },
                )
        finally:
            invalidate_redirect_cache()
    if change.status == models.LayoutChange.STATUS_SWITCHED:
        for source in sorted(mapping):
            if source != mapping.get(source):
                storage.delete_object(source)
        change.status = models.LayoutChange.STATUS_COMPLETED
        change.completed_at = timezone.now()
        change.save(update_fields=["status", "completed_at", "updated_at"])
        record_audit(
            change.project,
            "layout.change_completed",
            {
                "old": _layout_name(
                    _layout(change.old_language_enabled, change.old_version_enabled)
                ),
                "new": _layout_name(
                    _layout(change.new_language_enabled, change.new_version_enabled)
                ),
            },
        )
    return change


def _rewrite_redirects_for_layout(
    project: models.Project,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
    language_label: str,
    version_label: str,
) -> int:
    """Rewrite the project's redirect paths for a new layout (transactional).

    Only operator-managed redirects are remapped here: redirects recorded
    in a layout change's ``created_redirect_ids`` are maintained by
    :func:`_maintain_prior_layout_redirects` instead (their dimension
    segments are layout state, not doc paths, and remapping them would
    fabricate phantom paths such as ``latest/latest``).

    Raises:
        ServiceError: 409 (actionable) when a rewritten path would collide
            with an existing redirect; the surrounding transaction rolls
            back and the change stays retryable.
    """
    root_prefix = "/" + project.root_path
    managed_ids = _layout_created_redirect_ids(project)
    count = 0
    for redirect in list(
        models.Redirect.objects.filter(project=project).exclude(pk__in=managed_ids)
    ):
        updates = {}
        for field in ("from_path", "to_path"):
            value = getattr(redirect, field)
            if value == root_prefix or value.startswith(root_prefix + "/"):
                updates[field] = _remap_site_path(
                    value,
                    project.root_path,
                    old_layout,
                    new_layout,
                    language_label,
                    version_label,
                )
        if not updates:
            continue
        new_from = updates.get("from_path", redirect.from_path)
        new_to = updates.get("to_path", redirect.to_path)
        if new_from == new_to:
            # The remap collapsed the redirect onto itself: it became a
            # no-op and is removed instead of saved as a self-redirect.
            with transaction.atomic():
                redirect.delete()
                record_audit(
                    redirect.project,
                    "redirect.deleted",
                    {
                        "from_path": redirect.from_path,
                        "to_path": redirect.to_path,
                        "match_type": redirect.match_type,
                        "reason": "became a no-op after the layout change",
                    },
                )
            invalidate_redirect_cache()
            count += 1
            continue
        redirect.from_path = new_from
        redirect.to_path = new_to
        try:
            redirect.save(update_fields=["from_path", "to_path", "updated_at"])
        except IntegrityError as exc:
            raise ServiceError(
                409,
                f"rewriting the redirect to {new_from!r} collides with an "
                f"existing {redirect.match_type} redirect; remove it (or move "
                "it away) and retry the layout change",
            ) from exc
        invalidate_redirect_cache()
        count += 1
    return count


def _key_dimension_prefix(
    key: str, root: str, layout: tuple[bool, bool]
) -> str | None:
    """Return ``key`` truncated after the layout's dimension segments."""
    if not key.startswith(root + "/"):
        return None
    segments = [segment for segment in key[len(root) + 1:].split("/") if segment]
    dimensions = sum(layout)
    if len(segments) < dimensions:
        return None
    return "/".join([root, *segments[:dimensions]])


def _layout_redirect_pairs(
    change: models.LayoutChange,
    project: models.Project,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
) -> list[tuple[str, str]]:
    """Return (old_prefix, new_prefix) pairs derived from the key mapping.

    Only prefixes of keys that were actually mapped for re-keying (real
    publication prefixes with content) produce redirects; publications
    without storage never do, so no phantom label-filled paths such as
    ``latest/latest`` or ``latest/en`` can be generated.
    """
    root = project.root_path
    pairs: list[tuple[str, str]] = []
    for source in sorted(change.key_mapping or {}):
        old_prefix = _key_dimension_prefix(source, root, old_layout)
        new_prefix = _key_dimension_prefix(
            change.key_mapping[source], root, new_layout
        )
        if old_prefix is None or new_prefix is None or old_prefix == new_prefix:
            continue
        pair = ("/" + old_prefix, "/" + new_prefix)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def _validate_layout_redirect(
    project: models.Project, from_path: str, to_path: str
) -> None:
    """Validate a generated layout redirect with the standard rules.

    The source may be the project's own root (root-only layouts), so the
    shadow check excludes it; nested roots under the source still reject.
    """
    from_norm = paths.normalize_site_path(from_path)
    to_norm = paths.normalize_site_path(to_path)
    first_segment = from_norm.strip("/").split("/")[0] if from_norm != "/" else ""
    if first_segment in paths.RESERVED_SEGMENTS:
        raise ServiceError(
            422, f"redirect source {from_norm!r} lives in a reserved namespace"
        )
    if from_norm == to_norm:
        raise ServiceError(422, "redirect source and destination must differ")
    _ensure_no_root_shadow(
        from_norm, models.Redirect.MATCH_PREFIX, exclude_roots={project.root_path}
    )
    _ensure_no_redirect_loop(from_norm, to_norm)


def _layout_created_redirect_ids(project: models.Project) -> set[int]:
    """Return the IDs of the redirects recorded as layout-created."""
    ids: set[int] = set()
    for other in models.LayoutChange.objects.filter(project_id=project.pk):
        ids.update(other.created_redirect_ids or [])
    return ids


def _maintain_prior_layout_redirects(
    change: models.LayoutChange,
    project: models.Project,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
) -> None:
    """Remap or remove the redirects installed by previous layout changes.

    Only redirects recorded in ``created_redirect_ids`` are touched;
    unrelated manual redirects are preserved. The destination of each
    prior layout redirect is remapped from the old layout to the new one so
    old URLs keep resolving to current content, and a redirect that
    collapses onto its own source (the stale inverse of this change) is
    removed instead of left behind to loop.
    """
    prior_ids = _layout_created_redirect_ids(project)
    if not prior_ids:
        return
    for redirect in models.Redirect.objects.filter(pk__in=prior_ids):
        new_to = _remap_site_path(
            redirect.to_path,
            project.root_path,
            old_layout,
            new_layout,
            change.language_label,
            change.version_label,
        )
        if new_to == redirect.from_path:
            with transaction.atomic():
                snapshot = {
                    "from_path": redirect.from_path,
                    "to_path": redirect.to_path,
                    "match_type": redirect.match_type,
                }
                redirect.delete()
                record_audit(
                    project,
                    "redirect.deleted",
                    {**snapshot, "reason": "replaced by the layout change"},
                )
        elif new_to != redirect.to_path:
            redirect.to_path = new_to
            with transaction.atomic():
                redirect.save(update_fields=["to_path", "updated_at"])
                record_audit(
                    project,
                    "redirect.updated",
                    {
                        "from_path": redirect.from_path,
                        "to_path": new_to,
                        "match_type": redirect.match_type,
                        "reason": "destination remapped by the layout change",
                    },
                )
    invalidate_redirect_cache()


def _create_layout_redirects(
    change: models.LayoutChange,
    project: models.Project,
    old_layout: tuple[bool, bool],
    new_layout: tuple[bool, bool],
) -> list[int]:
    """Create validated old-layout -> new-layout prefix redirects.

    Pairs come from the recorded key mapping (actual publication
    prefixes with content), and redirects created by previous layout
    changes are maintained by their recorded IDs only.

    Raises:
        ServiceError: 409 (actionable) when a prefix redirect already sits
            at a generated source with a different destination; the
            surrounding transaction rolls back and the change stays
            retryable.
    """
    _maintain_prior_layout_redirects(change, project, old_layout, new_layout)
    pairs = _layout_redirect_pairs(change, project, old_layout, new_layout)
    if not pairs:
        return []
    created_ids: list[int] = []
    for from_path, to_path in pairs:
        existing = models.Redirect.objects.filter(
            from_path=from_path, match_type=models.Redirect.MATCH_PREFIX
        ).first()
        if existing is not None:
            if existing.to_path != to_path:
                raise ServiceError(
                    409,
                    f"cannot create the layout redirect {from_path!r} -> "
                    f"{to_path!r}: a prefix redirect to {existing.to_path!r} "
                    "already exists there; remove it and retry the layout change",
                )
            if not existing.enabled:
                existing.enabled = True
                with transaction.atomic():
                    existing.save(update_fields=["enabled", "updated_at"])
                    record_audit(
                        project,
                        "redirect.updated",
                        {"from_path": from_path, "to_path": to_path, "match_type": "prefix"},
                    )
                invalidate_redirect_cache()
            created_ids.append(existing.pk)
            continue
        _validate_layout_redirect(project, from_path, to_path)
        try:
            with transaction.atomic():
                redirect = models.Redirect.objects.create(
                    project=project,
                    match_type=models.Redirect.MATCH_PREFIX,
                    from_path=from_path,
                    to_path=to_path,
                    enabled=True,
                )
        except IntegrityError as exc:
            raise ServiceError(
                409,
                f"cannot create the layout redirect {from_path!r} -> "
                f"{to_path!r}: a prefix redirect already exists there; remove "
                "it and retry the layout change",
            ) from exc
        created_ids.append(redirect.pk)
        invalidate_redirect_cache()
    return created_ids


# --------------------------------------------------------------------------
# Root migrations
# --------------------------------------------------------------------------


def _assert_migration_destination_free(
    project: models.Project, old_root: str, new_root: str
) -> None:
    """Run the root migration destination boundary checks.

    The destination must be a valid unclaimed root that shadows no
    existing nested root, the current root must not contain a nested
    project (it would be orphaned behind the old-to-new redirect), and the
    destination must not sit under any other registered root.
    """
    for candidate in models.Project.objects.exclude(pk=project.pk):
        if candidate.root_path == new_root:
            raise ServiceError(
                409, f"destination {new_root!r} is already claimed by another project"
            )
        if paths.is_ancestor_root(new_root, candidate.root_path):
            raise ServiceError(
                409,
                f"destination {new_root!r} would shadow the existing nested "
                f"root {candidate.root_path!r}",
            )
        if paths.is_ancestor_root(old_root, candidate.root_path):
            raise ServiceError(
                409,
                f"the current root {old_root!r} contains the nested root "
                f"{candidate.root_path!r}; migrate it first",
            )
        if paths.is_ancestor_root(candidate.root_path, new_root):
            raise ServiceError(
                409,
                f"destination {new_root!r} is nested under the existing root "
                f"{candidate.root_path!r}",
            )


def create_root_migration(
    project: models.Project, new_root_path: str
) -> models.PathMigration:
    """Validate a root migration destination and create its state row.

    Only one non-completed migration may exist per project; the check and
    the destination boundary checks run with the project row locked and
    the PostgreSQL advisory claim lock held, so concurrent claims and
    migration requests cannot interleave.

    Raises:
        ServiceError: 422 for an invalid destination; 409 for the boundary
            violations above, a destination equal to the current root, or
            a competing non-completed migration.
    """
    try:
        new_root = paths.normalize_root_path(new_root_path)
    except paths.InvalidPathError as exc:
        raise ServiceError(422, str(exc)) from exc
    with transaction.atomic():
        project = models.Project.objects.select_for_update().get(pk=project.pk)
        _acquire_claim_lock()
        if models.PathMigration.objects.filter(project=project).exclude(
            status=models.PathMigration.STATUS_COMPLETED
        ).exists():
            raise ServiceError(
                409,
                "another root migration is already pending or switched for "
                "this project; resume or complete it first",
            )
        old_root = project.root_path
        if new_root == old_root:
            raise ServiceError(
                409, "destination equals the project's current root path"
            )
        _assert_migration_destination_free(project, old_root, new_root)
        migration = models.PathMigration.objects.create(
            project=project, old_root=old_root, new_root=new_root
        )
        record_audit(
            project,
            "migration.requested",
            {"old_root": old_root, "new_root": new_root},
        )
    return migration


def _validate_root_migration_redirect(
    old_root: str, new_root: str, exclude_roots: Iterable[str]
) -> None:
    """Validate the generated old-root -> new-root prefix redirect.

    The standard loop/hop and root-shadow rules apply: a preexisting
    destination-to-source redirect (or any chain looping back to the old
    root) is rejected before the transactional switch.

    Raises:
        ServiceError: 409 for a loop/hop overload or a shadowing root,
            422 for a reserved source namespace.
    """
    from_norm = paths.normalize_site_path("/" + old_root)
    to_norm = paths.normalize_site_path("/" + new_root)
    first_segment = from_norm.strip("/").split("/")[0] if from_norm != "/" else ""
    if first_segment in paths.RESERVED_SEGMENTS:
        raise ServiceError(
            422, f"redirect source {from_norm!r} lives in a reserved namespace"
        )
    if from_norm == to_norm:
        raise ServiceError(422, "redirect source and destination must differ")
    _ensure_no_root_shadow(
        from_norm, models.Redirect.MATCH_PREFIX, exclude_roots=exclude_roots
    )
    _ensure_no_redirect_loop(from_norm, to_norm)


def run_root_migration(
    migration: models.PathMigration, storage: S3Storage
) -> models.PathMigration:
    """Execute (or resume) a root migration: copy -> switch -> delete.

    The project row is locked (with the PostgreSQL advisory claim lock)
    and the current root is asserted against the migration's expectation
    before any storage is touched: the copy/switch phases require the
    project to still live at ``old_root``, the delete phase requires it to
    live at the switched ``new_root``, and the destination boundary checks
    are re-run inside the switch transaction.

    Raises:
        ServiceError: 409 when the migration is stale or competing, or a
            redirect rule blocks the generated old-to-new prefix redirect;
            storage is not touched in that case.
    """
    if migration.status == models.PathMigration.STATUS_COMPLETED:
        return migration
    if migration.status == models.PathMigration.STATUS_PENDING:
        from_path = "/" + migration.old_root
        to_path = "/" + migration.new_root
        # Validate the generated old-to-new redirect with the standard
        # loop/hop rules BEFORE anything changes: a preexisting
        # destination-to-source redirect yields a clean 409 and leaves the
        # migration pending, retryable, with serving untouched.
        _validate_root_migration_redirect(
            migration.old_root,
            migration.new_root,
            exclude_roots={migration.old_root, migration.new_root},
        )
        old_prefix = f"{migration.old_root}/"
        new_prefix = f"{migration.new_root}/"
        mapping: dict[str, str] = dict(migration.key_mapping or {})
        try:
            with transaction.atomic():
                project = models.Project.objects.select_for_update().get(
                    pk=migration.project_id
                )
                _acquire_claim_lock()
                if models.PathMigration.objects.filter(
                    project_id=project.pk
                ).exclude(status=models.PathMigration.STATUS_COMPLETED).exclude(
                    pk=migration.pk
                ).exists():
                    raise ServiceError(
                        409,
                        "a competing root migration exists for this "
                        "project; resume or complete it first",
                    )
                if project.root_path != migration.old_root:
                    raise ServiceError(
                        409,
                        f"stale root migration: the project root is "
                        f"{project.root_path!r}, not the recorded "
                        f"{migration.old_root!r}; refusing to copy or switch",
                    )
                # Re-run the destination boundary checks inside the
                # switch transaction: a concurrent claim of the
                # destination (or a nested root) since the request must not
                # be switched onto.
                _assert_migration_destination_free(
                    project, migration.old_root, migration.new_root
                )
                if not mapping:
                    for key in storage.list_keys(old_prefix):
                        destination = new_prefix + key[len(old_prefix):]
                        storage.copy_object(key, destination)
                        mapping[key] = destination
                _rewrite_redirects_for_migration(
                    project, migration.old_root, migration.new_root
                )
                project.root_path = migration.new_root
                project.save(update_fields=["root_path", "updated_at"])
                # Re-validate inside the switch transaction: concurrent
                # claims or redirects since the pre-check still loop the
                # generated redirect, and the rollback keeps serving intact.
                _validate_root_migration_redirect(
                    migration.old_root,
                    migration.new_root,
                    exclude_roots={migration.new_root},
                )
                try:
                    with transaction.atomic():
                        models.Redirect.objects.create(
                            project=project,
                            match_type=models.Redirect.MATCH_PREFIX,
                            from_path=from_path,
                            to_path=to_path,
                        )
                except IntegrityError as exc:
                    raise ServiceError(
                        409,
                        f"cannot install the {from_path!r} -> {to_path!r} "
                        "prefix redirect: a prefix redirect already exists "
                        "there; remove it and retry the migration",
                    ) from exc
                migration.key_mapping = mapping
                migration.status = models.PathMigration.STATUS_SWITCHED
                migration.save(
                    update_fields=["key_mapping", "status", "updated_at"]
                )
                record_audit(
                    project,
                    "migration.switched",
                    {"old_root": migration.old_root, "new_root": migration.new_root},
                )
        finally:
            invalidate_redirect_cache()
    if migration.status == models.PathMigration.STATUS_SWITCHED:
        # The migration must still own the project root before any old key
        # is deleted: a stale (superseded) migration cannot touch storage.
        with transaction.atomic():
            project = models.Project.objects.select_for_update().get(
                pk=migration.project_id
            )
            _acquire_claim_lock()
            if project.root_path != migration.new_root:
                raise ServiceError(
                    409,
                    f"stale root migration: the project root is "
                    f"{project.root_path!r}, not the switched "
                    f"{migration.new_root!r}; refusing to delete the old keys",
                )
        # Delete exactly the copied old source keys recorded in the key
        # mapping snapshot (never other content, even when the new root
        # lives inside the old prefix as in an upward migration).
        for source in sorted(migration.key_mapping or {}):
            storage.delete_object(source)
        migration.status = models.PathMigration.STATUS_COMPLETED
        migration.completed_at = timezone.now()
        migration.save(update_fields=["status", "completed_at", "updated_at"])
        record_audit(
            migration.project,
            "migration.completed",
            {"old_root": migration.old_root, "new_root": migration.new_root},
        )
    return migration


def _rewrite_redirects_for_migration(
    project: models.Project, old_root: str, new_root: str
) -> int:
    """Rewrite the project's redirect paths from the old root to the new one.

    Raises:
        ServiceError: 409 (actionable) when a rewritten path would collide
            with an existing redirect; the surrounding transaction rolls
            back and the migration stays retryable.
    """
    old_prefix = "/" + old_root
    new_prefix = "/" + new_root
    count = 0
    for redirect in list(models.Redirect.objects.filter(project=project)):
        updates = {}
        for field in ("from_path", "to_path"):
            value = getattr(redirect, field)
            if value == old_prefix:
                updates[field] = new_prefix
            elif value.startswith(old_prefix + "/"):
                updates[field] = new_prefix + value[len(old_prefix):]
        if not updates:
            continue
        new_from = updates.get("from_path", redirect.from_path)
        redirect.from_path = new_from
        redirect.to_path = updates.get("to_path", redirect.to_path)
        try:
            redirect.save(update_fields=["from_path", "to_path", "updated_at"])
        except IntegrityError as exc:
            raise ServiceError(
                409,
                f"rewriting the redirect to {new_from!r} collides with an "
                f"existing {redirect.match_type} redirect; remove it (or move "
                "it away) and retry the migration",
            ) from exc
        invalidate_redirect_cache()
        count += 1
    return count


# --------------------------------------------------------------------------
# Serving helpers
# --------------------------------------------------------------------------


def match_project_for_path(raw_path: str) -> tuple[models.Project, str] | None:
    """Return ``(project, remainder)`` for the longest registered root match."""
    view = paths.normalize_request_path(raw_path)
    best_root: str | None = None
    for root in models.Project.objects.values_list("root_path", flat=True):
        prefixed = "/" + root
        if view == prefixed or view.startswith(prefixed + "/"):
            if best_root is None or len(root) > len(best_root):
                best_root = root
    if best_root is None:
        return None
    remainder = paths.match_root_prefix(raw_path, best_root)
    if remainder is None:
        return None
    project = models.Project.objects.filter(root_path=best_root).first()
    if project is None:
        return None
    return project, remainder


def parse_serving_url(project: models.Project, remainder: str) -> dict[str, Any] | None:
    """Parse the URL remainder after a project root against the layout.

    Returns one of:

    * ``{"action": "redirect", "target": "/docs/en/latest/"}`` for the
      layout root without a trailing slash;
    * ``{"action": "serve", "base_key": "docs/en/latest/", "doc_path": ...}``;
    * ``None`` when the URL does not fit the layout or contains traversal
      segments.
    """
    segments = [segment for segment in remainder.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        return None
    trailing = remainder.endswith("/")
    dimensions: list[str] = []
    if project.language_enabled:
        dimensions.append("language")
    if project.version_enabled:
        dimensions.append("version")
    if len(segments) < len(dimensions):
        return None
    root = project.root_path
    dimension_values = segments[: len(dimensions)]
    doc_segments = segments[len(dimensions):]
    if not doc_segments:
        if not trailing:
            target = "/" + "/".join([root, *dimension_values]) + "/"
            return {"action": "redirect", "target": target}
        doc_path = ""
    else:
        doc_path = "/".join(doc_segments) + ("/" if trailing else "")
    base_key = "/".join([root, *dimension_values]) + "/"
    return {"action": "serve", "base_key": base_key, "doc_path": doc_path}
