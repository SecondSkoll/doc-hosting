"""Unit tests for the publish API: the two-gate credential model and ownership."""

from __future__ import annotations

import pytest
from conftest import SECRET, TOKEN, publish, publish_as
from fastapi.testclient import TestClient

from doc_hosting.registry import models

pytestmark = pytest.mark.usefixtures("client")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_publish_requires_well_formed_bearer_credentials(client):
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "docs.example.com",
        "root_path": "docs",
        "project_secret": SECRET,
    }
    assert client.post("/api/v1/publish", json=body).status_code == 401
    assert (
        client.post(
            "/api/v1/publish", json=body, headers={"Authorization": "Basic abc"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/publish", json=body, headers={"Authorization": "Bearer"}
        ).status_code
        == 401
    )
    # 401 wins over the body gates: the deployment gate is checked first.
    assert publish_as(client, credential=None, project_secret=None).status_code == 401


def test_wrong_bearer_with_correct_project_secret_is_rejected(client):
    response = publish_as(client, credential="wrong-token")
    assert response.status_code == 403
    assert "wrong-token" not in response.text
    assert not models.Project.objects.exists()


def test_valid_bearer_with_wrong_project_secret_is_rejected(client):
    publish(client)
    response = publish_as(client, project_secret="wrong-secret")
    assert response.status_code == 403
    assert "wrong-secret" not in response.text
    # The bearer token never doubles as a project secret.
    response = publish_as(client, project_secret=TOKEN)
    assert response.status_code == 403


def test_missing_or_empty_project_secret_is_rejected(client):
    # Missing key, empty value and whitespace all fail with 422, and the
    # error never echoes what was sent.
    response = publish_as(client, project_secret=None)
    assert response.status_code == 422
    response = publish_as(client, project_secret="")
    assert response.status_code == 422
    response = publish_as(client, project_secret="   ")
    assert response.status_code == 422
    assert "project-secret" not in response.text
    # Nothing was claimed.
    assert not models.Project.objects.exists()


def test_publish_rejected_when_global_token_unconfigured(monkeypatch, aws):
    from doc_hosting.server import create_app

    monkeypatch.delenv("APP_PUBLISH_TOKEN")
    bare_client = TestClient(create_app())
    response = publish(bare_client)
    assert response.status_code == 503
    assert not models.Project.objects.exists()


def test_first_publication_claims_the_root(client):
    response = publish(client, root_path="  Project-1//Docs ")
    assert response.status_code == 201
    entry = response.json()
    assert entry["root_path"] == "project-1/docs"
    assert entry["claimed"] is True
    assert entry["commit_hash"] == "deadbeef"
    assert entry["registered_at"]
    # No secret material (either credential) is ever echoed back.
    assert "project-secret" not in response.text
    assert "global-token" not in response.text

    project = models.Project.objects.get(root_path="project-1/docs")
    assert project.secret_claimed
    assert "project-secret" not in project.secret_hash
    assert project.secret_hash.startswith("pbkdf2_sha256$")
    assert project.check_secret("project-secret")
    assert not project.check_secret("wrong-secret")
    # The deployment bearer token is not stored as the project secret.
    assert not project.check_secret(TOKEN)
    assert project.domain == "docs.example.com"


def test_republish_requires_the_same_secret(client):
    assert publish(client).status_code == 201
    # The same secret may publish again.
    assert publish(client, commit_hash="cafebabe").status_code == 201
    # A different secret is rejected with 403.
    response = publish_as(client, project_secret="wrong-secret")
    assert response.status_code == 403
    assert "wrong-secret" not in response.text


def test_republish_upserts_one_row_and_audits_every_push(client):
    publish(client, commit_hash="hash-1")
    publish(client, commit_hash="hash-2")
    project = models.Project.objects.get(root_path="docs")
    publications = project.publications.all()
    assert publications.count() == 1
    assert publications.first().commit_hash == "hash-2"

    publish(client, language="fr", commit_hash="hash-fr")
    assert project.publications.count() == 2

    upserts = models.AuditEvent.objects.filter(event_type="publication.upserted")
    assert upserts.count() == 3
    claims = models.AuditEvent.objects.filter(event_type="project.claimed")
    assert claims.count() == 1
    for event in models.AuditEvent.objects.all():
        assert "project-secret" not in str(event.payload)
        assert "global-token" not in str(event.payload)


def test_matching_secret_may_claim_a_nested_root(client):
    publish(client, root_path="project-1")
    response = publish(client, root_path="project-1/docs")
    assert response.status_code == 201
    assert models.Project.objects.filter(root_path="project-1/docs").exists()


def test_foreign_ancestor_secret_is_rejected(client):
    publish(client, root_path="project-1")
    response = publish_as(client, project_secret="attacker-secret", root_path="project-1/evil")
    assert response.status_code == 403
    assert not models.Project.objects.filter(root_path="project-1/evil").exists()


def test_claiming_a_prefix_that_shadows_a_descendant_conflicts(client):
    publish(client, root_path="project-1/docs")
    response = publish_as(client, project_secret="some-other-secret", root_path="project-1")
    assert response.status_code == 409
    assert not models.Project.objects.filter(root_path="project-1").exists()


def test_project_1_and_project_10_are_unrelated(client):
    publish(client, root_path="project-1")
    response = publish_as(client, project_secret="other-secret", root_path="project-10")
    assert response.status_code == 201
    assert models.Project.objects.filter(root_path="project-10").exists()


def test_publish_rejects_unsafe_paths(client):
    for field, value in [
        ("root_path", "../etc"),
        ("root_path", ""),
        ("root_path", ".."),
        ("root_path", "docs/../etc"),
        ("root_path", "api"),
        ("root_path", "manage"),
        ("root_path", "health"),
        ("root_path", "_registry"),
        ("language", ".."),
        ("language", "en/../../etc"),
        ("version", "1..0/../x"),
    ]:
        response = publish(client, **{field: value})
        assert response.status_code == 422, (field, value)

    # Multi-segment roots are now valid (nested roots), but still validated.
    assert publish(client, root_path="docs/guides").status_code == 201

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


def test_publish_while_dimension_disabled_conflicts(client, storage):
    from doc_hosting.registry import services

    publish(client)
    project = models.Project.objects.get(root_path="docs")
    services.toggle_project_layout(
        project, language_enabled=False, version_enabled=True, storage=storage
    )
    assert publish(client, language="fr").status_code == 409
    assert publish(client, language="en").status_code == 201
    assert publish(client, version="1.0").status_code == 201


def test_domain_updates_on_publish(client):
    publish(client, domain="first.example.com")
    publish(client, domain="second.example.com")
    project = models.Project.objects.get(root_path="docs")
    assert project.domain == "second.example.com"


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
            services.publish_build(
                root_path=root,
                language="en",
                version="latest",
                commit_hash="x",
                domain="d",
                project_secret=secret,
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
