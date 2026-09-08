#!/usr/bin/env python3
"""Publish built documentation to a doc-hosting deployment.

Uploads the files of a documentation build directory to the S3 bucket
under the project's active URL layout and then registers the build with
the doc-hosting ingestion API (commit hash, version, language, domain,
root path), exactly as the GitHub Actions workflow does.

Two independent credentials are required, mirroring the API's two gates:

* ``API_TOKEN`` is the deployment-wide publish token; it is only ever sent
  as the ``Authorization: Bearer`` header.
* ``PROJECT_SECRET`` is the project's shared secret; it is only ever sent
  inside the JSON request body (the first fully authenticated publication
  claims an unclaimed root path, and every later publication of that root
  must present the same secret).

Neither credential is ever logged, printed, or included in error messages.
Both are supplied through the environment (or an env file such as the
``.juju-deploy.env`` written by ``scripts/deploy.py``); environment
variables take precedence over the env file.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import pathlib
import subprocess
import sys
from typing import Any

import boto3
import httpx
from botocore.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from doc_hosting import paths


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (ignoring blanks and comments)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


class PublishError(RuntimeError):
    """Raised on any publish failure; reported to stderr with exit code 1."""


def default_version() -> str:
    """Return the tag name when running on a tag push, else ``latest``."""
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME") or "latest"
    return "latest"


def default_commit_hash() -> str:
    """Return GITHUB_SHA when available, else the current git commit."""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublishError(
            "unable to determine the commit hash: no GITHUB_SHA and "
            "`git rev-parse HEAD` failed"
        ) from exc


def fetch_layout(api_url: str, root_path: str) -> dict[str, Any]:
    """Return the project's URL layout, defaulting to language+version.

    An unknown root path (a brand-new project) uses the default layout.
    """
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/api/v1/versions",
            params={"root_path": root_path},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"unable to query the project layout: {exc}") from exc
    if response.status_code == 404:
        return {"language_enabled": True, "version_enabled": True}
    if response.status_code != 200:
        raise PublishError(
            f"unable to query the project layout: the API returned {response.status_code}"
        )
    layout = response.json().get("layout")
    if not isinstance(layout, dict):
        return {"language_enabled": True, "version_enabled": True}
    return layout


def upload_prefix(
    root_path: str,
    layout: dict[str, Any],
    *,
    language: str,
    version: str,
) -> str:
    """Return the S3 key prefix for the project's active URL layout."""
    segments = [root_path]
    if layout.get("language_enabled", True):
        segments.append(language)
    if layout.get("version_enabled", True):
        segments.append(version)
    return "/".join(segments)


def upload_build(
    build_dir: pathlib.Path,
    *,
    prefix: str,
    endpoint: str | None,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str | None,
) -> int:
    """Upload every file under ``build_dir`` to the S3 bucket; return the count."""
    config = Config(s3={"addressing_style": "path"}) if endpoint else None
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region or "us-east-1",
        config=config,
    )
    count = 0
    for path in sorted(p for p in build_dir.rglob("*") if p.is_file()):
        key = f"{prefix}/{path.relative_to(build_dir).as_posix()}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType=content_type,
        )
        count += 1
        print(f"uploaded s3://{bucket}/{key} ({content_type})")
    return count


def register_build(
    api_url: str,
    api_token: str,
    project_secret: str,
    *,
    commit_hash: str,
    version: str,
    language: str,
    domain: str,
    root_path: str,
) -> dict[str, Any]:
    """POST the build metadata to the ingestion API and return the stored entry.

    The deployment-wide ``api_token`` travels as the bearer header and the
    per-root ``project_secret`` travels only inside the JSON body; neither
    is ever included in an error message.
    """
    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/v1/publish",
            json={
                "commit_hash": commit_hash,
                "version": version,
                "language": language,
                "domain": domain,
                "root_path": root_path,
                "project_secret": project_secret,
            },
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        # Never include either credential in errors; httpx errors do not
        # carry request headers or the body.
        raise PublishError(f"unable to reach the publish API: {exc}") from exc
    if response.status_code != 201:
        raise PublishError(
            f"publish API returned {response.status_code}: {response.text}"
        )
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-dir",
        required=True,
        type=pathlib.Path,
        help="directory containing the built documentation (e.g. docs/_build/dirhtml)",
    )
    parser.add_argument(
        "--root-path",
        default="docs",
        help="root path, possibly nested (default: docs)",
    )
    parser.add_argument("--language", default="en", help="language (default: en)")
    parser.add_argument(
        "--version",
        default=None,
        help="version tag (default: tag name on tag pushes, else 'latest')",
    )
    parser.add_argument(
        "--commit-hash",
        default=None,
        help="commit hash (default: GITHUB_SHA, else `git rev-parse HEAD`)",
    )
    parser.add_argument(
        "--domain", default=None, help="serving domain (default: DOC_DOMAIN or localhost)"
    )
    parser.add_argument(
        "--env-file",
        type=pathlib.Path,
        default=None,
        help="KEY=VALUE file with API_URL/API_TOKEN/PROJECT_SECRET/S3_* settings (e.g. .juju-deploy.env)",
    )
    args = parser.parse_args(argv)

    try:
        env_file = parse_env_file(args.env_file) if args.env_file else {}

        def setting(name: str) -> str | None:
            return os.environ.get(name) or env_file.get(name) or None

        build_dir = args.build_dir.resolve()
        if not build_dir.is_dir():
            raise PublishError(f"build directory not found: {build_dir}")

        try:
            root_path = paths.normalize_root_path(args.root_path)
        except paths.InvalidPathError as exc:
            raise PublishError(str(exc)) from exc

        api_url = setting("API_URL")
        api_token = setting("API_TOKEN")
        project_secret = setting("PROJECT_SECRET")
        s3_endpoint = setting("S3_ENDPOINT")
        s3_access_key = setting("S3_ACCESS_KEY")
        s3_secret_key = setting("S3_SECRET_KEY")
        s3_bucket = setting("S3_BUCKET")
        s3_region = setting("S3_REGION") or "us-east-1"

        missing = [
            name
            for name, value in [
                ("API_URL", api_url),
                ("API_TOKEN", api_token),
                ("PROJECT_SECRET", project_secret),
                ("S3_ACCESS_KEY", s3_access_key),
                ("S3_SECRET_KEY", s3_secret_key),
                ("S3_BUCKET", s3_bucket),
            ]
            if not value
        ]
        if missing:
            raise PublishError(
                "missing required settings (set them in the environment or the "
                f"env file): {', '.join(missing)}"
            )

        version = args.version or default_version()
        commit_hash = args.commit_hash or default_commit_hash()
        domain = args.domain or setting("DOC_DOMAIN") or "localhost"

        layout = fetch_layout(api_url, root_path)
        prefix = upload_prefix(
            root_path, layout, language=args.language, version=version
        )

        count = upload_build(
            build_dir,
            prefix=prefix,
            endpoint=s3_endpoint,
            access_key=s3_access_key,
            secret_key=s3_secret_key,
            bucket=s3_bucket,
            region=s3_region,
        )
        print(f"uploaded {count} files to s3://{s3_bucket}/{prefix}")

        entry = register_build(
            api_url,
            api_token,
            project_secret,
            commit_hash=commit_hash,
            version=version,
            language=args.language,
            domain=domain,
            root_path=root_path,
        )
        print(f"registered build: {entry}")
        served = f"{api_url.rstrip('/')}/{prefix}/"
        print(f"documentation now served at {served}")
        return 0
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - report any failure clearly
        print(f"error: publish failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
