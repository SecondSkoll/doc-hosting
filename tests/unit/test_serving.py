"""Unit tests for the catch-all serving route (moto-mocked S3)."""

from __future__ import annotations

import pytest
from conftest import publish

from doc_hosting.registry import models, services

pytestmark = pytest.mark.usefixtures("client")


def test_serving_default_layout(client, aws):
    publish(client)
    aws.put_object(
        Bucket="test-bucket",
        Key="docs/en/latest/index.html",
        Body=b"<html><body>doc-hosting PoC</body></html>",
        ContentType="text/html",
    )
    aws.put_object(
        Bucket="test-bucket", Key="docs/en/latest/usage/index.html", Body=b"usage page"
    )
    aws.put_object(
        Bucket="test-bucket", Key="docs/en/latest/_static/style.css", Body=b"x{color:red}"
    )

    # Nothing uploaded yet for a missing page.
    assert client.get("/docs/en/latest/missing.html").status_code == 404

    # Index resolution (dirhtml layout: trailing slash -> index.html).
    response = client.get("/docs/en/latest/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert b"doc-hosting PoC" in response.content

    # Version root without trailing slash redirects (307) to the index.
    response = client.get("/docs/en/latest", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs/en/latest/"

    # Nested page and asset content types.
    response = client.get("/docs/en/latest/usage/")
    assert response.status_code == 200
    assert b"usage page" in response.content
    response = client.get("/docs/en/latest/_static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")

    # Missing page -> 404; traversal attempts -> 404.
    assert client.get("/docs/en/latest/missing.html").status_code == 404
    assert client.get("/docs/en/latest/../../_registry/docs.json").status_code == 404
    assert client.get("/docs/en/../..").status_code == 404


def test_partial_dimension_paths_are_not_served(client):
    publish(client)
    assert client.get("/docs/en").status_code == 404
    assert client.get("/docs/en/").status_code == 404
    assert client.get("/docs").status_code == 404


def test_unclaimed_paths_return_404(client):
    publish(client)
    assert client.get("/unknown/en/latest/").status_code == 404
    assert client.get("/api/v1/unknown").status_code == 404


def test_nested_root_serves(client, aws):
    publish(client, root_path="Project-1//Docs/")
    aws.put_object(
        Bucket="test-bucket",
        Key="project-1/docs/en/latest/index.html",
        Body=b"nested index",
    )
    response = client.get("/project-1/docs/en/latest/")
    assert response.status_code == 200
    assert b"nested index" in response.content
    response = client.get("/project-1/docs/en/latest", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/project-1/docs/en/latest/"


def test_longest_registered_root_wins(client, aws):
    publish(client, root_path="docs")
    publish(client, root_path="docs/guides")
    aws.put_object(
        Bucket="test-bucket", Key="docs/en/latest/index.html", Body=b"outer"
    )
    aws.put_object(
        Bucket="test-bucket", Key="docs/guides/en/latest/index.html", Body=b"inner"
    )
    assert b"outer" in client.get("/docs/en/latest/").content
    assert b"inner" in client.get("/docs/guides/en/latest/").content


def test_case_insensitive_root_matching_preserves_doc_case(client, aws):
    publish(client)
    aws.put_object(
        Bucket="test-bucket", Key="docs/en/latest/File.HTML", Body=b"cased"
    )
    response = client.get("/Docs/en/latest/File.HTML")
    assert response.status_code == 200
    assert b"cased" in response.content


def test_layout_root_only_serves_at_the_root(client, storage):
    publish(client)
    # Content under the old layout: layout redirect pairs are derived from
    # actually mapped keys, so the toggle needs content to re-key.
    storage.put_bytes("docs/en/latest/index.html", b"root index")
    storage.put_bytes("docs/en/latest/usage/index.html", b"usage")
    project = models.Project.objects.get(root_path="docs")
    services.toggle_project_layout(
        project, language_enabled=False, version_enabled=False, storage=storage
    )

    assert b"root index" in client.get("/docs/").content
    assert client.get("/docs", follow_redirects=False).status_code == 307
    assert client.get("/docs", follow_redirects=False).headers["location"] == "/docs/"
    assert b"usage" in client.get("/docs/usage/").content
    # The old dimension URLs redirect (301) to their new locations.
    response = client.get("/docs/en/latest/", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/docs/"
    assert b"root index" in client.get("/docs/en/latest/", follow_redirects=True).content
    # A partial dimension URL was never valid and still does not resolve.
    assert client.get("/docs/en/").status_code == 404


def test_public_404_does_not_leak_the_internal_s3_key(client, aws):
    publish(client)
    aws.put_object(
        Bucket="test-bucket", Key="docs/en/latest/index.html", Body=b"index"
    )
    response = client.get("/docs/en/latest/missing.html")
    assert response.status_code == 404
    assert response.json()["detail"] == "not found"
    # The internal S3 key never appears in the public error response.
    assert "docs/en/latest" not in response.text
    assert "base_key" not in response.text


def test_manage_requires_login(client):
    response = client.get("/manage/", follow_redirects=False)
    assert response.status_code == 302
    assert "/manage/login" in response.headers["location"]
