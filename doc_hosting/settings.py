"""Runtime settings for the doc-hosting API layer.

All configuration is provided through environment variables (12-factor),
injected by the paas-charm based charm:

* ``S3_ACCESS_KEY``, ``S3_SECRET_KEY``, ``S3_BUCKET`` (required, from the
  ``s3`` integration, e.g. the MinIO charm's ``s3-credentials`` endpoint)
* ``S3_ENDPOINT``, ``S3_REGION``, ``S3_PATH``, ``S3_URI_STYLE``,
  ``S3_ADDRESSING_STYLE`` (optional, from the same integration)
* ``APP_PUBLISH_TOKEN`` (the charm's ``publish-token`` config option)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(RuntimeError):
    """Raised when required settings (environment variables) are missing."""


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the environment-driven application settings."""

    access_key: str
    secret_key: str
    bucket: str
    endpoint: str | None
    region: str
    path_prefix: str
    addressing_style: str
    publish_token: str | None


def _normalize_addressing_style(*values: str | None) -> str:
    """Return a boto3 addressing style for the first non-empty style hint."""
    for value in values:
        if not value:
            continue
        normalized = value.strip().lower().replace("-hosted", "").replace("_", "-")
        if normalized in ("virtual", "virtualhost"):
            return "virtual"
        return "path"
    return "path"


def get_settings() -> Settings:
    """Read the application settings from the environment.

    Raises:
        SettingsError: if one of the required S3 variables is not set.

    """
    missing = [
        name
        for name in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET")
        if not os.environ.get(name)
    ]
    if missing:
        raise SettingsError(
            "missing required environment variables: " + ", ".join(missing)
        )
    return Settings(
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        bucket=os.environ["S3_BUCKET"],
        endpoint=os.environ.get("S3_ENDPOINT") or None,
        region=os.environ.get("S3_REGION") or "us-east-1",
        path_prefix=(os.environ.get("S3_PATH") or "").strip("/"),
        addressing_style=_normalize_addressing_style(
            os.environ.get("S3_ADDRESSING_STYLE"), os.environ.get("S3_URI_STYLE")
        ),
        publish_token=os.environ.get("APP_PUBLISH_TOKEN") or None,
    )
