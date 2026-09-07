"""End-to-end publish-and-serve integration test for the doc-hosting PoC.

Requires a Juju controller bootstrapped on microk8s
(``uv run scripts/deploy.py setup``) and run with ``-m integration``:
``uv run pytest tests/integration -v -m integration``.
"""

from __future__ import annotations

import os
import pathlib
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


def test_integration_publish_and_serve(
    juju: jubilant.Juju, connection: dict[str, str], docs_build: pathlib.Path
):
    """Publish via scripts/publish.py and serve the docs from the deployed charm."""
    # Publish the built docs with the same tooling the GitHub workflow uses.
    env = {
        **os.environ,
        "API_URL": connection["api_url"],
        "API_TOKEN": connection["api_token"],
        "S3_ENDPOINT": connection["s3_endpoint"],
        "S3_ACCESS_KEY": connection["s3_access_key"],
        "S3_SECRET_KEY": connection["s3_secret_key"],
        "S3_BUCKET": connection["s3_bucket"],
        "S3_REGION": "us-east-1",
    }
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/publish.py",
            "--build-dir",
            str(docs_build),
            "--root-path",
            "docs",
            "--language",
            "en",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"publish failed:\n{result.stdout}\n{result.stderr}"

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

    # The version API lists the registered build.
    response = httpx.get(
        f"{base_url}/api/v1/versions", params={"root_path": "docs"}, timeout=60
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["root_path"] == "docs"
    versions = {v["version"]: v for v in payload["versions"]}
    assert "latest" in versions
    assert versions["latest"]["languages"] == ["en"]
    assert versions["latest"]["commit_hash"]

    # Unauthenticated publishes are rejected.
    response = httpx.post(
        f"{base_url}/api/v1/publish",
        json={
            "commit_hash": "deadbeef",
            "version": "latest",
            "language": "en",
            "domain": "localhost",
            "root_path": "docs",
        },
        timeout=60,
    )
    assert response.status_code == 401
