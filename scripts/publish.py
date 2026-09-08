#!/usr/bin/env python3
"""Publish built documentation to a doc-hosting deployment.

Builds the documentation manifest (relative path, SHA-256 and size of
every file), asks the doc-hosting publishing API to authorize a direct
upload, PUTs every file to the short-lived path-restricted presigned S3
URLs the API returns (the API verifies project/repository authorization
and computes the storage keys), and then finalizes the upload with the
manifest so the API verifies the uploaded objects and atomically
registers the build, exactly as the GitHub Actions workflow does.

Two independent credentials are required, mirroring the API's two gates:

* ``API_TOKEN`` is the deployment-wide publish token; it is only ever sent
  as the ``Authorization: Bearer`` header.
* ``PROJECT_SECRET`` is the project's shared secret; it is only ever sent
  inside the JSON request body (the first fully authenticated publication
  claims an unclaimed root path, and every later publication of that root
  must present the same secret).

No S3 credentials are needed: the presigned URLs authorize the uploads.

Neither credential is ever logged, printed, or included in error messages.
Both are supplied through the environment (or an env file such as the
``.juju-deploy.env`` written by ``scripts/deploy.py``); environment
variables take precedence over the env file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import subprocess
import sys
from typing import Any

import httpx

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


def build_manifest(build_dir: pathlib.Path) -> list[dict[str, Any]]:
    """Return the sorted per-file manifest (path, sha256, size) of the build."""
    entries: list[dict[str, Any]] = []
    for path in sorted(p for p in build_dir.rglob("*") if p.is_file()):
        data = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(build_dir).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return entries


def begin_upload(
    api_url: str,
    api_token: str,
    project_secret: str,
    *,
    commit_hash: str,
    version: str,
    language: str,
    domain: str,
    root_path: str,
    manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask the API to authorize a direct upload; return its presigned URLs.

    The deployment-wide ``api_token`` travels as the bearer header and the
    per-root ``project_secret`` travels only inside the JSON body; neither
    is ever included in an error message.
    """
    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/v1/uploads",
            json={
                "commit_hash": commit_hash,
                "version": version,
                "language": language,
                "domain": domain,
                "root_path": root_path,
                "project_secret": project_secret,
                "manifest": manifest,
            },
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        # Never include either credential in errors; httpx errors do not
        # carry request headers or the body.
        raise PublishError(f"unable to reach the upload API: {exc}") from exc
    if response.status_code != 201:
        raise PublishError(
            f"upload API returned {response.status_code}: {response.text}"
        )
    return response.json()


def upload_files(build_dir: pathlib.Path, uploads: list[dict[str, Any]]) -> int:
    """PUT every build file to its presigned URL with the returned headers."""
    count = 0
    for upload in uploads:
        path = build_dir / upload["path"]
        try:
            response = httpx.put(
                upload["url"],
                content=path.read_bytes(),
                headers=upload.get("headers") or {},
                timeout=300,
            )
        except httpx.HTTPError as exc:
            # The presigned URL (a short-lived authorization of its own)
            # never appears in the error; only the file path does.
            raise PublishError(f"unable to upload {upload['path']!r}: {exc}") from exc
        if response.status_code != 200:
            raise PublishError(
                f"upload of {upload['path']!r} failed: the storage returned "
                f"{response.status_code}"
            )
        count += 1
        print(f"uploaded {upload['path']}")
    return count


def finalize_upload(
    api_url: str,
    api_token: str,
    project_secret: str,
    *,
    upload_id: Any,
    manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    """Finalize the upload with its manifest; the API verifies and registers."""
    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/api/v1/uploads/{upload_id}/finalize",
            json={"project_secret": project_secret, "manifest": manifest},
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"unable to reach the finalize API: {exc}") from exc
    if response.status_code not in (200, 201):
        raise PublishError(
            f"finalize API returned {response.status_code}: {response.text}"
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
        help="KEY=VALUE file with API_URL/API_TOKEN/PROJECT_SECRET settings (e.g. .juju-deploy.env)",
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

        missing = [
            name
            for name, value in [
                ("API_URL", api_url),
                ("API_TOKEN", api_token),
                ("PROJECT_SECRET", project_secret),
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

        manifest = build_manifest(build_dir)
        if not manifest:
            raise PublishError(f"no files found under the build directory: {build_dir}")

        begun = begin_upload(
            api_url,
            api_token,
            project_secret,
            commit_hash=commit_hash,
            version=version,
            language=args.language,
            domain=domain,
            root_path=root_path,
            manifest=manifest,
        )
        key_prefix = begun["key_prefix"]
        print(f"upload session {begun['upload_id']} authorized under {key_prefix}")

        count = upload_files(build_dir, begun["uploads"])
        print(f"uploaded {count} files under {key_prefix}")

        entry = finalize_upload(
            api_url,
            api_token,
            project_secret,
            upload_id=begun["upload_id"],
            manifest=manifest,
        )
        print(f"registered build: {entry}")
        served = f"{api_url.rstrip('/')}/{key_prefix}/"
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
