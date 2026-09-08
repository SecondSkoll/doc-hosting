"""Unit tests for the S3 storage helpers (list, copy, delete)."""

from __future__ import annotations

import pytest

from doc_hosting.settings import get_settings
from doc_hosting.storage import S3Storage

pytestmark = pytest.mark.usefixtures("aws")


def test_list_keys_returns_logical_keys_in_order(storage):
    storage.put_bytes("docs/en/latest/b.html", b"b")
    storage.put_bytes("docs/en/latest/a.html", b"a")
    storage.put_bytes("other/x.html", b"x")
    assert storage.list_keys("docs/") == ["docs/en/latest/a.html", "docs/en/latest/b.html"]
    assert storage.list_keys() == [
        "docs/en/latest/a.html",
        "docs/en/latest/b.html",
        "other/x.html",
    ]
    assert storage.list_keys("missing/") == []


def test_copy_object_overwrites_idempotently(storage):
    storage.put_bytes("docs/src.html", b"v1")
    storage.copy_object("docs/src.html", "docs/dst.html")
    assert storage.get_bytes("docs/dst.html") == b"v1"
    storage.put_bytes("docs/src.html", b"v2")
    storage.copy_object("docs/src.html", "docs/dst.html")
    assert storage.get_bytes("docs/dst.html") == b"v2"
    storage.copy_object("docs/src.html", "docs/dst.html")
    assert storage.get_bytes("docs/dst.html") == b"v2"


def test_delete_object_is_idempotent_for_missing_keys(storage):
    storage.put_bytes("docs/x.html", b"x")
    storage.delete_object("docs/x.html")
    assert not storage.exists("docs/x.html")
    # Deleting a missing key is not an error.
    storage.delete_object("docs/x.html")


def test_delete_prefix_removes_only_the_prefix(storage):
    storage.put_bytes("docs/en/latest/index.html", b"1")
    storage.put_bytes("docs/guides/index.html", b"2")
    storage.put_bytes("other/index.html", b"3")
    deleted = storage.delete_prefix("docs/")
    assert deleted == 2
    assert not storage.exists("docs/en/latest/index.html")
    assert not storage.exists("docs/guides/index.html")
    assert storage.get_bytes("other/index.html") == b"3"


def test_helpers_round_trip_end_to_end(storage):
    storage.put_bytes("docs/en/latest/index.html", b"index")
    keys = storage.list_keys("docs/")
    for key in keys:
        storage.copy_object(key, f"backup/{key}")
    assert storage.get_bytes("backup/docs/en/latest/index.html") == b"index"
    storage.delete_prefix("docs/")
    assert storage.list_keys("docs/") == []
    assert storage.list_keys("backup/docs/") == ["backup/docs/en/latest/index.html"]


def test_registry_read_helpers_remain_available(storage, aws):
    import json

    aws.put_object(
        Bucket="test-bucket",
        Key="_registry/docs.json",
        Body=json.dumps({"root_path": "docs", "builds": []}).encode(),
    )
    assert storage.list_registry_root_paths() == ["docs"]
    assert storage.get_registry("docs") == {"root_path": "docs", "builds": []}
    assert storage.get_registry("unknown") is None


def test_storage_instance_built_from_environment():
    # The storage helper is constructible from the charm-injected environment.
    storage = S3Storage(get_settings())
    assert storage.bucket == "test-bucket"


def test_list_keys_with_s3_path_prefix(monkeypatch):
    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
        monkeypatch.setenv("S3_PATH", "prefix")
        storage = S3Storage(get_settings())
        storage.put_bytes("docs/x.html", b"x")
        # Logical keys exclude the configured S3_PATH prefix.
        assert storage.list_keys("docs/") == ["docs/x.html"]
        assert storage.get_bytes("docs/x.html") == b"x"
