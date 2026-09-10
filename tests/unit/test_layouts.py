"""Unit tests for the four URL layouts and the layout-toggle state machine."""

from __future__ import annotations

import pytest
from conftest import register_build

from doc_hosting.registry import models, services

pytestmark = pytest.mark.usefixtures("aws")


def claimed(root="docs", secret="project-secret"):
    """Claim a root path directly through the services."""
    register_build(root_path=root, project_secret=secret)
    return models.Project.objects.get(root_path=root)


class TestFourLayoutsServe:
    def test_language_and_version_layout(self, client, storage):
        claimed()
        storage.put_bytes("docs/en/latest/index.html", b"both dims")
        assert b"both dims" in client.get("/docs/en/latest/").content
        assert client.get("/docs/en/latest", follow_redirects=False).status_code == 307

    def test_language_only_layout(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"content")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        storage.put_bytes("docs/en/index.html", b"lang only")
        assert b"lang only" in client.get("/docs/en/").content
        assert client.get("/docs/en", follow_redirects=False).status_code == 307
        # The old-layout URL redirects to the new location (301).
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/en/"

    def test_version_only_layout(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"content")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        storage.put_bytes("docs/latest/index.html", b"ver only")
        assert b"ver only" in client.get("/docs/latest/").content
        assert client.get("/docs/latest", follow_redirects=False).status_code == 307
        # The old language dimension URL redirects to the new location.
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/latest/"

    def test_root_only_layout(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"content")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        storage.put_bytes("docs/index.html", b"no dims")
        assert b"no dims" in client.get("/docs/").content
        assert client.get("/docs", follow_redirects=False).status_code == 307
        # Both old dimension URLs redirect to the new location.
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/"


