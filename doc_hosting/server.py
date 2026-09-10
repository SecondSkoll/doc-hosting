"""FastAPI application: ingestion API, version API, admin and doc serving.

The PostgreSQL control plane (Django ORM) is the source of truth for all
metadata: projects and their publications, redirects, layout flags,
migrations and the immutable audit history.  The application still serves
built documentation straight from the S3 bucket, with object keys mirroring
the URL paths under the project root according to its active layout.

Every publication requires two independent credentials: the deployment-wide
bearer token (``Authorization: Bearer <APP_PUBLISH_TOKEN>``) as the
deployment gate, and a non-empty ``project_secret`` in the JSON body as the
per-root ownership proof.  Only the project secret is hashed and stored.
Publications are registered through the direct-upload flow ``POST
/api/v1/uploads`` -> presigned exact-key PUT URLs -> ``POST
/api/v1/uploads/{upload_id}/finalize`` (the API verifies the uploaded
objects' checksums and atomically registers the build).

Routes are registered so that the platform endpoints win: ``/health``,
``/api/v1/*``, the Django admin mounted at ``/manage/``, and finally one
catch-all that performs redirect resolution, the longest registered-root
match, project layout parsing and the S3 fetch.
"""

from __future__ import annotations

import mimetypes
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from . import db, paths

# Importing the server configures Django (the registry models require it).
db.setup()

from .registry import models, services
from .settings import Settings, SettingsError, get_settings
from .storage import S3Storage

SLUG_RE = paths.SLUG_RE


class UploadManifestEntry(BaseModel):
    """One declared build file: relative path, SHA-256 digest and size."""

    # Strict types: a size arriving as a string or bool is a schema error,
    # not a silently coerced value that later participates in verification.
    model_config = {"strict": True}

    path: str
    sha256: str
    size: int


class UploadBeginRequest(BaseModel):
    """Request body for beginning an API-authorised direct upload."""

    commit_hash: str
    version: str
    language: str
    domain: str
    root_path: str
    project_secret: str | None = None
    manifest: list[UploadManifestEntry] | None = None


class UploadFinalizeRequest(BaseModel):
    """Request body for finalizing a direct upload with its manifest."""

    project_secret: str | None = None
    manifest: list[UploadManifestEntry] | None = None


def _is_safe_slug(value: str) -> bool:
    """Return whether ``value`` is a single safe URL path segment."""
    return bool(value) and value not in (".", "..") and "/" not in value and bool(
        SLUG_RE.match(value)
    )


def _validate_slug(value: str, field: str) -> None:
    """Reject unsafe path segments on the ingestion API."""
    if not _is_safe_slug(value):
        raise HTTPException(
            status_code=422,
            detail=f"invalid {field}: {value!r} must be a single URL-safe segment",
        )


def _get_settings(app: FastAPI) -> Settings:
    """Return (and cache) the app settings, raising a 503 when unconfigured."""
    settings = getattr(app.state, "settings", None)
    if settings is None:
        try:
            settings = get_settings()
        except SettingsError as exc:
            raise HTTPException(
                status_code=503, detail=f"service not configured: {exc}"
            ) from exc
        app.state.settings = settings
    return settings


def _get_storage(app: FastAPI) -> S3Storage:
    """Return (and cache) the S3 storage client, raising a 503 when unconfigured."""
    storage = getattr(app.state, "storage", None)
    if storage is None:
        storage = S3Storage(_get_settings(app))
        app.state.storage = storage
    return storage


