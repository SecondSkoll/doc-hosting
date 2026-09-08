"""S3-backed storage for built documentation and legacy registry metadata.

The object key layout mirrors the served URL paths
(``{root_path}/{language}/{version}/<page>``); legacy registry metadata is
kept as JSON objects under the reserved ``_registry/`` prefix in the same
bucket and is only read (for the idempotent importer into PostgreSQL).
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
            config=Config(
                s3={"addressing_style": settings.addressing_style},
                # Deterministic SigV4 presigned URLs: without an explicit
                # signature version botocore still emits legacy SigV2
                # query URLs for the us-east-1 region.
                signature_version="s3v4",
            ),
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

    def presign_put(
        self, key: str, expires_in: int, checksum_sha256_b64: str | None = None
    ) -> dict[str, Any]:
        """Return a short-lived exact-key presigned PUT url for ``key``.

        The URL is restricted to exactly this object key (with the
        configured ``S3_PATH`` prefix applied) and expires after
        ``expires_in`` seconds. When ``checksum_sha256_b64`` (the
        base64-encoded SHA-256 digest) is given, it is signed into the URL
        and the storage rejects a PUT whose body does not match; the
        header the caller must send alongside the URL is returned in
        ``headers``.
        """
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": self._full_key(key)}
        headers: dict[str, str] = {}
        if checksum_sha256_b64:
            params["ChecksumSHA256"] = checksum_sha256_b64
            # With SigV4 presigning the checksum becomes a signed header
            # the uploader must present unchanged.
            headers["x-amz-checksum-sha256"] = checksum_sha256_b64
        url = self._client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=expires_in
        )
        return {"url": url, "headers": headers}

    def head_object_info(self, key: str) -> dict[str, Any] | None:
        """Return ``{"size", "checksum_sha256"}`` for ``key``, or ``None``.

        The checksum is the base64-encoded SHA-256 digest as stored by the
        object (``ChecksumMode=ENABLED``); it is ``None`` when the storage
        does not report a stored checksum for the object.
        """
        try:
            response = self._client.head_object(
                Bucket=self.bucket, Key=self._full_key(key), ChecksumMode="ENABLED"
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {
            "size": response["ContentLength"],
            "checksum_sha256": response.get("ChecksumSHA256"),
        }

    def list_keys(self, prefix: str = "") -> list[str]:
        """Return the sorted object keys under ``prefix`` (logical keys).

        Logical keys exclude the configured ``S3_PATH`` prefix but keep the
        caller's prefix (matching :meth:`get_bytes` and :meth:`copy_object`).
        """
        full_prefix = self._full_key(prefix)
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        path_prefix = self._settings.path_prefix
        if path_prefix:
            strip = len(path_prefix) + 1
            keys = [key[strip:] for key in keys]
        return sorted(keys)

    def copy_object(self, source: str, destination: str) -> None:
        """Copy one object to ``destination`` (idempotent: overwrites)."""
        self._client.copy_object(
            Bucket=self.bucket,
            Key=self._full_key(destination),
            CopySource={"Bucket": self.bucket, "Key": self._full_key(source)},
        )

    def delete_object(self, key: str) -> None:
        """Delete one object; deleting a missing key is not an error."""
        self._client.delete_object(Bucket=self.bucket, Key=self._full_key(key))

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; return the deleted count."""
        keys = self.list_keys(prefix)
        for key in keys:
            self.delete_object(key)
        return len(keys)

    def list_registry_root_paths(self) -> list[str]:
        """Return the root paths that have a legacy registry entry in the bucket."""
        root_paths: list[str] = []
        prefix = self._full_key(REGISTRY_PREFIX)
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if name.endswith(REGISTRY_SUFFIX):
                    root_paths.append(name[: -len(REGISTRY_SUFFIX)])
        return sorted(root_paths)

    def get_registry(self, root_path: str) -> dict[str, Any] | None:
        """Return the parsed legacy registry JSON for ``root_path``, or ``None``."""
        data = self.get_bytes(f"{REGISTRY_PREFIX}{root_path}{REGISTRY_SUFFIX}")
        if data is None:
            return None
        return json.loads(data)