class TestToggleRekeying:
    def test_disable_version_rekeys_s3_and_deletes_old_keys(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        assert storage.exists("docs/en/index.html")
        assert storage.exists("docs/en/usage/index.html")
        assert not storage.exists("docs/en/latest/index.html")
        assert not storage.exists("docs/en/latest/usage/index.html")
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, False)
        assert project.version_label == "latest"

    def test_enable_language_assigns_operator_label(self, storage):
        project = claimed()
        # Content placed while the language dimension is enabled: the
        # disable transition validates it and records its membership.
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        services.toggle_project_layout(
            project,
            language_enabled=True,
            version_enabled=True,
            language_label="de",
            storage=storage,
        )
        assert storage.exists("docs/de/latest/index.html")
        assert not storage.exists("docs/latest/index.html")
        project.refresh_from_db()
        assert project.language_label == ""

    def test_enable_language_defaults_to_stored_then_fallback_label(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        project.refresh_from_db()
        assert project.language_label == "en"  # sole surviving label is stored
        # No operator label: the stored sole label is reused.
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        assert storage.exists("docs/en/latest/index.html")

    def test_toggle_to_same_layout_conflicts(self, storage):
        project = claimed()
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=True, storage=storage
            )
        assert excinfo.value.status_code == 409

    def test_disable_dimension_with_ambiguous_labels_conflicts(self, storage):
        register_build(language="en", commit_hash="a", domain="d")
        register_build(language="fr", commit_hash="b", domain="d")
        project = models.Project.objects.get(root_path="docs")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=False, version_enabled=True, storage=storage
            )
        assert excinfo.value.status_code == 409

    def test_disable_dimension_with_stray_subtree_conflicts(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        # A stray object already sits at the destination of the re-keying.
        storage.put_bytes("docs/latest/index.html", b"stray")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=False, version_enabled=True, storage=storage
            )
        assert excinfo.value.status_code == 409
        project.refresh_from_db()
        assert project.language_enabled is True

    def test_disable_dimension_with_non_colliding_stray_keys_conflicts(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        # A stray object in the old layout that does NOT collide with any
        # re-keying destination still blocks disabling the dimension.
        storage.put_bytes("docs/en/other/index.html", b"stray")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=False, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "docs/en/other/index.html" in excinfo.value.detail
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)
        # The stray object is untouched and still served by the old layout.
        assert storage.exists("docs/en/other/index.html")

    def test_enable_dimension_with_stray_keys_conflicts(self, storage):
        project = claimed()
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        storage.put_bytes("docs/latest/index.html", b"moved")
        # A stray version segment (unpublished) in the old layout.
        storage.put_bytes("docs/other/index.html", b"stray")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=True, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "docs/other/index.html" in excinfo.value.detail
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (False, True)
        # Stray objects are never moved or deleted by the rejected toggle.
        assert storage.exists("docs/other/index.html")
        assert storage.exists("docs/latest/index.html")
        assert not storage.exists("docs/en/latest/index.html")

    def test_toggle_preserves_redirects_transactionally(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")
        services.create_redirect(
            "/docs/en/latest/guide",
            "/docs/en/latest/usage",
            match_type=models.Redirect.MATCH_EXACT,
            project=project,
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        redirect = models.Redirect.objects.get(
            match_type=models.Redirect.MATCH_EXACT
        )
        assert redirect.from_path == "/docs/en/guide"
        assert redirect.to_path == "/docs/en/usage"
        # The redirect still resolves to the re-keyed content.
        assert services.resolve_redirect("/docs/en/guide") == "/docs/en/usage"

    def test_publications_survive_and_are_audited(self, storage):
        project = claimed()
        change = services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        assert change.status == models.LayoutChange.STATUS_COMPLETED
        assert change.completed_at is not None
        project.refresh_from_db()
        assert project.publications.count() == 1
        event_types = set(
            models.AuditEvent.objects.values_list("event_type", flat=True)
        )
        assert {
            "layout.change_requested",
            "layout.changed",
            "layout.change_completed",
        } <= event_types

    def test_layout_appears_in_versions_api(self, client, storage):
        project = claimed()
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        response = client.get("/api/v1/versions", params={"root_path": "docs"})
        assert response.json()["layout"] == {
            "language_enabled": False,
            "version_enabled": False,
            "language_label": "en",
            "version_label": "latest",
        }


class TestLayoutRedirects:
    def test_disable_creates_old_layout_prefix_redirect(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        redirect = models.Redirect.objects.get(
            from_path="/docs/en/latest", match_type=models.Redirect.MATCH_PREFIX
        )
        assert redirect.to_path == "/docs/en"
        assert redirect.project == project
        # Old URLs 301 to their new location, deep suffixes preserved.
        response = client.get("/docs/en/latest/usage/x.html", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/en/usage/x.html"

    def test_enable_creates_downward_redirect_without_retriggering(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        # The old (language-only) URL redirects downward to the new layout.
        response = client.get("/docs/en/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/en/latest/"
        # The new-layout URL serves directly (downward self-exclusion, no
        # re-triggering loop).
        assert b"index" in client.get("/docs/en/latest/").content
        assert services.resolve_redirect("/docs/en/latest/index.html") is None

    def test_root_only_to_dimensions_redirects_from_the_root(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        storage.put_bytes("docs/index.html", b"root index")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        response = client.get("/docs/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/en/latest/"
        # Requests already inside the new layout are served, not re-triggered.
        assert b"index" in client.get("/docs/en/latest/").content

    def test_disable_language_redirects_old_urls(self, client, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        response = client.get("/docs/en/latest/", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/docs/latest/"

    def test_redirect_rewrite_collision_is_actionable_409(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.create_redirect(
            "/docs/en/latest/guide",
            "/docs/en/latest/usage",
            match_type=models.Redirect.MATCH_EXACT,
            project=project,
        )
        # Another exact redirect already occupies the rewritten source.
        services.create_redirect(
            "/docs/en/guide", "/elsewhere", match_type=models.Redirect.MATCH_EXACT
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=False, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "/docs/en/guide" in excinfo.value.detail
        # Transaction rollback: the layout is untouched and the change is
        # pending (retryable once the colliding redirect is removed).
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)
        change = models.LayoutChange.objects.get()
        assert change.status == models.LayoutChange.STATUS_PENDING
        models.Redirect.objects.get(
            from_path="/docs/en/latest/guide", match_type=models.Redirect.MATCH_EXACT
        ).delete()
        services.invalidate_redirect_cache()
        services.run_layout_change(change, storage)
        change.refresh_from_db()
        assert change.status == models.LayoutChange.STATUS_COMPLETED

    def test_layout_redirect_creation_collision_is_actionable_409(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        # A prefix redirect with a different destination sits exactly where
        # the layout change would install its old-layout redirect.
        services.create_redirect(
            "/docs/en/latest",
            "/elsewhere",
            match_type=models.Redirect.MATCH_PREFIX,
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=False, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "/docs/en/latest" in excinfo.value.detail
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)
        change = models.LayoutChange.objects.get()
        assert change.status == models.LayoutChange.STATUS_PENDING
        models.Redirect.objects.get(
            from_path="/docs/en/latest", match_type=models.Redirect.MATCH_PREFIX
        ).delete()
        services.invalidate_redirect_cache()
        services.run_layout_change(change, storage)
        change.refresh_from_db()
        assert change.status == models.LayoutChange.STATUS_COMPLETED
        assert models.Redirect.objects.filter(
            from_path="/docs/en/latest",
            match_type=models.Redirect.MATCH_PREFIX,
            to_path="/docs/en",
        ).exists()


class TestToggleRetry:
    def test_retry_after_partial_copy(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")

        real_copy = storage.copy_object
        calls = {"n": 0}

        def flaky_copy(source, destination):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("boom mid-copy")
            real_copy(source, destination)

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(storage, "copy_object", flaky_copy)
            with pytest.raises(OSError):
                services.toggle_project_layout(
                    project,
                    language_enabled=True,
                    version_enabled=False,
                    storage=storage,
                )
        change = models.LayoutChange.objects.get()
        assert change.status == models.LayoutChange.STATUS_PENDING
        # The metadata switch has not happened: layout is untouched.
        project.refresh_from_db()
        assert project.version_enabled is True

        services.run_layout_change(change, storage)
        change.refresh_from_db()
        assert change.status == models.LayoutChange.STATUS_COMPLETED
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, False)
        assert storage.exists("docs/en/index.html")
        assert not storage.exists("docs/en/latest/index.html")

    def test_retry_after_switch_before_delete(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")

        def failing_delete(key):
            raise OSError("boom on delete")

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(storage, "delete_object", failing_delete)
            with pytest.raises(OSError):
                services.toggle_project_layout(
                    project,
                    language_enabled=True,
                    version_enabled=False,
                    storage=storage,
                )
        change = models.LayoutChange.objects.get()
        assert change.status == models.LayoutChange.STATUS_SWITCHED
        project.refresh_from_db()
        assert project.version_enabled is False
        assert storage.exists("docs/en/index.html")
        assert storage.exists("docs/en/latest/index.html")

        services.run_layout_change(change, storage)
        change.refresh_from_db()
        assert change.status == models.LayoutChange.STATUS_COMPLETED
        assert storage.exists("docs/en/index.html")
        assert not storage.exists("docs/en/latest/index.html")


class TestRepeatedLayoutTransitions:
    """Layout redirects across repeated toggles stay valid and phantom-free."""

    def claimed_with_content(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")
        return project

    def test_full_cycle_toggles_keep_redirects_valid(self, client, storage):
        project = self.claimed_with_content(storage)
        # (T,T) -> (T,F) -> (F,F) -> (T,T): every prefix ever used keeps
        # resolving to the current layout, without phantom paths.
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        for redirect in models.Redirect.objects.all():
            for path in (redirect.from_path, redirect.to_path):
                assert "/latest/latest" not in path
                assert "/latest/en" not in path
                assert "/en/latest/en" not in path
        # The new-layout URLs serve directly (no redirect loop).
        assert b"index" in client.get("/docs/en/latest/").content
        assert b"usage" in client.get("/docs/en/latest/usage/").content
        # Old-layout URLs chain to the current locations.
        assert (
            client.get("/docs/", follow_redirects=False).headers["location"]
            == "/docs/en/latest/"
        )
        assert (
            client.get("/docs/en/", follow_redirects=False).headers["location"]
            == "/docs/en/latest/"
        )
        assert b"index" in client.get("/docs/en/latest/", follow_redirects=True).content

    def test_oscillating_toggles_replace_layout_redirects(self, client, storage):
        project = self.claimed_with_content(storage)
        for _ in range(3):
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=False, storage=storage
            )
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=True, storage=storage
            )
        # Only the current transition's redirect survives the oscillation.
        prefix_redirects = models.Redirect.objects.filter(
            match_type=models.Redirect.MATCH_PREFIX
        )
        assert [(r.from_path, r.to_path) for r in prefix_redirects] == [
            ("/docs/en", "/docs/en/latest")
        ]
        # Old and new URLs resolve without loops.
        assert services.resolve_redirect("/docs/en/latest/usage/x.html") is None
        assert (
            services.resolve_redirect("/docs/en/usage/x.html")
            == "/docs/en/latest/usage/x.html"
        )
        assert b"index" in client.get("/docs/en/latest/").content

    def test_repeated_toggles_preserve_and_remap_manual_redirects(self, storage):
        project = self.claimed_with_content(storage)
        services.create_redirect(
            "/docs/en/latest/guide",
            "/docs/en/latest/usage",
            match_type=models.Redirect.MATCH_EXACT,
            project=project,
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        # The manual redirect survives every transition and ends up remapped
        # to the current layout (never deleted, never phantom).
        manual = models.Redirect.objects.get(match_type=models.Redirect.MATCH_EXACT)
        assert manual.from_path == "/docs/en/latest/guide"
        assert manual.to_path == "/docs/en/latest/usage"
        assert (
            services.resolve_redirect("/docs/en/latest/guide") == "/docs/en/latest/usage"
        )

    def test_publication_without_content_creates_no_redirect(self, storage):
        # A publication row with no storage under the old layout produces
        # no layout redirect: pairs come only from actually mapped keys.
        project = claimed()
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        assert not models.Redirect.objects.exists()


class TestLayoutChangeGuards:
    """One non-completed change per project; stale changes never apply."""

    def test_toggle_while_change_pending_conflicts(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        models.LayoutChange.objects.create(
            project=project,
            old_language_enabled=True,
            old_version_enabled=True,
            new_language_enabled=True,
            new_version_enabled=False,
            language_label="",
            version_label="latest",
            key_mapping={"docs/en/latest/index.html": "docs/en/index.html"},
            status=models.LayoutChange.STATUS_PENDING,
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=False, version_enabled=False, storage=storage
            )
        assert excinfo.value.status_code == 409
        # No second change row was created.
        assert models.LayoutChange.objects.count() == 1
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)

    def test_stale_change_cannot_touch_storage(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        stale = models.LayoutChange.objects.create(
            project=project,
            old_language_enabled=True,
            old_version_enabled=False,  # never was the project's layout
            new_language_enabled=True,
            new_version_enabled=False,
            language_label="",
            version_label="latest",
            key_mapping={"docs/en/latest/index.html": "docs/en/zz/index.html"},
            status=models.LayoutChange.STATUS_PENDING,
        )
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_layout_change(stale, storage)
        assert excinfo.value.status_code == 409
        assert "stale" in excinfo.value.detail
        # The stale change was not applied: nothing was copied or switched.
        assert storage.exists("docs/en/latest/index.html")
        assert not storage.exists("docs/en/zz/index.html")
        stale.refresh_from_db()
        assert stale.status == models.LayoutChange.STATUS_PENDING
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)

    def test_resume_with_missing_source_is_actionable_409(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        pending = models.LayoutChange.objects.create(
            project=project,
            old_language_enabled=True,
            old_version_enabled=True,
            new_language_enabled=True,
            new_version_enabled=False,
            language_label="",
            version_label="latest",
            key_mapping={"docs/en/latest/index.html": "docs/en/index.html"},
            status=models.LayoutChange.STATUS_PENDING,
        )
        # The recorded source key vanished from storage after the request.
        storage.delete_object("docs/en/latest/index.html")
        with pytest.raises(services.ServiceError) as excinfo:
            services.run_layout_change(pending, storage)
        assert excinfo.value.status_code == 409
        assert "docs/en/latest/index.html" in excinfo.value.detail
        # No destructive continuation: nothing was copied, switched or saved.
        pending.refresh_from_db()
        assert pending.status == models.LayoutChange.STATUS_PENDING
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)
        assert not storage.exists("docs/en/index.html")


class TestEnableMembershipProof:
    """Enabling a disabled dimension demands provable publication membership.

    Old-layout paths carry no segment for a disabled dimension, so label
    checks are blind there: arbitrary objects under the root would be
    re-keyed into the publication tree. Only content with recorded
    re-keying lineage (label-validated by a prior transition) may move.
    """

    def test_enable_from_root_only_with_stray_key_conflicts_then_retries(
        self, storage
    ):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        # An arbitrary object under the root is not attributable to any
        # publication: enabling a dimension would re-key (and publish) it.
        storage.put_bytes("docs/attacker/x.html", b"evil")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=False, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "docs/attacker/x.html" in excinfo.value.detail
        # Nothing moved or switched; the operation stays retryable.
        assert storage.exists("docs/attacker/x.html")
        assert storage.exists("docs/index.html")
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (False, False)
        # After removing the stray, the enable succeeds.
        storage.delete_object("docs/attacker/x.html")
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        assert storage.exists("docs/en/index.html")
        assert not storage.exists("docs/index.html")
        assert not storage.exists("docs/attacker/x.html")

    def test_enable_language_with_unattributable_publication_key_conflicts(
        self, storage
    ):
        # Reviewer example: version-only layout, sole published version
        # 'latest'; an arbitrary object inside the publication prefix is
        # structurally indistinguishable from real content.
        register_build(root_path="d")
        project = models.Project.objects.get(root_path="d")
        storage.put_bytes("d/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        storage.put_bytes("d/latest/attacker/x.html", b"evil")
        with pytest.raises(services.ServiceError) as excinfo:
            services.toggle_project_layout(
                project, language_enabled=True, version_enabled=True, storage=storage
            )
        assert excinfo.value.status_code == 409
        assert "d/latest/attacker/x.html" in excinfo.value.detail
        # The unattributable object is not moved into the publication tree.
        assert storage.exists("d/latest/attacker/x.html")
        assert not storage.exists("d/en/latest/attacker/x.html")
        # The provable content is untouched too: the layout is unchanged.
        assert storage.exists("d/latest/index.html")
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (False, True)

    def test_enable_with_lineage_proven_content_succeeds(self, storage):
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        storage.put_bytes("docs/en/latest/usage/index.html", b"usage")
        # Disable: the transition validates the labels and records the
        # flattened keys as publication content.
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        # Re-enable: the recorded lineage proves membership.
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        assert storage.exists("docs/en/latest/index.html")
        assert storage.exists("docs/en/latest/usage/index.html")
        assert not storage.exists("docs/en/index.html")

    def test_enable_with_empty_storage_is_trivially_provable(self, storage):
        project = claimed()
        # No objects under the root: nothing can be smuggled, so enabling
        # needs no lineage proof.
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=True, storage=storage
        )
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=True, storage=storage
        )
        project.refresh_from_db()
        assert (project.language_enabled, project.version_enabled) == (True, True)

    def test_enable_after_root_migration_keeps_lineage(self, storage):
        # Lineage survives a root migration: the migrated destination
        # inherits the membership proof of its recorded source key.
        project = claimed()
        storage.put_bytes("docs/en/latest/index.html", b"index")
        services.toggle_project_layout(
            project, language_enabled=False, version_enabled=False, storage=storage
        )
        migration = services.create_root_migration(project, "newdocs")
        services.run_root_migration(migration, storage)
        services.toggle_project_layout(
            project, language_enabled=True, version_enabled=False, storage=storage
        )
        assert storage.exists("newdocs/en/index.html")
        assert not storage.exists("newdocs/index.html")
