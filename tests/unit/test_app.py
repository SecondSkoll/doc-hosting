"""Unit tests for the application routes: health, versions and availability."""

from __future__ import annotations

import pytest
from conftest import SECRET, TOKEN, ManifestStorage, manifest_for, register_build

from doc_hosting.registry import models

pytestmark = pytest.mark.usefixtures("client")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_removed_publish_endpoint_returns_method_not_allowed(client):
    """The register-only publish endpoint is gone.

    A fully credentialed ``POST /api/v1/publish`` no longer reaches any
    ingestion handler: only the GET catch-all serving route matches the
    path, so the request fails with 405 and nothing is claimed.
    """
    response = client.post(
        "/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "docs.example.com",
            "root_path": "docs",
            "project_secret": SECRET,
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 405
    assert not models.Project.objects.exists()


def test_versions_api(client):
    response = client.get("/api/v1/versions", params={"root_path": "unknown"})
    assert response.status_code == 404

    register_build(commit_hash="hash-en-latest")
    register_build(language="fr", commit_hash="hash-fr-latest")
    register_build(version="1.0", commit_hash="hash-en-1.0")

    response = client.get("/api/v1/versions", params={"root_path": "DOCS/"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["root_path"] == "docs"
    assert payload["domain"] == "docs.example.com"
    assert payload["layout"] == {
        "language_enabled": True,
        "version_enabled": True,
        "language_label": "",
        "version_label": "",
    }
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

    # Unsafe root paths are rejected, unknown ones 404.
    assert client.get("/api/v1/versions", params={"root_path": ".."}).status_code == 422


def test_concurrent_claims_are_serialized(client):
    """Concurrent boundary claims cannot interleave check and insert.

    One thread claims ``project-1`` while another claims ``project-1/docs``
    with a foreign secret. Serialized boundary checks leave exactly one
    project: either the ancestor claim wins (the nested claim is rejected
    with 403 for its foreign secret) or the nested claim wins (the ancestor
    claim is rejected with 409 for shadowing). Unserialized checks could
    install the shadowing pair with the wrong ownership.
    """
    import threading

    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("the claim advisory lock is only exercised on PostgreSQL")
    from doc_hosting.registry import services

    outcomes: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def claim(root: str, secret: str) -> None:
        barrier.wait()
        try:
            services.begin_upload(
                root_path=root,
                language="en",
                version="latest",
                commit_hash="x",
                domain="d",
                project_secret=secret,
                manifest=manifest_for({"index.html": b"<html>"}),
                storage=ManifestStorage({"index.html": b"<html>"}),
                url_ttl=900,
            )
            outcomes.append(("created", root))
        except services.ServiceError as exc:
            outcomes.append((str(exc.status_code), root))

    threads = [
        threading.Thread(target=claim, args=("project-1", "secret-one")),
        threading.Thread(target=claim, args=("project-1/docs", "secret-two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for outcome, _ in outcomes) in (
        ["403", "created"],
        ["409", "created"],
    )
    # Exactly one project exists; no shadowing pair was installed.
    projects = list(models.Project.objects.values_list("root_path", flat=True))
    assert len(projects) == 1
    root = projects[0]
    if root == "project-1":
        assert models.Project.objects.get(root_path="project-1").check_secret(
            "secret-one"
        )
    else:
        assert root == "project-1/docs"
