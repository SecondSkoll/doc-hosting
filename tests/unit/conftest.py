"""Shared fixtures for the doc-hosting unit tests.

The control-plane database is configured before any Django import: the CI
workflow provides a PostgreSQL service through ``POSTGRESQL_DB_CONNECT_STRING``
(and the tests then run against it), while local runs fall back to a
throwaway SQLite file. The Django settings are placed in explicit test mode
(``DOC_HOSTING_DEV=1``) so the fail-closed SECRET_KEY/ALLOWED_HOSTS rules
permit the local fallbacks.
"""

from __future__ import annotations

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
    monkeypatch.delenv("DOC_HOSTING_REDIRECT_CACHE_TTL", raising=False)


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


def _publish_body(**overrides):
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "docs.example.com",
        "root_path": "docs",
        "project_secret": SECRET,
    }
    body.update(overrides)
    return body


def publish(client: TestClient, **overrides):
    """POST a fully authenticated build registration (both credentials)."""
    return client.post(
        "/api/v1/publish",
        json=_publish_body(**overrides),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def publish_as(client: TestClient, credential=TOKEN, project_secret=SECRET, **overrides):
    """POST a build registration with explicit credentials.

    ``credential`` is the deployment bearer token and ``project_secret``
    the per-root ownership proof; either may be set to ``None`` to omit it
    from the request.
    """
    headers = {}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    body = _publish_body(**overrides)
    if project_secret is None:
        body.pop("project_secret")
    else:
        body["project_secret"] = project_secret
    return client.post("/api/v1/publish", json=body, headers=headers)


@pytest.fixture()
def project(aws):
    """A claimed ``docs`` project with one publication and one uploaded page."""
    from doc_hosting.registry import models, services

    services.publish_build(
        root_path="docs",
        language="en",
        version="latest",
        commit_hash="deadbeef",
        domain="docs.example.com",
        project_secret=SECRET,
    )
    from doc_hosting.settings import get_settings
    from doc_hosting.storage import S3Storage

    s3 = S3Storage(get_settings())
    s3.put_bytes("docs/en/latest/index.html", b"<html>index</html>")
    s3.put_bytes("docs/en/latest/usage/index.html", b"<html>usage</html>")
    return models.Project.objects.get(root_path="docs")
