"""Unit tests for the publish script: API direct upload without S3 credentials."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from scripts import publish as publish_script

API_TOKEN = "the-api-token"
PROJECT_SECRET = "super-secret"


def _build_dir(tmp_path, files: dict[str, bytes] | None = None):
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    for name, data in (files or {"index.html": b"<html>index</html>"}).items():
        target = build_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return build_dir


def _env_file(tmp_path, extra: dict[str, str] | None = None):
    env_file = tmp_path / "env"
    env_file.write_text(
        "\n".join(
            [
                "API_URL=http://api",
                f"API_TOKEN={API_TOKEN}",
                f"PROJECT_SECRET={PROJECT_SECRET}",
                *(f"{key}={value}" for key, value in (extra or {}).items()),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_file


def _begin_response(manifest, upload_id=7):
    return {
        "upload_id": upload_id,
        "root_path": "docs",
        "key_prefix": "docs/en/latest",
        "expires_at": "2026-09-08T10:00:00+00:00",
        "url_ttl": 900,
        "uploads": [
            {
                "path": entry["path"],
                "url": f"https://storage.test/presigned/{upload_id}/{entry['path']}",
                "headers": {"x-amz-checksum-sha256": "checksum-" + entry["path"]},
            }
            for entry in manifest
        ],
    }


class TestBuildManifest:
    def test_lists_files_sorted_with_checksums_and_sizes(self, tmp_path):
        build_dir = _build_dir(
            tmp_path,
            {
                "usage/index.html": b"usage",
                "index.html": b"index",
                "_static/a.css": b"css",
            },
        )
        manifest = publish_script.build_manifest(build_dir)
        assert [entry["path"] for entry in manifest] == [
            "_static/a.css",
            "index.html",
            "usage/index.html",
        ]
        for entry in manifest:
            data = (build_dir / entry["path"]).read_bytes()
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()
            assert entry["size"] == len(data)
        assert set(manifest[0]) == {"path", "sha256", "size"}


class TestBeginUpload:
    def test_sends_bearer_secret_and_manifest(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return httpx.Response(
                201, json=_begin_response(json["manifest"]), request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        manifest = [
            {"path": "index.html", "sha256": "a" * 64, "size": 6},
        ]
        begun = publish_script.begin_upload(
            "http://api",
            API_TOKEN,
            PROJECT_SECRET,
            commit_hash="c",
            version="latest",
            language="en",
            domain="d",
            root_path="docs",
            manifest=manifest,
        )
        assert captured["url"] == "http://api/api/v1/uploads"
        assert captured["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
        assert captured["json"]["project_secret"] == PROJECT_SECRET
        assert captured["json"]["manifest"] == manifest
        assert captured["json"]["root_path"] == "docs"
        assert begun["upload_id"] == 7

    def test_connection_error_never_leaks_either_credential(self, monkeypatch):
        def fake_post(url, json=None, headers=None, timeout=None):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.begin_upload(
                "http://api",
                API_TOKEN,
                PROJECT_SECRET,
                commit_hash="c",
                version="latest",
                language="en",
                domain="d",
                root_path="docs",
                manifest=[{"path": "index.html", "sha256": "a" * 64, "size": 1}],
            )
        assert PROJECT_SECRET not in str(excinfo.value)
        assert API_TOKEN not in str(excinfo.value)

    def test_error_response_never_leaks_either_credential(self, monkeypatch):
        def fake_post(url, json=None, headers=None, timeout=None):
            return httpx.Response(
                403, text="invalid publish token", request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.begin_upload(
                "http://api",
                API_TOKEN,
                PROJECT_SECRET,
                commit_hash="c",
                version="latest",
                language="en",
                domain="d",
                root_path="docs",
                manifest=[{"path": "index.html", "sha256": "a" * 64, "size": 1}],
            )
        assert PROJECT_SECRET not in str(excinfo.value)
        assert API_TOKEN not in str(excinfo.value)


class TestUploadFiles:
    def test_puts_each_file_to_its_url_with_the_returned_headers(self, tmp_path):
        build_dir = _build_dir(tmp_path, {"index.html": b"index", "a/b.html": b"b"})
        uploads = _begin_response(
            [
                {"path": "index.html", "sha256": "a" * 64, "size": 5},
                {"path": "a/b.html", "sha256": "b" * 64, "size": 1},
            ]
        )["uploads"]
        puts = []

        def fake_put(url, content=None, headers=None, timeout=None):
            puts.append((url, content, headers))
            return httpx.Response(200, request=httpx.Request("PUT", url))

        original_put = publish_script.httpx.put
        publish_script.httpx.put = fake_put
        try:
            count = publish_script.upload_files(build_dir, uploads)
        finally:
            publish_script.httpx.put = original_put
        assert count == 2
        assert [put[0] for put in puts] == [upload["url"] for upload in uploads]
        assert [put[1] for put in puts] == [b"index", b"b"]
        assert [put[2] for put in puts] == [
            {"x-amz-checksum-sha256": "checksum-index.html"},
            {"x-amz-checksum-sha256": "checksum-a/b.html"},
        ]

    def test_failed_upload_reports_the_path_without_the_url(self, tmp_path):
        build_dir = _build_dir(tmp_path, {"index.html": b"index"})
        uploads = _begin_response(
            [{"path": "index.html", "sha256": "a" * 64, "size": 5}]
        )["uploads"]

        def fake_put(url, content=None, headers=None, timeout=None):
            return httpx.Response(403, request=httpx.Request("PUT", url))

        original_put = publish_script.httpx.put
        publish_script.httpx.put = fake_put
        try:
            with pytest.raises(publish_script.PublishError) as excinfo:
                publish_script.upload_files(build_dir, uploads)
        finally:
            publish_script.httpx.put = original_put
        assert "index.html" in str(excinfo.value)
        assert "403" in str(excinfo.value)
        # The presigned URL (a short-lived authorization) never leaks.
        assert "presigned" not in str(excinfo.value)

    def test_connection_error_reports_the_path_without_the_url(self, tmp_path):
        build_dir = _build_dir(tmp_path, {"index.html": b"index"})
        uploads = _begin_response(
            [{"path": "index.html", "sha256": "a" * 64, "size": 5}]
        )["uploads"]

        def fake_put(url, content=None, headers=None, timeout=None):
            raise httpx.ConnectError("refused")

        original_put = publish_script.httpx.put
        publish_script.httpx.put = fake_put
        try:
            with pytest.raises(publish_script.PublishError) as excinfo:
                publish_script.upload_files(build_dir, uploads)
        finally:
            publish_script.httpx.put = original_put
        assert "index.html" in str(excinfo.value)
        assert uploads[0]["url"] not in str(excinfo.value)


class TestFinalizeUpload:
    def _finalize(self, monkeypatch, status_code):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return httpx.Response(
                status_code,
                json={"root_path": "docs", "replay": status_code == 200},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        manifest = [{"path": "index.html", "sha256": "a" * 64, "size": 6}]
        entry = publish_script.finalize_upload(
            "http://api",
            API_TOKEN,
            PROJECT_SECRET,
            upload_id=7,
            manifest=manifest,
        )
        assert captured["url"] == "http://api/api/v1/uploads/7/finalize"
        assert captured["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
        assert captured["json"]["project_secret"] == PROJECT_SECRET
        assert captured["json"]["manifest"] == manifest
        assert entry["root_path"] == "docs"

    def test_finalize_accepts_first_completion(self, monkeypatch):
        self._finalize(monkeypatch, 201)

    def test_finalize_accepts_idempotent_replay(self, monkeypatch):
        self._finalize(monkeypatch, 200)

    def test_error_response_never_leaks_either_credential(self, monkeypatch):
        def fake_post(url, json=None, headers=None, timeout=None):
            return httpx.Response(
                409, text="manifest does not match", request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.finalize_upload(
                "http://api",
                API_TOKEN,
                PROJECT_SECRET,
                upload_id=7,
                manifest=[{"path": "index.html", "sha256": "a" * 64, "size": 6}],
            )
        assert PROJECT_SECRET not in str(excinfo.value)
        assert API_TOKEN not in str(excinfo.value)


class TestMainFlow:
    def _install_fake_http(self, monkeypatch, tmp_path, files):
        manifest = publish_script.build_manifest(_build_dir(tmp_path, files))
        begun = _begin_response(manifest)
        calls: list[tuple[str, str, Any]] = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(("post", url, json))
            if url.endswith("/api/v1/uploads"):
                return httpx.Response(
                    201, json=begun, request=httpx.Request("POST", url)
                )
            return httpx.Response(
                201, json={"root_path": "docs"}, request=httpx.Request("POST", url)
            )

        def fake_put(url, content=None, headers=None, timeout=None):
            calls.append(("put", url, content))
            return httpx.Response(200, request=httpx.Request("PUT", url))

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        monkeypatch.setattr(publish_script.httpx, "put", fake_put)
        return begun, calls

    def test_main_runs_begin_put_finalize_without_s3_credentials(
        self, tmp_path, monkeypatch, capsys
    ):
        files = {"index.html": b"<html>index</html>", "usage/index.html": b"usage"}
        begun, calls = self._install_fake_http(monkeypatch, tmp_path, files)
        # No S3_* settings exist anywhere: the presigned URLs authorize the
        # uploads, the script holds no storage credentials.
        for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
            monkeypatch.delenv(name, raising=False)

        code = publish_script.main(
            [
                "--build-dir",
                str(tmp_path / "build"),
                "--commit-hash",
                "deadbeef",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 0

        kinds = [call[0] for call in calls]
        # One begin, one PUT per file, one finalize - in that order.
        assert kinds == ["post", "put", "put", "post"]
        begin_call = calls[0]
        finalize_call = calls[-1]
        assert begin_call[1].endswith("/api/v1/uploads")
        assert begin_call[2]["manifest"] == publish_script.build_manifest(
            tmp_path / "build"
        )
        assert begin_call[2]["project_secret"] == PROJECT_SECRET
        for upload, call in zip(begun["uploads"], calls[1:3]):
            assert call[1] == upload["url"]
            assert call[2] == (tmp_path / "build" / upload["path"]).read_bytes()
        assert finalize_call[1].endswith(f"/api/v1/uploads/{begun['upload_id']}/finalize")
        assert finalize_call[2]["manifest"] == begin_call[2]["manifest"]

        out = capsys.readouterr()
        assert f"uploaded 2 files under {begun['key_prefix']}" in out.out
        assert PROJECT_SECRET not in out.out + out.err
        assert API_TOKEN not in out.out + out.err

    def test_main_failure_output_never_leaks_credentials(
        self, tmp_path, monkeypatch, capsys
    ):
        _build_dir(tmp_path)

        def fake_post(url, json=None, headers=None, timeout=None):
            return httpx.Response(
                403, text="invalid publish token", request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        code = publish_script.main(
            [
                "--build-dir",
                str(tmp_path / "build"),
                "--commit-hash",
                "deadbeef",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert PROJECT_SECRET not in captured.out + captured.err
        assert API_TOKEN not in captured.out + captured.err

    def test_missing_api_token_is_reported_without_leaking(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_URL", "http://api")
        monkeypatch.setenv("PROJECT_SECRET", PROJECT_SECRET)
        monkeypatch.delenv("API_TOKEN", raising=False)
        build_dir = _build_dir(tmp_path)
        code = publish_script.main(["--build-dir", str(build_dir)])
        assert code == 1

    def test_missing_project_secret_is_reported_without_leaking(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("API_URL", "http://api")
        monkeypatch.setenv("API_TOKEN", API_TOKEN)
        monkeypatch.delenv("PROJECT_SECRET", raising=False)
        build_dir = _build_dir(tmp_path)
        code = publish_script.main(["--build-dir", str(build_dir)])
        assert code == 1

    def test_empty_build_directory_is_rejected(self, tmp_path, monkeypatch):
        code = publish_script.main(
            [
                "--build-dir",
                str(tmp_path / "build"),
                "--commit-hash",
                "deadbeef",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 1

    def test_invalid_root_is_rejected(self, tmp_path):
        build_dir = _build_dir(tmp_path)
        code = publish_script.main(
            [
                "--build-dir",
                str(build_dir),
                "--root-path",
                "docs/../etc",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 1

    def test_nested_roots_are_normalized_before_use(self, tmp_path, monkeypatch):
        files = {"index.html": b"<html>index</html>"}
        _begun, calls = self._install_fake_http(monkeypatch, tmp_path, files)
        code = publish_script.main(
            [
                "--build-dir",
                str(tmp_path / "build"),
                "--root-path",
                "  Project-1//Docs/ ",
                "--commit-hash",
                "deadbeef",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 0
        begin_call = calls[0]
        assert begin_call[2]["root_path"] == "project-1/docs"
        assert begin_call[2]["project_secret"] == "super-secret"
