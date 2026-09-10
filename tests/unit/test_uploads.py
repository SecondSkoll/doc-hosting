"""Unit tests for the direct-upload API: begin -> presigned PUTs -> finalize."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse as urlparse
from datetime import datetime, timedelta

import pytest
from conftest import (
    SECRET,
    TOKEN,
    begin_upload,
    finalize_upload,
    manifest_for,
    register_build,
)
from django.utils import timezone

from doc_hosting.registry import models, services

pytestmark = pytest.mark.usefixtures("client")


def _query(url: str) -> dict[str, list[str]]:
    return urlparse.parse_qs(urlparse.urlparse(url).query)


def _upload_objects(storage, key_prefix: str, files: dict[str, bytes]) -> None:
    """Upload the manifest objects the way the presigned PUTs would."""
    for path, data in files.items():
        storage.put_bytes(f"{key_prefix}/{path}", data)


class TestBeginAuthentication:
    def test_begin_requires_well_formed_bearer_credentials(self, client):
        files = {"index.html": b"<html>"}
        for headers in (
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
        ):
            response = client.post(
                "/api/v1/uploads",
                json={
                    "commit_hash": "deadbeef",
                    "version": "latest",
                    "language": "en",
                    "domain": "docs.example.com",
                    "root_path": "docs",
                    "project_secret": SECRET,
                    "manifest": manifest_for(files),
                },
                headers=headers,
            )
            assert response.status_code == 401
        # 401 wins over the body gates: the deployment gate is checked first.
        assert (
            begin_upload(client, credential=None, project_secret=None).status_code
            == 401
        )

    def test_begin_wrong_bearer_is_rejected(self, client):
        response = begin_upload(client, credential="wrong-token")
        assert response.status_code == 403
        assert "wrong-token" not in response.text
        assert not models.Project.objects.exists()
        assert not models.UploadSession.objects.exists()

    def test_begin_rejected_when_global_token_unconfigured(self, monkeypatch):
        from fastapi.testclient import TestClient

        from doc_hosting.server import create_app

        monkeypatch.delenv("APP_PUBLISH_TOKEN")
        bare_client = TestClient(create_app())
        response = begin_upload(bare_client)
        assert response.status_code == 503
        assert not models.UploadSession.objects.exists()

    def test_begin_missing_or_empty_project_secret_is_rejected(self, client):
        for secret in (None, "", "   "):
            response = begin_upload(client, project_secret=secret)
            assert response.status_code == 422
            assert "project-secret" not in response.text
        assert not models.Project.objects.exists()

    def test_begin_wrong_project_secret_is_rejected(self, client):
        assert begin_upload(client).status_code == 201
        response = begin_upload(client, project_secret="wrong-secret")
        assert response.status_code == 403
        assert "wrong-secret" not in response.text
        # The deployment bearer token never doubles as a project secret.
        assert begin_upload(client, project_secret=TOKEN).status_code == 403


class TestBeginManifestValidation:
    def _post_manifest(self, client, manifest):
        return begin_upload(client, manifest)

    def test_missing_empty_or_malformed_manifest_is_rejected(self, client):
        for manifest in (None, [], {}, "index.html", [None], ["index.html"]):
            body = {
                "commit_hash": "deadbeef",
                "version": "latest",
                "language": "en",
                "domain": "docs.example.com",
                "root_path": "docs",
                "project_secret": SECRET,
            }
            if manifest is not None:
                body["manifest"] = manifest
            response = client.post(
                "/api/v1/uploads",
                json=body,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert response.status_code == 422, manifest
        assert not models.Project.objects.exists()

    def test_incomplete_entries_are_rejected(self, client):
        good = {"path": "index.html", "sha256": "a" * 64, "size": 1}
        for remove in ("path", "sha256", "size"):
            entry = {key: value for key, value in good.items() if key != remove}
            response = self._post_manifest(client, [entry])
            assert response.status_code == 422, remove

    def test_unsafe_paths_are_rejected(self, client):
        for path in (
            "../escape.html",
            "a/../../escape.html",
            "/absolute.html",
            "a//b.html",
            "a/./b.html",
            "a/../b.html",
            "back\\slash.html",
            "ctrl\x01.html",
            "",
        ):
            entry = {"path": path, "sha256": "a" * 64, "size": 1}
            response = self._post_manifest(client, [entry])
            assert response.status_code == 422, path
        assert not models.UploadSession.objects.exists()

    def test_invalid_checksums_are_rejected(self, client):
        for sha256 in (
            "A" * 64,  # uppercase
            "z" * 64,  # not hex
            "a" * 63,  # too short
            "a" * 65,  # too long
            "",
            42,
        ):
            entry = {"path": "index.html", "sha256": sha256, "size": 1}
            response = self._post_manifest(client, [entry])
            assert response.status_code == 422, sha256

    def test_invalid_sizes_are_rejected(self, client):
        for size in (-1, "10", 1.5, None):
            entry = {"path": "index.html", "sha256": "a" * 64, "size": size}
            response = self._post_manifest(client, [entry])
            assert response.status_code == 422, size

    def test_duplicate_paths_are_rejected(self, client):
        entry = {"path": "index.html", "sha256": "a" * 64, "size": 1}
        response = self._post_manifest(client, [entry, dict(entry)])
        assert response.status_code == 422

    def test_unsafe_root_language_or_version_is_rejected(self, client):
        files = {"index.html": b"<html>"}
        for field, value in [
            ("root_path", "../etc"),
            ("root_path", "api"),
            ("language", "en/../../etc"),
            ("version", ".."),
        ]:
            response = begin_upload(client, manifest_for(files), **{field: value})
            assert response.status_code == 422, (field, value)

    def test_manifest_is_normalized_to_sorted_unique_entries(self, client, storage):
        files = {"b.html": b"b-data", "a/index.html": b"a-data"}
        response = begin_upload(client, manifest_for(files))
        assert response.status_code == 201
        session = models.UploadSession.objects.get(
            pk=response.json()["upload_id"]
        )
        assert [entry["path"] for entry in session.manifest] == [
            "a/index.html",
            "b.html",
        ]
        for entry in session.manifest:
            assert set(entry) == {"path", "sha256", "size"}
            assert entry["sha256"] == hashlib.sha256(files[entry["path"]]).hexdigest()
            assert entry["size"] == len(files[entry["path"]])


class TestBeginAuthorizationAndClaim:
    def test_first_begin_claims_the_root_without_registering(self, client):
        files = {"index.html": b"<html>"}
        response = begin_upload(client, manifest_for(files), root_path="  Project-1//Docs ")
        assert response.status_code == 201
        payload = response.json()
        assert payload["root_path"] == "project-1/docs"

        project = models.Project.objects.get(root_path="project-1/docs")
        assert project.secret_claimed
        # Only a salted hash of the project secret is stored.
        assert project.secret_hash.startswith("pbkdf2_sha256$")
        assert "project-secret" not in project.secret_hash
        assert project.check_secret(SECRET)
        assert not project.check_secret("wrong-secret")

        # The upload session exists (pending) but nothing is published yet.
        session = models.UploadSession.objects.get(pk=payload["upload_id"])
        assert session.project == project
        assert session.status == models.UploadSession.STATUS_PENDING
        assert session.expires_at > timezone.now()
        assert not models.Publication.objects.exists()
        assert models.AuditEvent.objects.filter(
            event_type="project.claimed"
        ).exists()
        assert models.AuditEvent.objects.filter(
            event_type="upload.begun"
        ).exists()

    def test_matching_secret_may_begin_a_nested_root(self, client):
        assert begin_upload(client, root_path="project-1").status_code == 201
        response = begin_upload(client, root_path="project-1/docs")
        assert response.status_code == 201
        assert models.Project.objects.filter(root_path="project-1/docs").exists()

    def test_foreign_ancestor_secret_is_rejected(self, client):
        assert begin_upload(client, root_path="project-1").status_code == 201
        response = begin_upload(
            client, project_secret="attacker-secret", root_path="project-1/evil"
        )
        assert response.status_code == 403
        assert not models.Project.objects.filter(root_path="project-1/evil").exists()

    def test_claiming_a_prefix_that_shadows_a_descendant_conflicts(self, client):
        assert begin_upload(client, root_path="project-1/docs").status_code == 201
        response = begin_upload(
            client, project_secret="some-other-secret", root_path="project-1"
        )
        assert response.status_code == 409
        assert not models.Project.objects.filter(root_path="project-1").exists()

    def test_unrelated_segment_prefix_roots_do_not_shadow(self, client):
        # Ownership works on segment boundaries: ``project-1`` neither owns
        # nor shadows ``project-10``.
        assert begin_upload(client, root_path="project-1").status_code == 201
        response = begin_upload(
            client, project_secret="other-secret", root_path="project-10"
        )
        assert response.status_code == 201
        assert models.Project.objects.filter(root_path="project-10").exists()

    def test_begin_while_dimension_disabled_conflicts(self, client, storage):
        files = {"index.html": b"<html>"}
        register_build()
        project = models.Project.objects.get(root_path="docs")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        assert (
            begin_upload(client, manifest_for(files), language="fr").status_code == 409
        )
        assert (
            begin_upload(client, manifest_for(files), language="en").status_code == 201
        )


class TestPresignedUploads:
    def test_one_exact_key_url_per_declared_file(self, client):
        files = {
            "index.html": b"<html>index</html>",
            "usage/index.html": b"<html>usage</html>",
        }
        response = begin_upload(client, manifest_for(files))
        assert response.status_code == 201
        payload = response.json()

        assert payload["key_prefix"] == "docs/en/latest"
        assert sorted(upload["path"] for upload in payload["uploads"]) == sorted(files)
        for upload in payload["uploads"]:
            key = f"{payload['key_prefix']}/{upload['path']}"
            # The URL is restricted to exactly this object's key.
            assert urlparse.unquote(urlparse.urlparse(upload["url"]).path).endswith(key)
            # Short-lived: the expiry is bounded and announced.
            assert _query(upload["url"])["X-Amz-Expires"] == ["900"]
            assert payload["url_ttl"] == 900
        # No secrets are echoed.
        assert "project-secret" not in response.text
        assert "global-token" not in response.text

    def test_urls_expire_with_the_configured_ttl(self, client, monkeypatch):
        monkeypatch.setenv("DOC_HOSTING_UPLOAD_URL_TTL", "60")
        files = {"index.html": b"<html>"}
        response = begin_upload(client, manifest_for(files))
        assert response.status_code == 201
        payload = response.json()
        assert payload["url_ttl"] == 60
        for upload in payload["uploads"]:
            assert _query(upload["url"])["X-Amz-Expires"] == ["60"]
        expires_at = datetime.fromisoformat(payload["expires_at"])
        remaining = (expires_at - timezone.now()).total_seconds()
        assert 0 < remaining <= 60

    def test_invalid_ttl_values_fall_back_to_the_default(self, client, monkeypatch):
        files = {"index.html": b"<html>"}
        for raw in ("abc", "0", "-5", ""):
            monkeypatch.setenv("DOC_HOSTING_UPLOAD_URL_TTL", raw)
            response = begin_upload(client, manifest_for(files))
            assert response.status_code == 201, raw
            payload = response.json()
            assert payload["url_ttl"] == 900
            for upload in payload["uploads"]:
                assert _query(upload["url"])["X-Amz-Expires"] == ["900"]

    def test_urls_carry_the_manifest_checksum_as_a_signed_header(self, client):
        files = {"index.html": b"<html>index</html>"}
        response = begin_upload(client, manifest_for(files))
        upload = response.json()["uploads"][0]
        digest = base64.b64encode(
            hashlib.sha256(files["index.html"]).digest()
        ).decode()
        assert upload["headers"]["x-amz-checksum-sha256"] == digest
        assert _query(upload["url"])["X-Amz-SignedHeaders"] == [
            "host;x-amz-checksum-sha256"
        ]

    def test_urls_apply_the_configured_s3_path_prefix(self, client, monkeypatch):
        monkeypatch.setenv("S3_PATH", "prefix")
        files = {"index.html": b"<html>"}
        response = begin_upload(client, manifest_for(files))
        assert response.status_code == 201
        upload = response.json()["uploads"][0]
        assert urlparse.unquote(urlparse.urlparse(upload["url"]).path).endswith(
            "prefix/docs/en/latest/index.html"
        )

    def test_key_prefix_respects_disabled_dimensions(self, client, storage):
        register_build()
        project = models.Project.objects.get(root_path="docs")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        files = {"index.html": b"<html>"}
        response = begin_upload(client, manifest_for(files))
        assert response.status_code == 201
        payload = response.json()
        assert payload["key_prefix"] == "docs/en"
        assert urlparse.urlparse(payload["uploads"][0]["url"]).path.endswith(
            "/docs/en/index.html"
        )


class TestFinalizeAuthentication:
    def _begun(self, client):
        response = begin_upload(client)
        assert response.status_code == 201
        return response.json()

    def test_finalize_requires_the_bearer_token(self, client):
        payload = self._begun(client)
        response = finalize_upload(client, payload["upload_id"], credential=None)
        assert response.status_code == 401

    def test_finalize_wrong_bearer_is_rejected(self, client):
        payload = self._begun(client)
        response = finalize_upload(client, payload["upload_id"], credential="wrong")
        assert response.status_code == 403

    def test_finalize_wrong_project_secret_is_rejected(self, client, storage):
        payload = self._begun(client)
        _upload_objects(storage, payload["key_prefix"], {"index.html": b"<html>"})
        response = finalize_upload(
            client, payload["upload_id"], project_secret="wrong-secret"
        )
        assert response.status_code == 403
        assert "wrong-secret" not in response.text
        session = models.UploadSession.objects.get(pk=payload["upload_id"])
        assert session.status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

    def test_finalize_missing_project_secret_is_rejected(self, client):
        payload = self._begun(client)
        response = finalize_upload(client, payload["upload_id"], project_secret=None)
        assert response.status_code == 422


class TestFinalizeHappyPath:
    def test_finalize_registers_and_completes_atomically(self, client, storage):
        files = {
            "index.html": b"<html>index</html>",
            "usage/index.html": b"<html>usage</html>",
        }
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)

        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 201
        payload = response.json()
        assert payload["upload_id"] == begun["upload_id"]
        assert payload["root_path"] == "docs"
        assert payload["language"] == "en"
        assert payload["version"] == "latest"
        assert payload["commit_hash"] == "deadbeef"
        assert payload["registered_at"]
        assert payload["replay"] is False
        assert "project-secret" not in response.text
        assert "global-token" not in response.text

        project = models.Project.objects.get(root_path="docs")
        publication = project.publications.get()
        assert publication.commit_hash == "deadbeef"
        assert publication.language == "en"
        assert publication.version == "latest"

        session = models.UploadSession.objects.get(pk=begun["upload_id"])
        assert session.status == models.UploadSession.STATUS_COMPLETED
        assert session.completed_at is not None

        assert models.AuditEvent.objects.filter(
            event_type="publication.upserted"
        ).exists()
        assert models.AuditEvent.objects.filter(
            event_type="upload.completed"
        ).exists()
        for event in models.AuditEvent.objects.all():
            assert "project-secret" not in str(event.payload)
            assert "global-token" not in str(event.payload)

        # The published build is served straight from storage.
        assert client.get("/docs/en/latest/").status_code == 200
        assert client.get("/docs/en/latest/usage/").status_code == 200

    def test_finalize_upserts_an_existing_publication(self, client, storage):
        register_build(commit_hash="old-hash")
        files = {"index.html": b"<html>"}
        begun = begin_upload(
            client, manifest_for(files), commit_hash="new-hash"
        ).json()
        _upload_objects(storage, begun["key_prefix"], files)
        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 201
        publication = models.Publication.objects.get()
        assert publication.commit_hash == "new-hash"

    def test_every_push_upserts_one_row_and_audits_every_push(
        self, client, storage
    ):
        files = {"index.html": b"<html>"}

        def push(commit_hash, **overrides):
            begun = begin_upload(
                client, manifest_for(files), commit_hash=commit_hash, **overrides
            ).json()
            _upload_objects(storage, begun["key_prefix"], files)
            response = finalize_upload(client, begun["upload_id"], manifest_for(files))
            assert response.status_code == 201

        push("hash-1")
        push("hash-2")
        project = models.Project.objects.get(root_path="docs")
        publications = project.publications.all()
        assert publications.count() == 1
        assert publications.first().commit_hash == "hash-2"

        push("hash-fr", language="fr")
        assert project.publications.count() == 2

        upserts = models.AuditEvent.objects.filter(event_type="publication.upserted")
        assert upserts.count() == 3
        claims = models.AuditEvent.objects.filter(event_type="project.claimed")
        assert claims.count() == 1
        for event in models.AuditEvent.objects.all():
            assert "project-secret" not in str(event.payload)
            assert "global-token" not in str(event.payload)

    def test_finalize_applies_the_session_domain(self, client, storage):
        files = {"index.html": b"<html>"}
        begun = begin_upload(
            client, manifest_for(files), domain="new.example.com"
        ).json()
        _upload_objects(storage, begun["key_prefix"], files)
        assert finalize_upload(
            client, begun["upload_id"], manifest_for(files)
        ).status_code == 201
        project = models.Project.objects.get(root_path="docs")
        assert project.domain == "new.example.com"


class TestFinalizeFailures:
    def test_unknown_upload_session_is_a_404(self, client):
        response = finalize_upload(client, 424242)
        assert response.status_code == 404

    def test_missing_object_leaves_the_session_pending_and_retryable(
        self, client, storage
    ):
        files = {"a.html": b"a", "b.html": b"b"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], {"a.html": b"a"})  # b.html missing

        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 409
        assert "b.html" in response.json()["detail"]
        session = models.UploadSession.objects.get(pk=begun["upload_id"])
        assert session.status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

        # The session stays retryable: completing the uploads registers.
        storage.put_bytes(f"{begun['key_prefix']}/b.html", b"b")
        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 201
        assert models.Publication.objects.count() == 1

    def test_size_mismatch_is_rejected(self, client, storage):
        files = {"index.html": b"<html>index</html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], {"index.html": b"short"})
        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 409
        assert "size mismatch" in response.json()["detail"]
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

    def test_checksum_mismatch_is_rejected(self, client, storage):
        files = {"index.html": b"<html>index</html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        # Same size, different content: only the SHA-256 can catch it.
        _upload_objects(
            storage, begun["key_prefix"], {"index.html": b"<html>indey</html>"}
        )
        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 409
        assert "sha256 mismatch" in response.json()["detail"]
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

    def test_expired_session_is_rejected(self, client, storage):
        files = {"index.html": b"<html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)
        models.UploadSession.objects.filter(pk=begun["upload_id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 409
        assert "expired" in response.json()["detail"]
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

    def test_manifest_drift_is_rejected(self, client, storage):
        files = {"index.html": b"<html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)

        other_files = {"index.html": b"<html>", "extra.html": b"extra"}
        response = finalize_upload(
            client, begun["upload_id"], manifest_for(other_files)
        )
        assert response.status_code == 409
        assert "manifest" in response.json()["detail"]
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING

        tampered = [{"path": "index.html", "sha256": "a" * 64, "size": 6}]
        response = finalize_upload(client, begun["upload_id"], tampered)
        assert response.status_code == 409
        assert not models.Publication.objects.exists()

    def test_dimension_change_between_begin_and_finalize_is_rejected(
        self, client, storage
    ):
        files = {"index.html": b"<html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)
        project = models.Project.objects.get(root_path="docs")
        project.version_enabled = False
        project.version_label = "stable"
        project.save()

        response = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert response.status_code == 409
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()


class TestFinalizeReplay:
    def test_identical_replay_is_idempotent(self, client, storage):
        files = {"index.html": b"<html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)
        first = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert first.status_code == 201

        session = models.UploadSession.objects.get(pk=begun["upload_id"])
        completed_at = session.completed_at

        replay = finalize_upload(client, begun["upload_id"], manifest_for(files))
        assert replay.status_code == 200
        payload = replay.json()
        assert payload["replay"] is True
        assert payload["registered_at"] == first.json()["registered_at"]

        # Nothing was duplicated or re-registered.
        assert models.Publication.objects.count() == 1
        session.refresh_from_db()
        assert session.completed_at == completed_at
        assert models.AuditEvent.objects.filter(
            event_type="upload.completed"
        ).count() == 1

    def test_differing_replay_conflicts(self, client, storage):
        files = {"index.html": b"<html>"}
        begun = begin_upload(client, manifest_for(files)).json()
        _upload_objects(storage, begun["key_prefix"], files)
        assert (
            finalize_upload(client, begun["upload_id"], manifest_for(files)).status_code
            == 201
        )
        other_files = {"index.html": b"<html>", "extra.html": b"extra"}
        response = finalize_upload(
            client, begun["upload_id"], manifest_for(other_files)
        )
        assert response.status_code == 409


class TestStoredChecksumVerification:
    """The HEAD-reported stored checksum is authoritative at finalize."""

    def _begin(self, files: dict[str, bytes], fake) -> dict:
        return services.begin_upload(
            root_path="docs",
            language="en",
            version="latest",
            commit_hash="deadbeef",
            domain="docs.example.com",
            project_secret=SECRET,
            manifest=manifest_for(files),
            storage=fake,
            url_ttl=900,
        )

    def test_finalize_uses_the_stored_checksum_without_reading_the_body(self):
        files = {"index.html": b"<html>index</html>"}
        key = "docs/en/latest/index.html"
        stored = base64.b64encode(hashlib.sha256(files["index.html"]).digest()).decode()
        fake = FakeStorage({key: files["index.html"]}, {key: stored})
        begun = self._begin(files, fake)

        result = services.finalize_upload(
            begun["upload_id"],
            project_secret=SECRET,
            manifest=manifest_for(files),
            storage=fake,
        )
        assert result["replay"] is False
        # The stored checksum satisfied verification: no body was read.
        assert fake.reads == []
        assert models.Publication.objects.filter(
            project__root_path="docs", language="en", version="latest"
        ).exists()
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_COMPLETED

    def test_finalize_rejects_a_stored_checksum_mismatch(self):
        files = {"index.html": b"<html>index</html>"}
        key = "docs/en/latest/index.html"
        wrong = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
        fake = FakeStorage({key: files["index.html"]}, {key: wrong})
        begun = self._begin(files, fake)

        with pytest.raises(services.ServiceError) as excinfo:
            services.finalize_upload(
                begun["upload_id"],
                project_secret=SECRET,
                manifest=manifest_for(files),
                storage=fake,
            )
        assert excinfo.value.status_code == 409
        assert "sha256 mismatch" in excinfo.value.detail
        assert models.UploadSession.objects.get(
            pk=begun["upload_id"]
        ).status == models.UploadSession.STATUS_PENDING
        assert not models.Publication.objects.exists()

    def test_begin_presigns_exact_keys_with_ttl_and_checksums(self):
        files = {"index.html": b"<html>index</html>", "a/b.html": b"b"}
        fake = FakeStorage({}, {})
        begun = self._begin(files, fake)
        assert fake.presigned == [
            (
                "docs/en/latest/a/b.html",
                900,
                base64.b64encode(hashlib.sha256(b"b").digest()).decode(),
            ),
            (
                "docs/en/latest/index.html",
                900,
                base64.b64encode(
                    hashlib.sha256(files["index.html"]).digest()
                ).decode(),
            ),
        ]
        assert begun["url_ttl"] == 900


class FakeStorage:
    """Storage double reporting a stored checksum from HEAD (like real S3)."""

    def __init__(self, objects: dict[str, bytes], stored_checksums: dict[str, str]):
        self.objects = objects
        self.stored_checksums = stored_checksums
        self.presigned: list[tuple[str, int, str | None]] = []
        self.reads: list[str] = []

    def presign_put(self, key, expires_in, checksum_sha256_b64=None):
        self.presigned.append((key, expires_in, checksum_sha256_b64))
        return {"url": f"https://storage.test/{key}", "headers": {}}

    def head_object_info(self, key):
        if key not in self.objects:
            return None
        return {
            "size": len(self.objects[key]),
            "checksum_sha256": self.stored_checksums.get(key),
        }

    def get_bytes(self, key):
        self.reads.append(key)
        return self.objects.get(key)
