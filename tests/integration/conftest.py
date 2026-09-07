"""Fixtures for the doc-hosting integration tests.

Prerequisite: a Juju controller bootstrapped on microk8s, e.g. via
``uv run scripts/deploy.py setup``. The module-scoped ``juju`` fixture from
pytest-jubilant creates a temporary model and destroys it on teardown.

The deployment mirrors ``scripts/deploy.py deploy``: MinIO provides the S3
storage backend, the s3-integrator charm (track 2) provides the ``s3``
interface to the doc-hosting-api charm (configured with the MinIO endpoint
and credentials and the bucket to use).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Any

import boto3
import jubilant
import pytest
from botocore.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = "doc-hosting-api"
MINIO = "minio"
MINIO_CHANNEL = "latest/edge"
S3_INTEGRATOR = "s3-integrator"
S3_INTEGRATOR_CHANNEL = "2/stable"
BUCKET = "doc-hosting"
TOKEN = "integration-token"
MINIO_ACCESS_KEY = "doc-hosting-int"
MINIO_SECRET_KEY = "integration-secret-key"
APP_IMAGE = "localhost:32000/doc-hosting-api:0.1"


def newest(matches: list[pathlib.Path]) -> pathlib.Path:
    """Return the most recently modified file of ``matches``."""
    return max(matches, key=lambda path: path.stat().st_mtime)


@pytest.fixture(scope="module")
def built_artifacts() -> tuple[pathlib.Path, str]:
    """Return ``(charm_file, app_image)``, building them if not provided.

    Set ``CHARM_FILE`` and ``APP_IMAGE`` in the environment to skip the
    (slow) build and reuse already packed artefacts.
    """
    charm_env = os.environ.get("CHARM_FILE")
    image_env = os.environ.get("APP_IMAGE")
    if charm_env and image_env:
        return pathlib.Path(charm_env), image_env

    rockcraft_env = {**os.environ, "ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "true"}
    charmcraft_env = {
        **os.environ,
        "CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS": "true",
    }
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
            "requirements.txt",
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        ["rockcraft", "pack"], cwd=REPO_ROOT, env=rockcraft_env, check=True
    )
    rock = newest(list(REPO_ROOT.glob("doc-hosting-api_*.rock")))
    subprocess.run(
        [
            "rockcraft.skopeo",
            "copy",
            "--insecure-policy",
            "--dest-tls-verify=false",
            f"oci-archive:{rock}",
            f"docker://{APP_IMAGE}",
        ],
        check=True,
    )
    subprocess.run(
        ["charmcraft", "pack"], cwd=REPO_ROOT / "charm", env=charmcraft_env, check=True
    )
    charm = newest(list((REPO_ROOT / "charm").glob("doc-hosting-api_*.charm")))
    return charm, APP_IMAGE


@pytest.fixture(scope="module")
def deployment(juju: jubilant.Juju, built_artifacts: tuple[pathlib.Path, str]):
    """Deploy MinIO + s3-integrator + the doc-hosting-api charm and wait for active."""
    charm_file, app_image = built_artifacts

    juju.deploy(
        MINIO,
        MINIO,
        channel=MINIO_CHANNEL,
        config={
            "access-key": MINIO_ACCESS_KEY,
            "secret-key": MINIO_SECRET_KEY,
        },
    )
    juju.deploy(S3_INTEGRATOR, S3_INTEGRATOR, channel=S3_INTEGRATOR_CHANNEL)
    secret_uri = juju.add_secret(
        name="doc-hosting-s3-credentials",
        content={
            "access-key": MINIO_ACCESS_KEY,
            "secret-key": MINIO_SECRET_KEY,
        },
    )
    juju.grant_secret(identifier=secret_uri, app=S3_INTEGRATOR)
    juju.config(
        S3_INTEGRATOR,
        {
            "endpoint": f"http://{MINIO}.{juju.model}.svc.cluster.local:9000",
            "bucket": BUCKET,
            "credentials": secret_uri,
        },
    )
    juju.deploy(charm_file, APP, resources={"app-image": app_image})
    juju.integrate(f"{APP}:s3", f"{S3_INTEGRATOR}:s3-credentials")
    juju.config(APP, {"publish-token": TOKEN})

    juju.wait(
        lambda status: (
            status.apps[MINIO].is_active
            and status.apps[S3_INTEGRATOR].is_active
            and status.apps[APP].is_active
        ),
        timeout=1500,
        delay=5,
        error=lambda status: (
            status.apps[APP].app_status.current == "error"
            or status.apps[S3_INTEGRATOR].app_status.current == "error"
        ),
    )
    yield


@pytest.fixture(scope="module")
def connection(juju: jubilant.Juju, deployment) -> dict[str, str]:
    """Return the connection details (API URL, MinIO endpoint, bucket, credentials)."""
    status = juju.status()
    api_url = f"http://{status.apps[APP].units[f'{APP}/0'].address}:8080"
    minio_endpoint = f"http://{status.apps[MINIO].units[f'{MINIO}/0'].address}:9000"

    # Discover the bucket and credentials the charm received over the s3
    # relation (scanning both sides of the relation).
    relation_data: dict[str, Any] = {}
    for unit in (f"{APP}/0", f"{S3_INTEGRATOR}/0"):
        for relation in juju.show_unit(unit).relation_info:
            for key, value in relation.app_data.items():
                if isinstance(value, str):
                    relation_data.setdefault(key, value)

    bucket = relation_data.get("bucket") or BUCKET
    access_key = relation_data.get("access-key") or MINIO_ACCESS_KEY
    secret_key = relation_data.get("secret-key") or MINIO_SECRET_KEY

    # Safety net: make sure the bucket exists before publishing into it.
    client = boto3.client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        client.create_bucket(Bucket=bucket)

    return {
        "api_url": api_url,
        "api_token": TOKEN,
        "s3_endpoint": minio_endpoint,
        "s3_access_key": access_key,
        "s3_secret_key": secret_key,
        "s3_bucket": bucket,
    }
