"""Unit tests for the root migration state machine."""

from __future__ import annotations

import pytest

from doc_hosting.registry import models, services

pytestmark = pytest.mark.usefixtures("aws")


def claimed(root="docs", secret="project-secret"):
    services.publish_build(
        root_path=root,
        language="en",
        version="latest",
        commit_hash="deadbeef",
        domain="docs.example.com",
        project_secret=secret,
    )
    return models.Project.objects.get(root_path=root)


class TestMigrationBoundaryChecks:
    def test_destination_equal_to_current_root_conflicts(self, storage):
        project = claimed()
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "DOCS")
        assert excinfo.value.status_code == 409

    def test_invalid_destination_is_rejected(self, storage):
        project = claimed()
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "../etc")
        assert excinfo.value.status_code == 422
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "api")
        assert excinfo.value.status_code == 422

    def test_claimed_destination_conflicts(self, storage):
        project = claimed("docs")
        claimed("guides", secret="guides-secret")
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "guides")
        assert excinfo.value.status_code == 409

    def test_destination_shadowing_a_nested_root_conflicts(self, storage):
        project = claimed("docs")
        # A project exists at x/y; migrating docs to x would shadow it.
        claimed("x/y", secret="nested-secret")
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "x")
        assert excinfo.value.status_code == 409

    def test_old_root_with_nested_project_conflicts(self, storage):
        claimed("outer", secret="outer-secret")
        project = models.Project.objects.get(root_path="outer")
        services.publish_build(
            root_path="outer/inner",
            language="en",
            version="latest",
            commit_hash="x",
            domain="d",
            project_secret="outer-secret",
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "somewhere-else")
        assert excinfo.value.status_code == 409

    def test_destination_nested_under_another_root_conflicts(self, storage):
        project = claimed("docs")
        claimed("guides", secret="guides-secret")
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "guides/under")
        assert excinfo.value.status_code == 409


