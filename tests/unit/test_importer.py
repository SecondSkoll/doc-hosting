"""Unit tests for the idempotent legacy-registry importer."""

from __future__ import annotations

import json

import pytest
from conftest import TOKEN

from doc_hosting.registry import importer, models, services

pytestmark = pytest.mark.usefixtures("aws")


def put_registry(aws, root_path, registry):
    aws.put_object(
        Bucket="test-bucket",
        Key=f"_registry/{root_path}.json",
        Body=json.dumps(registry).encode("utf-8"),
        ContentType="application/json",
    )


def legacy_registry(domain, builds):
    return {"root_path": "unused", "domain": domain, "builds": builds}


def test_import_is_idempotent(aws):
    put_registry(
        aws,
        "docs",
        legacy_registry(
            "docs.example.com",
            [
                {
                    "language": "en",
                    "version": "latest",
                    "commit_hash": "deadbeef",
                    "registered_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "language": "fr",
                    "version": "latest",
                    "commit_hash": "cafebabe",
                    "registered_at": "2026-01-02T00:00:00+00:00",
                },
            ],
        ),
    )
    put_registry(
        aws,
        "guides",
        legacy_registry("guides.example.com", []),
    )

    summary = importer.import_legacy_registry(_storage_from_env())
    assert summary["projects"] == 2
    assert summary["publications"] == 2
    assert summary["skipped_invalid_records"] == []

    # Re-running changes nothing.
    summary = importer.import_legacy_registry(_storage_from_env())
    assert summary["projects"] == 2
    assert summary["publications"] == 0
    assert summary["skipped_invalid_records"] == []
    assert models.Publication.objects.count() == 2
    assert models.Project.objects.count() == 2

    project = models.Project.objects.get(root_path="docs")
    assert project.domain == "docs.example.com"
    assert not project.secret_claimed
    versions = services.grouped_versions(project)
    assert versions[0]["languages"] == ["en", "fr"]
    assert versions[0]["commit_hash"] == "cafebabe"
    assert models.AuditEvent.objects.filter(event_type="registry.imported").count() == 2


def test_import_never_writes_registry_json(aws):
    put_registry(aws, "docs", legacy_registry("d", []))
    before = {
        obj["Key"]
        for obj in aws.list_objects_v2(Bucket="test-bucket")["Contents"]
    }
    importer.import_legacy_registry(_storage_from_env())
    after = {
        obj["Key"]
        for obj in aws.list_objects_v2(Bucket="test-bucket")["Contents"]
    }
    assert before == after


def test_import_skips_unsafe_roots_and_bad_timestamps(aws):
    put_registry(
        aws,
        "api",
        legacy_registry("d", [{"language": "en", "version": "latest", "commit_hash": "x"}]),
    )
    put_registry(
        aws,
        "weird",
        legacy_registry(
            "d",
            [
                {"language": "", "version": "latest", "commit_hash": "x"},
                {
                    "language": "en",
                    "version": "1.0",
                    "commit_hash": "y",
                    "registered_at": "not-a-date",
                },
            ],
        ),
    )
    summary = importer.import_legacy_registry(_storage_from_env())
    assert summary["projects"] == 1
    assert summary["publications"] == 1
    skipped = summary["skipped_invalid_records"]
    assert len(skipped) == 2
    assert {"root_path": "api", "reason": "unsafe root path"} in [
        {key: record[key] for key in ("root_path", "reason")} for record in skipped
    ]
    reasons = {record["reason"] for record in skipped}
    assert "unsafe root path" in reasons
    assert "missing language or version" in reasons
    assert not models.Project.objects.filter(root_path="api").exists()
    publication = models.Publication.objects.get()
    assert publication.language == "en"
    assert publication.version == "1.0"
    assert publication.registered_at is not None


def test_import_reports_invalid_language_and_version_labels(aws):
    put_registry(
        aws,
        "docs",
        legacy_registry(
            "d",
            [
                {"language": "en/../etc", "version": "latest", "commit_hash": "x"},
                {"language": "en", "version": "1.0/../x", "commit_hash": "y"},
                {"language": "x" * 300, "version": "latest", "commit_hash": "z"},
                {"language": "ok", "version": "fine", "commit_hash": "w"},
                "not-a-dict",
            ],
        ),
    )
    summary = importer.import_legacy_registry(_storage_from_env())
    assert summary["projects"] == 1
    assert summary["publications"] == 1
    skipped = summary["skipped_invalid_records"]
    assert len(skipped) == 4
    reasons = {record["reason"] for record in skipped}
    assert reasons == {
        "invalid language label",
        "invalid version label",
        "invalid record",
    }
    # The invalid labels never enter the control plane.
    assert set(
        models.Publication.objects.values_list("language", flat=True)
    ) == {"ok"}
    assert set(models.Publication.objects.values_list("version", flat=True)) == {"fine"}


def test_imported_project_adopts_the_first_secret(client, aws):
    put_registry(aws, "docs", legacy_registry("d", []))
    importer.import_legacy_registry(_storage_from_env())

    # The imported project has no secret yet: the first fully authenticated
    # publication (valid bearer token + project secret) claims one.
    response = client.post(
        "/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "docs.example.com",
            "root_path": "docs",
            "project_secret": "my-secret",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 201
    assert response.json()["claimed"] is True
    project = models.Project.objects.get(root_path="docs")
    assert project.check_secret("my-secret")

    # The adoption happens only after the deployment gate passed: a wrong
    # bearer cannot claim, even with a valid-looking secret.
    response = client.post(
        "/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "docs.example.com",
            "root_path": "docs",
            "project_secret": "other",
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 403

    # A different secret is now rejected.
    response = client.post(
        "/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "docs.example.com",
            "root_path": "docs",
            "project_secret": "other-secret",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 403


def _storage_from_env():
    from doc_hosting.settings import get_settings
    from doc_hosting.storage import S3Storage

    return S3Storage(get_settings())
