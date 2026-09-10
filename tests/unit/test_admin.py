"""Unit tests for the Django admin at /manage/ (staff-only, CSRF, read-only audit)."""

from __future__ import annotations

import pytest
from conftest import register_build
from django.contrib.auth import get_user_model
from django.test import Client

from doc_hosting.registry import models, services

pytestmark = pytest.mark.usefixtures("aws")


@pytest.fixture()
def superuser(clean_db):
    return get_user_model().objects.create_superuser("boss", "boss@example.com", "pass-1234")


@pytest.fixture()
def staff_client(superuser):
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture()
def claimed_project(clean_db):
    register_build(project_secret="project-secret")
    return models.Project.objects.get(root_path="docs")


def test_admin_requires_login(client):
    response = client.get("/manage/", follow_redirects=False)
    assert response.status_code == 302
    assert "/manage/login" in response.headers["location"]


def test_admin_rejects_non_staff(clean_db):
    get_user_model().objects.create_user("peon", password="pass-1234")
    client = Client()
    response = client.post(
        "/manage/login/",
        {"username": "peon", "password": "pass-1234", "next": "/manage/"},
    )
    # The non-staff user cannot authenticate into the admin.
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_staff_user_can_open_the_admin(staff_client):
    response = staff_client.get("/manage/")
    assert response.status_code == 200
    assert b"doc-hosting administration" in response.content


def test_csrf_is_enforced(staff_client):
    enforcing = Client(enforce_csrf_checks=True)
    enforcing.force_login(get_user_model().objects.get(username="boss"))
    response = enforcing.post(
        "/manage/registry/redirect/add/",
        {
            "match_type": "exact",
            "from_path": "/a",
            "to_path": "/b",
            "enabled": "on",
        },
    )
    assert response.status_code == 403


def test_direct_project_creation_is_disallowed(staff_client):
    response = staff_client.get("/manage/registry/project/add/")
    assert response.status_code == 403


def test_project_secret_rotation_is_write_only(staff_client, claimed_project):
    row = claimed_project

    # The change page never shows the stored hash.
    response = staff_client.get(f"/manage/registry/project/{row.pk}/change/")
    assert response.status_code == 200
    assert b"pbkdf2" not in response.content
    assert b"old-secret" not in response.content
    assert b"secret_hash" not in response.content

    response = staff_client.post(
        f"/manage/registry/project/{row.pk}/change/",
        {
            "root_path": "docs",
            "domain": "docs.example.com",
            "rotate_secret": "new-secret",
        },
        follow=True,
    )
    assert response.status_code == 200
    row.refresh_from_db()
    assert row.check_secret("new-secret")
    assert not row.check_secret("project-secret")
    assert b"new-secret" not in response.content
    assert models.AuditEvent.objects.filter(
        event_type="project.secret_rotated"
    ).exists()


def test_publication_add_is_disallowed(staff_client):
    response = staff_client.get("/manage/registry/publication/add/")
    assert response.status_code == 403


def test_publication_edit_is_disallowed_but_inspection_and_deletion_retained(
    staff_client, claimed_project
):
    publication = claimed_project.publications.get()
    change_url = f"/manage/registry/publication/{publication.pk}/change/"

    # Inspection is retained as a read-only view (no editable inputs).
    response = staff_client.get(change_url)
    assert response.status_code == 200
    assert b'name="commit_hash"' not in response.content
    assert b'name="language"' not in response.content

    # Direct edits are rejected outright.
    response = staff_client.post(change_url, {"commit_hash": "hacked"})
    assert response.status_code == 403
    publication.refresh_from_db()
    assert publication.commit_hash == "deadbeef"

    # Deletion (with audit) is still available.
    response = staff_client.post(
        f"/manage/registry/publication/{publication.pk}/delete/",
        {"post": "yes"},
        follow=True,
    )
    assert response.status_code == 200
    assert not models.Publication.objects.exists()
    assert models.AuditEvent.objects.filter(
        event_type="publication.deleted"
    ).exists()


