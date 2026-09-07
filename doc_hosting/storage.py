"""S3-backed storage for built documentation and registry metadata.

The object key layout mirrors the served URL paths
(``{root_path}/{language}/{version}/<page>``); registry metadata is kept as
JSON objects under the reserved ``_registry/`` prefix in the same bucket.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .settings import Settings

REGISTRY_PREFIX = "_registry/"
REGISTRY_SUFFIX = ".json"


class NotFound(Exception):
    """Raised when a requested object does not exist in the bucket."""


class S3Storage:
    """Thin wrapper around a boto3 S3 client built from application settings."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
            config=Config(s3={"addressing_style": settings.addressing_style}),
        )

    @property
    def bucket(self) -> str:
        """Return the configured bucket name."""
        return self._settings.bucket

    def _full_key(self, key: str) -> str:
        """Return ``key`` with the configured ``S3_PATH`` prefix applied."""
        prefix = self._settings.path_prefix
        if not prefix:
            return key
        return f"{prefix}/{key.lstrip('/')}"

    def get_bytes(self, key: str) -> bytes | None:
        """Return the object contents, or ``None`` if the key is missing."""
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read()

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Upload ``data`` to ``key``."""
        args: dict[str, Any] = {"Bucket": self.bucket, "Key": self._full_key(key), "Body": data}
        if content_type:
            args["ContentType"] = content_type
        self._client.put_object(**args)

    def exists(self, key: str) -> bool:
        """Return whether ``key`` exists in the bucket."""
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise
        return True

    def list_registry_root_paths(self) -> list[str]:
        """Return the root paths that have a registry entry in the bucket."""
        root_paths: list[str] = []
        prefix = self._full_key(REGISTRY_PREFIX)
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix) :]
                if name.endswith(REGISTRY_SUFFIX):
                    root_paths.append(name[: -len(REGISTRY_SUFFIX)])
        return sorted(root_paths)

    def get_registry(self, root_path: str) -> dict[str, Any] | None:
        """Return the parsed registry JSON for ``root_path``, or ``None``."""
        data = self.get_bytes(f"{REGISTRY_PREFIX}{root_path}{REGISTRY_SUFFIX}")
        if data is None:
            return None
        return json.loads(data)

    def put_registry(self, root_path: str, data: dict[str, Any]) -> None:
        """Store ``data`` as the registry JSON for ``root_path``."""
        payload = json.dumps(data).encode("utf-8")
        self.put_bytes(
            f"{REGISTRY_PREFIX}{root_path}{REGISTRY_SUFFIX}",
            payload,
            content_type="application/json",
        )
