"""Unit tests for the publish script: credentials, layouts, nested roots."""

from __future__ import annotations

import httpx
import pytest

from scripts import publish as publish_script

API_TOKEN = "the-api-token"


class TestUploadPrefix:
    def test_default_layout_uses_language_and_version(self):
        assert (
            publish_script.upload_prefix(
                "docs",
                {"language_enabled": True, "version_enabled": True},
                language="en",
                version="latest",
            )
            == "docs/en/latest"
        )

    def test_disabled_dimensions_drop_their_segment(self):
        layout = {"language_enabled": False, "version_enabled": True}
        assert (
            publish_script.upload_prefix("docs", layout, language="en", version="1.0")
            == "docs/1.0"
        )
        layout = {"language_enabled": True, "version_enabled": False}
        assert (
            publish_script.upload_prefix("docs", layout, language="en", version="1.0")
            == "docs/en"
        )
        assert (
            publish_script.upload_prefix(
                "docs",
                {"language_enabled": False, "version_enabled": False},
                language="en",
                version="1.0",
            )
            == "docs"
        )

    def test_nested_root_prefix(self):
        assert (
            publish_script.upload_prefix(
                "project-1/docs",
                {"language_enabled": True, "version_enabled": True},
                language="en",
                version="latest",
            )
            == "project-1/docs/en/latest"
        )


class TestFetchLayout:
    def test_unknown_root_defaults_to_both_dimensions(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return httpx.Response(404, request=httpx.Request("GET", url))

        monkeypatch.setattr(publish_script.httpx, "get", fake_get)
        layout = publish_script.fetch_layout("http://api", "docs")
        assert layout == {"language_enabled": True, "version_enabled": True}
        assert captured["params"] == {"root_path": "docs"}

    def test_returns_the_reported_layout(self, monkeypatch):
        def fake_get(url, params=None, timeout=None):
            return httpx.Response(
                200,
                json={"layout": {"language_enabled": False, "version_enabled": True}},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(publish_script.httpx, "get", fake_get)
        assert publish_script.fetch_layout("http://api", "docs") == {
            "language_enabled": False,
            "version_enabled": True,
        }

    def test_connection_error_is_a_publish_error(self, monkeypatch):
        def fake_get(url, params=None, timeout=None):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(publish_script.httpx, "get", fake_get)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.fetch_layout("http://api", "docs")
        assert "layout" in str(excinfo.value)


class TestCredentials:
    def test_register_sends_bearer_token_and_json_secret(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return httpx.Response(
                201, json={"root_path": "docs"}, request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        entry = publish_script.register_build(
            "http://api",
            API_TOKEN,
            "super-secret",
            commit_hash="c",
            version="latest",
            language="en",
            domain="d",
            root_path="docs",
        )
        assert entry == {"root_path": "docs"}
        # The API token travels as the bearer header...
        assert captured["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
        # ...and the project secret only inside the JSON body.
        assert captured["json"]["project_secret"] == "super-secret"

    def test_connection_error_never_leaks_either_credential(self, monkeypatch):
        def fake_post(url, json=None, headers=None, timeout=None):
            assert headers["Authorization"] == f"Bearer {API_TOKEN}"
            assert json["project_secret"] == "super-secret"
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.register_build(
                "http://api",
                API_TOKEN,
                "super-secret",
                commit_hash="c",
                version="latest",
                language="en",
                domain="d",
                root_path="docs",
            )
        assert "super-secret" not in str(excinfo.value)
        assert API_TOKEN not in str(excinfo.value)

    def test_error_response_never_leaks_either_credential(self, monkeypatch):
        def fake_post(url, json=None, headers=None, timeout=None):
            return httpx.Response(
                403, text="invalid publish token", request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        with pytest.raises(publish_script.PublishError) as excinfo:
            publish_script.register_build(
                "http://api",
                API_TOKEN,
                "super-secret",
                commit_hash="c",
                version="latest",
                language="en",
                domain="d",
                root_path="docs",
            )
        assert "super-secret" not in str(excinfo.value)
        assert API_TOKEN not in str(excinfo.value)

    def test_missing_api_token_is_reported_without_leaking(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("API_URL", "http://api")
        monkeypatch.setenv("PROJECT_SECRET", "super-secret")
        monkeypatch.delenv("API_TOKEN", raising=False)
        monkeypatch.setenv("S3_ACCESS_KEY", "k")
        monkeypatch.setenv("S3_SECRET_KEY", "s")
        monkeypatch.setenv("S3_BUCKET", "b")
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        code = publish_script.main(["--build-dir", str(build_dir)])
        assert code == 1

    def test_missing_project_secret_is_reported_without_leaking(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("API_URL", "http://api")
        monkeypatch.setenv("API_TOKEN", API_TOKEN)
        monkeypatch.delenv("PROJECT_SECRET", raising=False)
        monkeypatch.setenv("S3_ACCESS_KEY", "k")
        monkeypatch.setenv("S3_SECRET_KEY", "s")
        monkeypatch.setenv("S3_BUCKET", "b")
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        code = publish_script.main(["--build-dir", str(build_dir)])
        assert code == 1


class TestRootPathHandling:
    def test_nested_roots_are_normalized_before_use(self, tmp_path, monkeypatch):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["params"] = params
            return httpx.Response(404, request=httpx.Request("GET", url))

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["body"] = json
            captured["headers"] = headers
            return httpx.Response(
                201,
                json={"root_path": "project-1/docs", "claimed": True},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(publish_script.httpx, "get", fake_get)
        monkeypatch.setattr(publish_script.httpx, "post", fake_post)
        monkeypatch.setattr(
            publish_script,
            "upload_build",
            lambda *args, **kwargs: 0,
        )
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        code = publish_script.main(
            [
                "--build-dir",
                str(build_dir),
                "--root-path",
                "  Project-1//Docs/ ",
                "--env-file",
                str(_env_file(tmp_path)),
            ]
        )
        assert code == 0
        assert captured["params"] == {"root_path": "project-1/docs"}
        assert captured["body"]["root_path"] == "project-1/docs"
        assert captured["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
        assert captured["body"]["project_secret"] == "test-secret"

    def test_invalid_root_is_rejected(self, tmp_path, monkeypatch):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
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


def _env_file(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text(
        "\n".join(
            [
                "API_URL=http://api",
                f"API_TOKEN={API_TOKEN}",
                "PROJECT_SECRET=test-secret",
                "S3_ACCESS_KEY=k",
                "S3_SECRET_KEY=s",
                "S3_BUCKET=b",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_file