def test_redirect_management_runs_through_services(staff_client):
    # A valid redirect is created via the services (with validation).
    response = staff_client.post(
        "/manage/registry/redirect/add/",
        {
            "match_type": "exact",
            "from_path": "/docs/en/old",
            "to_path": "/docs/en/new",
            "enabled": "on",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert models.Redirect.objects.filter(from_path="/docs/en/old").exists()

    # An invalid redirect (reserved namespace) is rejected and not created.
    response = staff_client.post(
        "/manage/registry/redirect/add/",
        {
            "match_type": "exact",
            "from_path": "/api/evil",
            "to_path": "/docs",
            "enabled": "on",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert b"not saved" in response.content
    assert not models.Redirect.objects.filter(from_path="/api/evil").exists()


def test_audit_events_are_read_only(staff_client, claimed_project):
    response = staff_client.get("/manage/registry/auditevent/")
    assert response.status_code == 200
    # No add button is offered for the immutable audit history.
    assert b"/manage/registry/auditevent/add/" not in response.content
    event = models.AuditEvent.objects.first()
    add_response = staff_client.get("/manage/registry/auditevent/add/")
    assert add_response.status_code == 403
    delete_response = staff_client.post(
        f"/manage/registry/auditevent/{event.pk}/delete/", {"post": "yes"}
    )
    assert delete_response.status_code == 403
    assert models.AuditEvent.objects.count() >= 1


def test_path_migration_action_via_admin(staff_client, claimed_project):
    response = staff_client.post(
        "/manage/registry/pathmigration/add/",
        {"project": str(claimed_project.pk), "new_root": "newdocs"},
        follow=True,
    )
    assert response.status_code == 200
    claimed_project.refresh_from_db()
    assert claimed_project.root_path == "newdocs"
    migration = models.PathMigration.objects.get()
    assert migration.status == models.PathMigration.STATUS_COMPLETED


def test_path_migration_invalid_destination_reports_error(staff_client, claimed_project):
    response = staff_client.post(
        "/manage/registry/pathmigration/add/",
        {"project": str(claimed_project.pk), "new_root": "docs"},
        follow=True,
    )
    assert response.status_code == 200
    assert b"Migration failed" in response.content
    assert not models.PathMigration.objects.exists()


def test_migration_operation_fields_are_readonly_after_creation(
    staff_client, storage, claimed_project
):
    migration = services.create_root_migration(claimed_project, "newdocs")
    assert migration.status == models.PathMigration.STATUS_PENDING

    # Resuming the change page offers no editable operation-defining field.
    response = staff_client.get(
        f"/manage/registry/pathmigration/{migration.pk}/change/"
    )
    assert response.status_code == 200
    assert b'name="new_root"' not in response.content
    assert b'name="project"' not in response.content

    # Saving from the change page only resumes the recorded operation.
    response = staff_client.post(
        f"/manage/registry/pathmigration/{migration.pk}/change/", {}, follow=True
    )
    assert response.status_code == 200
    migration.refresh_from_db()
    claimed_project.refresh_from_db()
    assert migration.status == models.PathMigration.STATUS_COMPLETED
    assert migration.new_root == "newdocs"
    assert claimed_project.root_path == "newdocs"


def test_layout_change_action_via_admin(staff_client, claimed_project):
    response = staff_client.post(
        "/manage/registry/layoutchange/add/",
        {
            "project": str(claimed_project.pk),
            "new_language_enabled": "on",
            "language_label": "",
            "version_label": "",
        },
        follow=True,
    )
    assert response.status_code == 200
    claimed_project.refresh_from_db()
    assert claimed_project.version_enabled is False
    change = models.LayoutChange.objects.get()
    assert change.status == models.LayoutChange.STATUS_COMPLETED


def test_layout_change_operation_fields_are_readonly_after_creation(
    staff_client, storage, claimed_project
):
    # A pending change (the S3 copy fails mid-flight) is resumable.
    storage.put_bytes("docs/en/latest/index.html", b"index")

    with pytest.MonkeyPatch.context() as patcher:
        def failing_copy(source, destination):
            raise OSError("boom")

        patcher.setattr(storage, "copy_object", failing_copy)
        with pytest.raises(OSError):
            services.toggle_project_layout(
                claimed_project,
                language_enabled=True,
                version_enabled=False,
                storage=storage,
            )
    change = models.LayoutChange.objects.get(status=models.LayoutChange.STATUS_PENDING)
    original_root = claimed_project.root_path

    response = staff_client.get(
        f"/manage/registry/layoutchange/{change.pk}/change/"
    )
    assert response.status_code == 200
    assert b'name="new_language_enabled"' not in response.content
    assert b'name="new_version_enabled"' not in response.content
    assert b'name="language_label"' not in response.content
    assert b'name="project"' not in response.content

    # Saving from the change page only resumes the recorded operation.
    response = staff_client.post(
        f"/manage/registry/layoutchange/{change.pk}/change/", {}, follow=True
    )
    assert response.status_code == 200
    change.refresh_from_db()
    claimed_project.refresh_from_db()
    assert change.status == models.LayoutChange.STATUS_COMPLETED
    assert change.new_language_enabled is True
    assert change.new_version_enabled is False
    assert (claimed_project.language_enabled, claimed_project.version_enabled) == (
        True,
        False,
    )
    assert claimed_project.root_path == original_root
