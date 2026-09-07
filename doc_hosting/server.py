"""FastAPI application: ingestion API, version API and documentation serving.

The application serves built documentation straight from the S3 bucket as
middleware: object keys mirror the URL paths under
``{root_path}/{language}/{version}/``, and registry metadata is stored as JSON
under the reserved ``_registry/`` prefix.
"""

from __future__ import annotations

import mimetypes
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from .settings import Settings, SettingsError, get_settings
from .storage import S3Storage

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PublishRequest(BaseModel):
    """Request body for the ingestion (publish) API."""

    commit_hash: str
    version: str
    language: str
    domain: str
    root_path: str


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


def _require_publish_token(request: Request) -> None:
    """Enforce bearer authentication on the ingestion API."""
    settings = _get_settings(request.app)
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
    if settings.publish_token is None:
        raise HTTPException(status_code=503, detail="publish token not configured")
    if not secrets.compare_digest(token, settings.publish_token):
        raise HTTPException(status_code=403, detail="invalid bearer token")


def _grouped_versions(registry: dict[str, Any], language: str | None) -> list[dict[str, Any]]:
    """Group registry builds by version, optionally filtered by language."""
    builds = [
        build
        for build in registry.get("builds", [])
        if language is None or build.get("language") == language
    ]
    builds = sorted(builds, key=lambda build: str(build.get("registered_at", "")))
    grouped: dict[str, dict[str, Any]] = {}
    for build in builds:
        entry = grouped.setdefault(
            build["version"],
            {"version": build["version"], "languages": [], "commit_hash": build["commit_hash"]},
        )
        if build["language"] not in entry["languages"]:
            entry["languages"].append(build["language"])
        entry["commit_hash"] = build["commit_hash"]
    for entry in grouped.values():
        entry["languages"] = sorted(entry["languages"])
    return list(grouped.values())


def create_app() -> FastAPI:
    """Build the doc-hosting FastAPI application."""
    app = FastAPI(
        title="doc-hosting API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness endpoint (no S3 configuration required)."""
        return {"status": "ok"}

    @app.post("/api/v1/publish", status_code=201)
    def publish(body: PublishRequest, request: Request) -> dict[str, str]:
        """Ingestion API: register a documentation build in the registry."""
        _require_publish_token(request)
        _validate_slug(body.root_path, "root_path")
        _validate_slug(body.language, "language")
        _validate_slug(body.version, "version")
        storage = _get_storage(request.app)
        registry = storage.get_registry(body.root_path) or {
            "root_path": body.root_path,
            "domain": body.domain,
            "builds": [],
        }
        registry["domain"] = body.domain
        registry["builds"] = [
            build
            for build in registry.get("builds", [])
            if not (
                build.get("language") == body.language
                and build.get("version") == body.version
            )
        ]
        entry = {
            "language": body.language,
            "version": body.version,
            "commit_hash": body.commit_hash,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        registry["builds"].append(entry)
        storage.put_registry(body.root_path, registry)
        return {"root_path": body.root_path, "domain": body.domain, **entry}

    @app.get("/api/v1/versions")
    def versions(root_path: str, request: Request, language: str | None = None) -> dict[str, Any]:
        """Version API: list registered versions (and languages) for a root path."""
        if not _is_safe_slug(root_path):
            raise HTTPException(
                status_code=422,
                detail=f"invalid root_path: {root_path!r} must be a single URL-safe segment",
            )
        storage = _get_storage(request.app)
        registry = storage.get_registry(root_path)
        if registry is None:
            raise HTTPException(status_code=404, detail=f"unknown root_path: {root_path}")
        return {
            "root_path": root_path,
            "domain": registry.get("domain"),
            "versions": _grouped_versions(registry, language),
        }

    @app.get("/{root_path}/{language}/{version}")
    def redirect_to_version_index(
        root_path: str, language: str, version: str
    ) -> RedirectResponse:
        """Redirect a version root URL to its trailing-slash index."""
        return RedirectResponse(
            url=f"/{root_path}/{language}/{version}/", status_code=307
        )

    @app.get("/{root_path}/{language}/{version}/{doc_path:path}")
    def serve_doc(
        root_path: str, language: str, version: str, doc_path: str, request: Request
    ) -> Response:
        """Serve built documentation from the S3 bucket (middleware option)."""
        path_ok = _is_safe_slug(root_path) and _is_safe_slug(language) and _is_safe_slug(version)
        segments = [segment for segment in doc_path.split("/") if segment]
        if not path_ok or any(segment in (".", "..") for segment in segments):
            raise HTTPException(status_code=404, detail="not found")
        base_key = f"{root_path}/{language}/{version}/"
        if doc_path == "" or doc_path.endswith("/"):
            candidates = [doc_path + "index.html"]
        else:
            candidates = [doc_path, doc_path + "/index.html"]
        storage = _get_storage(request.app)
        for candidate in candidates:
            data = storage.get_bytes(base_key + candidate)
            if data is not None:
                content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
                return Response(content=data, media_type=content_type)
        raise HTTPException(
            status_code=404, detail=f"not found: /{base_key}{doc_path}"
        )

    return app