class TestMigrationStateMachine:
    def test_full_flow_copies_switches_redirects_and_deletes(self, client, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")

        migration = services.create_root_migration(project, "Project-2//Docs/")
        assert migration.status == models.PathMigration.STATUS_PENDING
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        assert migration.old_root == "docs"
        assert migration.new_root == "project-2/docs"
        assert migration.completed_at is not None

        project.refresh_from_db()
        assert project.root_path == "project-2/docs"
        assert storage.exists("project-2/docs/en/latest/index.html")
        assert not storage.exists("docs/en/latest/index.html")

        # The old root redirects to the new root (suffix preserved).
        response = client.get("/docs/en/latest/usage/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/project-2/docs/en/latest/usage/"
        # The new root serves directly.
        assert b"index" in client.get("/project-2/docs/en/latest/").content

        # The old root is no longer a registered project root.
        response = client.get("/api/v1/versions", params={"root_path": "docs"})
        assert response.status_code == 404
        assert (
            client.get("/api/v1/versions", params={"root_path": "project-2/docs"}).status_code
            == 200
        )

        event_types = set(
            models.AuditEvent.objects.values_list("event_type", flat=True)
        )
        assert {"migration.requested", "migration.switched", "migration.completed"} <= (
            event_types
        )

    def test_existing_redirects_are_rewritten_to_the_new_root(self, storage):
        project = claimed("docs")
        services.create_redirect(
            "/docs/en/latest/old",
            "/docs/en/latest/new",
            match_type=models.Redirect.MATCH_EXACT,
            project=project,
        )
        migration = services.create_root_migration(project, "newdocs")
        services.run_root_migration(migration, storage)
        redirect = models.Redirect.objects.get(match_type=models.Redirect.MATCH_EXACT)
        assert redirect.from_path == "/newdocs/en/latest/old"
        assert redirect.to_path == "/newdocs/en/latest/new"
        # The automatically created old-to-new prefix redirect exists too.
        prefix = models.Redirect.objects.get(match_type=models.Redirect.MATCH_PREFIX)
        assert prefix.from_path == "/docs"
        assert prefix.to_path == "/newdocs"

    def test_downward_migration_excludes_the_destination_subtree(self, client, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        migration = services.create_root_migration(project, "docs/new")
        services.run_root_migration(migration, storage)
        project.refresh_from_db()
        assert project.root_path == "docs/new"

        # Requests under the old root redirect into the new root...
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/new/en/latest/"
        # ...but requests already inside the destination are served, not
        # redirected (self-exclusion for the downward prefix redirect).
        response = client.get("/docs/new/en/latest/")
        assert response.status_code == 200
        assert b"index" in response.content

    def test_preexisting_destination_to_source_redirect_yields_clean_409(
        self, client, storage
    ):
        # A preexisting /newdocs -> /docs redirect would loop with the
        # generated old-to-new prefix redirect.
        services.create_redirect("/newdocs", "/docs", match_type=models.Redirect.MATCH_EXACT)
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")

        migration = services.create_root_migration(project, "newdocs")
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        # The migration stays pending (retryable) and nothing changed.
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        project.refresh_from_db()
        assert project.root_path == "docs"
        assert not models.Redirect.objects.filter(
            from_path="/docs", match_type=models.Redirect.MATCH_PREFIX
        ).exists()
        # No loop is installed: serving is undamaged on both sides.
        assert b"index" in client.get("/docs/en/latest/").content
        response = client.get("/newdocs", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs"

        # Removing the blocker makes the migration retryable to completion.
        models.Redirect.objects.get(
            from_path="/newdocs", match_type=models.Redirect.MATCH_EXACT
        ).delete()
        services.invalidate_redirect_cache()
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/newdocs/en/latest/"
        assert b"index" in client.get("/newdocs/en/latest/").content

    def test_upward_migration_deletes_exactly_the_copied_source_keys(
        self, client, storage
    ):
        project = claimed("docs/a")
        storage.put_bytes("docs/a/en/latest/index.html", b"index")
        storage.put_bytes("docs/a/en/latest/usage/index.html", b"usage")
        # Preexisting content inside the destination root but outside the
        # copied subtree must survive the migration.
        storage.put_bytes("docs/x/y.html", b"unrelated")

        migration = services.create_root_migration(project, "docs")
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        assert migration.key_mapping == {
            "docs/a/en/latest/index.html": "docs/en/latest/index.html",
            "docs/a/en/latest/usage/index.html": "docs/en/latest/usage/index.html",
        }

        project.refresh_from_db()
        assert project.root_path == "docs"
        # The copied old source keys are gone, the new keys exist.
        assert not storage.exists("docs/a/en/latest/index.html")
        assert not storage.exists("docs/a/en/latest/usage/index.html")
        assert storage.exists("docs/en/latest/index.html")
        assert storage.exists("docs/en/latest/usage/index.html")
        # The unrelated destination-side content was never deleted.
        assert storage.exists("docs/x/y.html")

        # Old URLs redirect upward into the new root and serve.
        response = client.get("/docs/a/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/en/latest/"
        assert b"index" in client.get("/docs/en/latest/").content

    def test_retry_after_partial_copy(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")

        def flaky_copy(source, destination):
            raise OSError("boom")

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(storage, "copy_object", flaky_copy)
            migration = services.create_root_migration(project, "newdocs")
            with pytest.raises(OSError):
                services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        project.refresh_from_db()
        assert project.root_path == "docs"

        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        project.refresh_from_db()
        assert project.root_path == "newdocs"
        assert storage.exists("newdocs/en/latest/index.html")
        assert not storage.exists("docs/en/latest/index.html")

    def test_retry_after_switch_before_delete(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")

        def failing_delete(key):
            raise OSError("boom")

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(storage, "delete_object", failing_delete)
            migration = services.create_root_migration(project, "newdocs")
            with pytest.raises(OSError):
                services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_SWITCHED
        # The source->destination mapping snapshot is persisted for the retry.
        assert migration.key_mapping == {
            "docs/en/latest/index.html": "newdocs/en/latest/index.html"
        }
        project.refresh_from_db()
        assert project.root_path == "newdocs"
        assert storage.exists("docs/en/latest/index.html")

        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        assert not storage.exists("docs/en/latest/index.html")

    def test_completed_migration_is_idempotent(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        migration = services.create_root_migration(project, "newdocs")
        migration = services.run_root_migration(migration, storage)
        # Re-running a completed migration is a no-op.
        assert services.run_root_migration(migration, storage).status == (
            models.PathMigration.STATUS_COMPLETED
        )

    def test_redirect_rewrite_collision_is_actionable_409(self, client, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.create_redirect(
            "/docs/en/old",
            "/docs/en/new",
            match_type=models.Redirect.MATCH_EXACT,
            project=project,
        )
        # Another exact redirect already occupies the rewritten source.
        services.create_redirect(
            "/newdocs/en/old", "/elsewhere", match_type=models.Redirect.MATCH_EXACT
        )
        migration = services.create_root_migration(project, "newdocs")
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        assert "/newdocs/en/old" in excinfo.value.detail
        # Transaction rollback: the migration stays pending, the project
        # keeps serving from the old root and the redirect is untouched.
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        project.refresh_from_db()
        assert project.root_path == "docs"
        redirect = models.Redirect.objects.get(
            from_path="/docs/en/old", match_type=models.Redirect.MATCH_EXACT
        )
        assert redirect.to_path == "/docs/en/new"
        assert b"index" in client.get("/docs/en/latest/").content

        # Removing the blocker makes the migration retryable to completion.
        models.Redirect.objects.get(
            from_path="/newdocs/en/old", match_type=models.Redirect.MATCH_EXACT
        ).delete()
        services.invalidate_redirect_cache()
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        redirect.refresh_from_db()
        assert redirect.from_path == "/newdocs/en/old"
        assert redirect.to_path == "/newdocs/en/new"

    def test_prefix_redirect_creation_collision_is_actionable_409(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        # A prefix redirect at the old root already exists (written through
        # the ORM to bypass the creation guards).
        models.Redirect.objects.create(
            match_type=models.Redirect.MATCH_PREFIX,
            from_path="/docs",
            to_path="/somewhere",
        )
        services.invalidate_redirect_cache()

        migration = services.create_root_migration(project, "newdocs")
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        assert "/docs" in excinfo.value.detail
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        project.refresh_from_db()
        assert project.root_path == "docs"

        # Removing the blocker makes the migration retryable to completion.
        models.Redirect.objects.get(
            from_path="/docs", match_type=models.Redirect.MATCH_PREFIX
        ).delete()
        services.invalidate_redirect_cache()
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        project.refresh_from_db()
        assert project.root_path == "newdocs"


class TestMigrationGuards:
    """One non-completed migration per project; stale ones never touch storage."""

    def test_second_migration_while_pending_conflicts(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.create_root_migration(project, "newdocs")
        with pytest.raises(services.ServiceError) as excinfo:
            services.create_root_migration(project, "otherdocs")
        assert excinfo.value.status_code == 409
        # No second migration row was created.
        assert models.PathMigration.objects.count() == 1
        assert models.PathMigration.objects.get().new_root == "newdocs"

    def test_stale_migration_cannot_touch_storage(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        migration = services.create_root_migration(project, "newdocs")
        # The project root has since moved on (a raced operation).
        project.root_path = "elsewhere"
        project.save(update_fields=["root_path", "updated_at"])
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        assert "stale" in excinfo.value.detail
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        # Storage is untouched: no copy happened.
        assert storage.exists("docs/en/latest/index.html")
        assert not storage.exists("newdocs/en/latest/index.html")
        assert not models.Redirect.objects.filter(
            from_path="/docs", match_type=models.Redirect.MATCH_PREFIX
        ).exists()
        # Restoring the expected root makes the migration retryable.
        project.root_path = "docs"
        project.save(update_fields=["root_path", "updated_at"])
        services.run_root_migration(migration, storage)
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_COMPLETED
        assert storage.exists("newdocs/en/latest/index.html")
        assert not storage.exists("docs/en/latest/index.html")

    def test_switch_reruns_destination_boundary_checks(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        migration = services.create_root_migration(project, "newdocs")
        # The destination was claimed by another project after the request.
        services.publish_build(
            root_path="newdocs",
            language="en",
            version="latest",
            commit_hash="x",
            domain="d",
            project_secret="other-secret",
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_PENDING
        project.refresh_from_db()
        assert project.root_path == "docs"
        # Storage is untouched and no redirect was installed.
        assert storage.exists("docs/en/latest/index.html")
        assert not storage.exists("newdocs/en/latest/index.html")
        assert not models.Redirect.objects.filter(
            from_path="/docs", match_type=models.Redirect.MATCH_PREFIX
        ).exists()

    def test_stale_switched_migration_cannot_delete(self, storage):
        project = claimed("docs")
        storage.put_bytes("docs/en/latest/index.html", b"index")
        migration = models.PathMigration.objects.create(
            project=project,
            old_root="docs",
            new_root="newdocs",
            key_mapping={
                "docs/en/latest/index.html": "newdocs/en/latest/index.html"
            },
            status=models.PathMigration.STATUS_SWITCHED,
        )
        storage.copy_object("docs/en/latest/index.html", "newdocs/en/latest/index.html")
        # The project root was moved elsewhere after the switch.
        project.root_path = "elsewhere"
        project.save(update_fields=["root_path", "updated_at"])
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_root_migration(migration, storage)
        assert excinfo.value.status_code == 409
        assert "stale" in excinfo.value.detail
        # The recorded old keys were not deleted by the superseded migration.
        assert storage.exists("docs/en/latest/index.html")
        migration.refresh_from_db()
        assert migration.status == models.PathMigration.STATUS_SWITCHED
