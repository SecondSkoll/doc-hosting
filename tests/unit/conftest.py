"""Shared fixtures for the doc-hosting unit tests.

The control-plane database is configured before any Django import: the CI
workflow provides a PostgreSQL service through ``POSTGRESQL_DB_CONNECT_STRING``
(and the tests then run against it), while local runs fall back to a
throwaway SQLite file. The Django settings are placed in explicit test mode
(``DOC_HOSTING_DEV=1``) so the fail-closed SECRET_KEY/ALLOWED_HOSTS rules
permit the local fallbacks.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile

os.environ.setdefault("DOC_HOSTING_DEV", "1")
os.environ.setdefault(
    "DOC_HOSTING_SQLITE_PATH",
    os.path.join(tempfile.mkdtemp(prefix="doc-hosting-unit-"), "db.sqlite3"),
)

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from doc_hosting import db

db.setup()  # configure Django before test modules import the registry models

BUCKET = "test-bucket"
TOKEN = "global-token"
SECRET = "project-secret"
OTHER_SECRET = "other-project-secret"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Provide the S3 and publish-token environment the charm would inject."""
    monkeypatch.setenv("S3_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("S3_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("APP_PUBLISH_TOKEN", TOKEN)
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("S3_PATH", raising=False)
    monkeypatch.delenv("S3_REGION", raising=False)
    monkeypatch.delenv("APP_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("APP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("APP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("DOC_HOSTING_REDIRECT_CACHE_TTL", raising=False)
    monkeypatch.delenv("DOC_HOSTING_UPLOAD_URL_TTL", raising=False)


@pytest.fixture(scope="session", autouse=True)
def django_db():
    """Apply migrations once for the whole test session."""
    db.ensure_database_ready()
    yield


@pytest.fixture(autouse=True)
def clean_db(django_db):
    """Give every test an empty control plane and a fresh redirect cache."""
    from django.contrib.auth import get_user_model

    from doc_hosting.registry import models, services

    services.invalidate_redirect_cache()
    models.AuditEvent.objects.all().delete()
    models.PathMigration.objects.all().delete()
    models.LayoutChange.objects.all().delete()
    models.UploadSession.objects.all().delete()
    models.Publication.objects.all().delete()
    models.Redirect.objects.all().delete()
    models.Project.objects.all().delete()
    get_user_model().objects.all().delete()
    yield
    services.invalidate_redirect_cache()


@pytest.fixture()
def aws(env):
    """Start moto's S3 mock and create the test bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


@pytest.fixture()
def storage(aws):
    """Return an S3 storage client bound to the mocked bucket."""
    from doc_hosting.settings import get_settings
    from doc_hosting.storage import S3Storage

    return S3Storage(get_settings())


@pytest.fixture()
def client(aws):
    """Return a TestClient for a freshly created app."""
    from doc_hosting.server import create_app

    return TestClient(create_app())


def manifest_for(files: dict[str, bytes]) -> list[dict]:
    """Return the manifest entries (path, sha256, size) for ``files``."""
    return [
        {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for path, data in files.items()
    ]


def _upload_body(manifest, **overrides):
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "docs.example.com",
        "root_path": "docs",
        "project_secret": SECRET,
        "manifest": manifest,
    }
    body.update(overrides)
    return body


def begin_upload(
    client: TestClient,
    manifest=None,
    credential=TOKEN,
    project_secret=SECRET,
    **overrides,
):
    """POST a fully authenticated direct-upload begin request."""
    headers = {}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    body = _upload_body(
        manifest if manifest is not None else manifest_for({"index.html": b"<html>"}),
        **overrides,
    )
    if project_secret is None:
        body.pop("project_secret")
    else:
        body["project_secret"] = project_secret
    return client.post("/api/v1/uploads", json=body, headers=headers)


def finalize_upload(
    client: TestClient,
    upload_id,
    manifest=None,
    credential=TOKEN,
    project_secret=SECRET,
):
    """POST a fully authenticated direct-upload finalize request."""
    headers = {}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    body = {
        "project_secret": project_secret,
        "manifest": manifest
        if manifest is not None
        else manifest_for({"index.html": b"<html>"}),
    }
    if project_secret is None:
        body.pop("project_secret")
    return client.post(f"/api/v1/uploads/{upload_id}/finalize", json=body, headers=headers)


class ManifestStorage:
    """Storage double that verifies manifest objects without a real bucket.

    Every declared file reports its size and SHA-256 checksum from HEAD
    (like a storage that accepted the presigned PUT), so a begin/finalize
    pair verifies and registers a publication without creating bucket
    objects. Keys are matched by their manifest path suffix, preferring
    the longest path so ``index.html`` never answers for
    ``a/index.html``.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.presigned: list[str] = []

    def _match(self, key):
        for path in sorted(self.files, key=len, reverse=True):
            if key.endswith(f"/{path}"):
                return path
        return None

    def presign_put(self, key, expires_in, checksum_sha256_b64=None):
        self.presigned.append(key)
        return {"url": f"https://storage.test/{key}", "headers": {}}

    def head_object_info(self, key):
        path = self._match(key)
        if path is None:
            return None
        data = self.files[path]
        return {
            "size": len(data),
            "checksum_sha256": base64.b64encode(
                hashlib.sha256(data).digest()
            ).decode(),
        }

    def get_bytes(self, key):
        path = self._match(key)
        return self.files[path] if path is not None else None


def register_build(
    *,
    root_path="docs",
    language="en",
    version="latest",
    commit_hash="deadbeef",
    domain="docs.example.com",
    project_secret=SECRET,
    files=None,
):
    """Register a publication through the direct-upload service flow.

    Begins and finalizes an upload session against the manifest storage
    double, so the publication is verified and registered exactly as a
    real direct upload would be, without creating bucket objects. Tests
    that need served content upload it separately.
    """
    from doc_hosting.registry import services

    files = files if files is not None else {"index.html": b"<html>index</html>"}
    manifest = manifest_for(files)
    storage = ManifestStorage(files)
    begun = services.begin_upload(
        root_path=root_path,
        language=language,
        version=version,
        commit_hash=commit_hash,
        domain=domain,
        project_secret=project_secret,
        manifest=manifest,
        storage=storage,
        url_ttl=900,
    )
    return services.finalize_upload(
        begun["upload_id"],
        project_secret=project_secret,
        manifest=manifest,
        storage=storage,
    )


@pytest.fixture()
def project(aws):
    """A claimed ``docs`` project with one publication and one uploaded page."""
    from doc_hosting.registry import models

    register_build()
    from doc_hosting.settings import get_settings
    from doc_hosting.storage import S3Storage

    s3 = S3Storage(get_settings())
    s3.put_bytes("docs/en/latest/index.html", b"<html>index</html>")
    s3.put_bytes("docs/en/latest/usage/index.html", b"<html>usage</html>")
    return models.Project.objects.get(root_path="docs")
