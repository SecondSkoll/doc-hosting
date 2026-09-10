"""End-to-end publish-and-serve integration test for the doc-hosting PoC.

Requires a Juju controller bootstrapped on microk8s
(``uv run scripts/deploy.py setup``) and run with ``-m integration``:
``uv run pytest tests/integration -v -m integration``.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import httpx
import jubilant
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def docs_build(tmp_path_factory) -> pathlib.Path:
    """Build the repository documentation with the direct Sphinx command."""
    build_dir = tmp_path_factory.mktemp("docs-build") / "dirhtml"
    subprocess.run(
        [
            "uv",
            "run",
            "--group",
            "docs",
            "sphinx-build",
            "-b",
            "dirhtml",
            str(REPO_ROOT / "docs"),
            str(build_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (build_dir / "index.html").is_file()
    return build_dir


def publish_env(connection: dict[str, str]) -> dict[str, str]:
    """The environment for scripts/publish.py (never prints the credentials).

    No S3 credentials are passed: the API authorizes the direct upload with
    short-lived presigned URLs and the script holds no storage credentials.
    """
    return {
        **os.environ,
        "API_URL": connection["api_url"],
        "API_TOKEN": connection["api_token"],
        "PROJECT_SECRET": connection["project_secret"],
    }


def run_publish(connection: dict[str, str], build_dir: pathlib.Path, *extra: str) -> str:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/publish.py",
            "--build-dir",
            str(build_dir),
            *extra,
        ],
        cwd=REPO_ROOT,
        env=publish_env(connection),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"publish failed:\n{result.stdout}\n{result.stderr}"
    assert connection["project_secret"] not in result.stdout
    assert connection["project_secret"] not in result.stderr
    assert connection["api_token"] not in result.stdout
    assert connection["api_token"] not in result.stderr
    return result.stdout


def test_integration_publish_and_serve(
    juju: jubilant.Juju, connection: dict[str, str], docs_build: pathlib.Path
):
    """Publish via scripts/publish.py and serve the docs from the deployed charm."""
    # Publish the built docs with the same tooling the GitHub workflow uses
    # (both credentials: the deployment bearer token and the project secret).
    run_publish(connection, docs_build, "--root-path", "docs", "--language", "en")

    base_url = connection["api_url"]

    # The built index page is served from the bucket by the deployed service.
    response = httpx.get(f"{base_url}/docs/en/latest/", timeout=60)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "doc-hosting" in response.text

    # Nested pages (dirhtml layout) and the redirect from the version root.
    response = httpx.get(f"{base_url}/docs/en/latest/reference/", timeout=60)
    assert response.status_code == 200
    assert "Reference" in response.text
    response = httpx.get(
        f"{base_url}/docs/en/latest", timeout=60, follow_redirects=False
    )
    assert response.status_code == 307
    assert response.headers["location"] == "/docs/en/latest/"

    # The version API lists the registered build (PostgreSQL control plane).
    response = httpx.get(
        f"{base_url}/api/v1/versions", params={"root_path": "docs"}, timeout=60
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["root_path"] == "docs"
    assert payload["layout"]["language_enabled"] is True
    assert payload["layout"]["version_enabled"] is True
    versions = {v["version"]: v for v in payload["versions"]}
    assert "latest" in versions
    assert versions["latest"]["languages"] == ["en"]
    assert versions["latest"]["commit_hash"]

    # Publishes without the deployment bearer are rejected with 401.
    response = httpx.post(
        f"{base_url}/api/v1/uploads",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "localhost",
            "root_path": "docs",
            "project_secret": connection["project_secret"],
        },
        timeout=60,
    )
    assert response.status_code == 401


def test_integration_publish_checks_both_credentials(connection: dict[str, str]):
    """Both gates are enforced independently: bearer and project secret.

    Depends on the ``docs`` root already being claimed with the connection's
    project secret by ``test_integration_publish_and_serve`` (module order):
    a begin against an unclaimed root would claim it instead of failing.
    """
    base_url = connection["api_url"]
    body = {
        "commit_hash": "deadbeef",
        "version": "latest",
        "language": "en",
        "domain": "localhost",
        "root_path": "docs",
    }

    # A wrong bearer with the correct project secret is rejected (403).
    response = httpx.post(
        f"{base_url}/api/v1/uploads",
        json={**body, "project_secret": connection["project_secret"]},
        headers={"Authorization": "Bearer wrong-api-token"},
        timeout=60,
    )
    assert response.status_code == 403

    # A valid bearer with a wrong project secret is rejected (403). The
    # probe carries a valid manifest so it passes manifest validation and
    # reaches the project-secret check instead of failing with 422.
    response = httpx.post(
        f"{base_url}/api/v1/uploads",
        json={
            **body,
            "project_secret": "wrong-project-secret",
            "manifest": [
                {
                    "path": "index.html",
                    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "size": 1,
                }
            ],
        },
        headers={"Authorization": f"Bearer {connection['api_token']}"},
        timeout=60,
    )
    assert response.status_code == 403

    # A valid bearer without a project secret is rejected (422).
    response = httpx.post(
        f"{base_url}/api/v1/uploads",
        json=body,
        headers={"Authorization": f"Bearer {connection['api_token']}"},
        timeout=60,
    )
    assert response.status_code == 422


def test_integration_removed_publish_endpoint_returns_method_not_allowed(
    connection: dict[str, str],
):
    """The register-only publish endpoint is gone.

    A fully credentialed ``POST /api/v1/publish`` no longer reaches any
    ingestion handler: only the GET catch-all serving route matches the
    path, so the request fails with 405.
    """
    response = httpx.post(
        f"{connection['api_url']}/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "localhost",
            "root_path": "docs",
            "project_secret": connection["project_secret"],
        },
        headers={"Authorization": f"Bearer {connection['api_token']}"},
        timeout=60,
    )
    assert response.status_code == 405


def test_integration_nested_root_publish_and_serve(
    connection: dict[str, str], docs_build: pathlib.Path
):
    """The same project secret may claim and publish a nested root path."""
    base_url = connection["api_url"]
    run_publish(
        connection,
        docs_build,
        "--root-path",
        "Project-1//Docs/",
        "--language",
        "en",
        "--version",
        "nested",
    )

    response = httpx.get(f"{base_url}/project-1/docs/en/nested/", timeout=60)
    assert response.status_code == 200
    assert "doc-hosting" in response.text

    response = httpx.get(
        f"{base_url}/api/v1/versions",
        params={"root_path": "project-1/docs"},
        timeout=60,
    )
    assert response.status_code == 200
    assert response.json()["root_path"] == "project-1/docs"


def test_integration_admin_is_staff_only(connection: dict[str, str]):
    """The Django admin at /manage/ redirects anonymous users to its login."""
    response = httpx.get(
        f"{connection['admin_url']}", timeout=60, follow_redirects=False
    )
    assert response.status_code == 302
    assert "/manage/login" in response.headers["location"]


def test_integration_admin_login_with_configured_credentials(connection: dict[str, str]):
    """The charm-configured superuser can sign in to the admin console."""
    admin_url = connection["admin_url"]
    with httpx.Client(timeout=60) as client:
        # GET the login page first: the session cookie and the form's CSRF
        # token are both required by the POST.
        login_page = client.get(f"{admin_url}login/")
        assert login_page.status_code == 200
        match = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text
        )
        assert match is not None
        response = client.post(
            f"{admin_url}login/",
            data={
                "csrfmiddlewaretoken": match.group(1),
                "username": connection["admin_username"],
                "password": connection["admin_password"],
                "next": "/manage/",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/manage/login" not in response.headers["location"]

        # The admin index loads for the signed-in superuser.
        index = client.get(admin_url, follow_redirects=False)
        assert index.status_code == 200
        assert "Site administration" in index.text