def _require_bearer(request: Request) -> str:
    """Return the bearer credential, raising 401 when missing or malformed."""
    header = request.headers.get("Authorization")
    if not header:
        raise HTTPException(
            status_code=401,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="invalid Authorization header, expected 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _require_publish_token(request: Request, bearer: str) -> Settings:
    """Pass the deployment-wide publishing gate, returning the settings."""
    settings = _get_settings(request.app)
    if settings.publish_token is None:
        raise HTTPException(
            status_code=503, detail="publish token is not configured"
        )
    if not secrets.compare_digest(bearer, settings.publish_token):
        raise HTTPException(status_code=403, detail="invalid publish token")
    return settings


def _require_project_secret(project_secret: str | None) -> str:
    """Return the non-empty project secret from the JSON body."""
    if not project_secret or not project_secret.strip():
        raise HTTPException(status_code=422, detail="project_secret must not be empty")
    return project_secret


def _fetch_object(storage: S3Storage, base_key: str, doc_path: str) -> Response | None:
    """Resolve and fetch the S3 object for a doc path (existing candidates)."""
    if doc_path == "" or doc_path.endswith("/"):
        candidates = [doc_path + "index.html"]
    else:
        candidates = [doc_path, doc_path + "/index.html"]
    for candidate in candidates:
        data = storage.get_bytes(base_key + candidate)
        if data is not None:
            content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
            return Response(content=data, media_type=content_type)
    return None


class _FullPathWSGI:
    """ASGI adapter serving a WSGI app with the full request path.

    Starlette mounts expose the matched prefix as ``root_path`` (WSGI
    ``SCRIPT_NAME``), and a2wsgi strips it from ``PATH_INFO``.  The Django
    URLconf carries the ``manage/`` prefix itself (so the admin is also
    directly reachable via ``manage.py runserver`` and the Django test
    client), so the prefix must stay part of the request path.
    """

    def __init__(self, wsgi_app: Any) -> None:
        import a2wsgi

        self._asgi_app = a2wsgi.WSGIMiddleware(wsgi_app)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            scope["root_path"] = ""
        await self._asgi_app(scope, receive, send)


def create_app() -> FastAPI:
    """Build the doc-hosting FastAPI application."""
    # Configure Django (pending migrations are applied here too, and the
    # bootstrap provisions the admin superuser from the charm's
    # admin-username/admin-password config options; the 12-factor charm
    # also runs `manage.py migrate` before the app starts).
    db.ensure_database_ready()

    app = FastAPI(
        title="doc-hosting API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness endpoint (no configuration required)."""
        return {"status": "ok"}

    @app.post("/api/v1/uploads", status_code=201)
    def begin_upload(body: UploadBeginRequest, request: Request) -> dict[str, Any]:
        """Begin an API-authorised direct upload.

        Authenticates like the publish API (deployment bearer token plus
        the project's shared secret, claiming the root on first use), then
        creates a pending upload session and returns one short-lived,
        path-restricted presigned S3 PUT URL per declared manifest file,
        each bound to the file's SHA-256. The returned upload ID identifies
        the publication for the finalize call.
        """
        bearer = _require_bearer(request)
        settings = _require_publish_token(request, bearer)
        _validate_slug(body.language, "language")
        _validate_slug(body.version, "version")
        project_secret = _require_project_secret(body.project_secret)
        try:
            return services.begin_upload(
                root_path=body.root_path,
                language=body.language,
                version=body.version,
                commit_hash=body.commit_hash,
                domain=body.domain,
                project_secret=project_secret,
                manifest=(
                    [entry.model_dump() for entry in body.manifest]
                    if body.manifest is not None
                    else None
                ),
                storage=_get_storage(request.app),
                url_ttl=settings.upload_url_ttl,
            )
        except services.ServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail
            ) from exc

    @app.post("/api/v1/uploads/{upload_id}/finalize", status_code=201)
    def finalize_upload(
        upload_id: int,
        body: UploadFinalizeRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        """Finalize a direct upload: verify the objects, register the build.

        Re-authenticates the project secret, verifies every manifest
        object's existence, size and stored SHA-256 in storage and then
        atomically registers the publication and completes the upload
        session. Replaying a completed session with an identical manifest
        is idempotent and answers 200.
        """
        bearer = _require_bearer(request)
        _require_publish_token(request, bearer)
        project_secret = _require_project_secret(body.project_secret)
        try:
            result = services.finalize_upload(
                upload_id,
                project_secret=project_secret,
                manifest=(
                    [entry.model_dump() for entry in body.manifest]
                    if body.manifest is not None
                    else None
                ),
                storage=_get_storage(request.app),
            )
        except services.ServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        if result.get("replay"):
            response.status_code = 200
        return result

    @app.get("/api/v1/versions")
    def versions(
        root_path: str, request: Request, language: str | None = None
    ) -> dict[str, Any]:
        """Version API: list registered versions (and languages) for a root path."""
        try:
            root = paths.normalize_root_path(root_path)
        except paths.InvalidPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.ensure_database_ready()
        project = models.Project.objects.filter(root_path=root).first()
        if project is None:
            raise HTTPException(status_code=404, detail=f"unknown root_path: {root}")
        return {
            "root_path": root,
            "domain": project.domain,
            "versions": services.grouped_versions(project, language),
            "layout": services.project_layout(project),
        }

    # Staff-only Django admin at /manage/ (after the API routes, before the
    # catch-all serving route).
    from django.core.wsgi import get_wsgi_application

    app.mount("/manage", _FullPathWSGI(get_wsgi_application()))

    @app.get("/{full_path:path}")
    def serve(full_path: str, request: Request) -> Response:
        """Serve docs from S3: redirect resolution -> root match -> layout parse."""
        raw_path = request.url.path
        db.ensure_database_ready()
        try:
            target = services.resolve_redirect(raw_path)
        except services.RedirectLoopError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail
            ) from exc
        if target is not None:
            return RedirectResponse(url=target, status_code=301)
        match = services.match_project_for_path(raw_path)
        if match is None:
            raise HTTPException(status_code=404, detail="not found")
        project, remainder = match
        parsed = services.parse_serving_url(project, remainder)
        if parsed is None:
            raise HTTPException(status_code=404, detail="not found")
        if parsed["action"] == "redirect":
            return RedirectResponse(url=parsed["target"], status_code=307)
        storage = _get_storage(request.app)
        response = _fetch_object(storage, parsed["base_key"], parsed["doc_path"])
        if response is None:
            # The internal S3 key never appears in public responses.
            raise HTTPException(status_code=404, detail="not found")
        return response

    return app
