"""Unit tests for the doc-hosting API layer (moto-mocked S3)."""

from __future__ import annotations

import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from doc_hosting.server import create_app

BUCKET = "test-bucket"
TOKEN = "test-token"


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


@pytest.fixture()
def aws(env):
    """Start moto's S3 mock and create the test bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        yield s3


@pytest.fixture()
def client(aws):
    """Return a TestClient for a freshly created app (lazy S3 storage)."""
    return TestClient(create_app())


def publish(client: TestClient, **overrides):
    """POST a build registration to the ingestion API."""
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "docs.example.com",
        "root_path": "docs",
    }
    body.update(overrides)
    return client.post(
        "/api/v1/publish", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_publish_requires_token(client):
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "docs.example.com",
        "root_path": "docs",
    }
    response = client.post("/api/v1/publish", json=body)
    assert response.status_code == 401

    response = client.post(
        "/api/v1/publish", json=body, headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 403


def test_publish_registers_build(client, aws):
    response = publish(client)
    assert response.status_code == 201
    entry = response.json()
    assert entry["root_path"] == "docs"
    assert entry["language"] == "en"
    assert entry["version"] == "latest"
    assert entry["commit_hash"] == "deadbeef"
    assert entry["domain"] == "docs.example.com"
    assert entry["registered_at"]

    registry = json.loads(
        aws.get_object(Bucket=BUCKET, Key="_registry/docs.json")["Body"].read()
    )
    assert registry["domain"] == "docs.example.com"
    assert len(registry["builds"]) == 1
    assert registry["builds"][0]["commit_hash"] == "deadbeef"

    # Re-publishing the same (language, version) upserts the entry.
    response = publish(client, commit_hash="cafebabe")
    assert response.status_code == 201
    assert response.json()["commit_hash"] == "cafebabe"
    registry = json.loads(
        aws.get_object(Bucket=BUCKET, Key="_registry/docs.json")["Body"].read()
    )
    assert len(registry["builds"]) == 1
    assert registry["builds"][0]["commit_hash"] == "cafebabe"


def test_publish_rejects_unsafe_paths(client):
    for field, value in [
        ("root_path", "../etc"),
        ("root_path", "a/b"),
        ("root_path", ""),
        ("root_path", ".."),
        ("language", ".."),
        ("language", "en/../../etc"),
        ("version", "1..0/../x"),
    ]:
        response = publish(client, **{field: value})
        assert response.status_code == 422, (field, value)

    # Missing required fields are rejected by the schema.
    response = client.post(
        "/api/v1/publish",
        json={"commit_hash": "deadbeef"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 422


def test_versions_api(client):
    response = client.get("/api/v1/versions", params={"root_path": "unknown"})
    assert response.status_code == 404

    publish(client, commit_hash="hash-en-latest")
    publish(client, language="fr", commit_hash="hash-fr-latest")
    publish(client, version="1.0", commit_hash="hash-en-1.0")

    response = client.get("/api/v1/versions", params={"root_path": "docs"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["root_path"] == "docs"
    assert payload["domain"] == "docs.example.com"
    versions = {v["version"]: v for v in payload["versions"]}
    assert set(versions) == {"latest", "1.0"}
    assert versions["latest"]["languages"] == ["en", "fr"]
    assert versions["latest"]["commit_hash"] == "hash-fr-latest"
    assert versions["1.0"]["languages"] == ["en"]
    assert versions["1.0"]["commit_hash"] == "hash-en-1.0"

    response = client.get(
        "/api/v1/versions", params={"root_path": "docs", "language": "fr"}
    )
    assert response.status_code == 200
    assert [v["version"] for v in response.json()["versions"]] == ["latest"]
    assert response.json()["versions"][0]["commit_hash"] == "hash-fr-latest"


def test_serving_from_s3(client, aws):
    # Nothing uploaded yet.
    response = client.get("/docs/en/latest/")
    assert response.status_code == 404

    aws.put_object(
        Bucket=BUCKET,
        Key="docs/en/latest/index.html",
        Body=b"<html><body>doc-hosting PoC</body></html>",
        ContentType="text/html",
    )
    aws.put_object(
        Bucket=BUCKET,
        Key="docs/en/latest/usage/index.html",
        Body=b"<html><body>usage page</body></html>",
    )
    aws.put_object(
        Bucket=BUCKET,
        Key="docs/en/latest/_static/style.css",
        Body=b"body { color: red; }",
    )

    # Index resolution (dirhtml layout: trailing slash -> index.html).
    response = client.get("/docs/en/latest/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert b"doc-hosting PoC" in response.content

    # Version root without trailing slash redirects (307) to the index.
    response = client.get("/docs/en/latest", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs/en/latest/"

    # Nested page and asset content types.
    response = client.get("/docs/en/latest/usage/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert b"usage page" in response.content

    response = client.get("/docs/en/latest/_static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert b"color: red" in response.content

    # Missing page -> 404; traversal attempts -> 404.
    assert client.get("/docs/en/latest/missing.html").status_code == 404
    assert client.get("/docs/en/latest/../../_registry/docs.json").status_code == 404
