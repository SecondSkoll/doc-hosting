"""Django admin for the doc-hosting control plane, mounted at ``/manage/``.

Standard staff-only, CSRF-protected Django admin.  All mutations run
through the control-plane services so the ownership, redirect, layout and
migration rules are enforced everywhere; the project secret is rotated
through a write-only form field and audit events are strictly read-only.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse

from .. import paths
from ..settings import SettingsError, get_settings
from ..storage import S3Storage
from . import models, services


def _storage(request) -> S3Storage | None:
    """Return an S3 storage client or ``None`` after reporting the problem."""
    try:
        return S3Storage(get_settings())
    except SettingsError as exc:
        messages.error(request, f"S3 storage is not configured: {exc}")
        return None


class ServiceBackedAdmin(admin.ModelAdmin):
    """Admin base whose add operations may fail inside the services.

    When a service rejects an operation during ``save_model``, the error is
    reported through the messages framework and the (unchanged) object is
    not saved; the add response then redirects to the changelist instead of
    the (never created) object page.
    """

    def _service_failed_on_add(self, request) -> None:
        request._service_failed_on_add = True  # type: ignore[attr-defined]

    def response_add(self, request, obj, post_url_continue=None):
        if getattr(request, "_service_failed_on_add", False):
            return HttpResponseRedirect(
                reverse(
                    f"admin:{obj._meta.app_label}_{obj._meta.model_name}_changelist"
                )
            )
        return super().response_add(request, obj, post_url_continue)


class ProjectAdminForm(forms.ModelForm):
    """Project form with a write-only shared-secret rotation field."""

    rotate_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Write-only: rotate the shared project secret (never displayed).",
    )

    class Meta:
        model = models.Project
        fields = ("root_path", "domain")

    def clean_root_path(self) -> str:
        return paths.normalize_root_path(self.cleaned_data["root_path"])


@admin.register(models.Project)
class ProjectAdmin(admin.ModelAdmin):
    """Inspect projects, rotate their shared secret, and manage layout flags.

    Direct creation is disallowed: projects are claimed through the
    ingestion API (or adopted by the legacy importer), never hand-made.
    """

    form = ProjectAdminForm
    list_display = (
        "root_path",
        "domain",
        "language_enabled",
        "version_enabled",
        "secret_claimed",
        "updated_at",
    )
    list_filter = ("language_enabled", "version_enabled")
    search_fields = ("root_path", "domain")
    readonly_fields = (
        "language_enabled",
        "version_enabled",
        "language_label",
        "version_label",
        "secret_claimed",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ("secret_claimed",)
        # Root path moves must go through the migration flow; layout flags
        # must go through layout changes (both re-key S3 content).
        return (
            "root_path",
            "language_enabled",
            "version_enabled",
            "language_label",
            "version_label",
            "secret_claimed",
            "created_at",
            "updated_at",
        )

    def save_model(self, request, obj, form, change):
        secret = form.cleaned_data.get("rotate_secret")
        with transaction.atomic():
            obj.save()
            if secret:
                obj.set_secret(secret)
                obj.save(update_fields=["secret_hash", "updated_at"])
                services.record_audit(
                    obj, "project.secret_rotated", {"root_path": obj.root_path}
                )

    def delete_model(self, request, obj):
        with transaction.atomic():
            services.record_audit(None, "project.deleted", {"root_path": obj.root_path})
            obj.delete()

    def secret_claimed(self, obj: models.Project) -> bool:
        return obj.secret_claimed

    secret_claimed.boolean = True
    secret_claimed.short_description = "secret claimed"


@admin.register(models.Publication)
class PublicationAdmin(admin.ModelAdmin):
    """Inspect and delete the current published builds.

    Publications are owned by the ingestion API: direct add and edit are
    disallowed, while inspection (view-only) and deletion are retained.
    """

    list_display = (
        "project",
        "language",
        "version",
        "commit_hash",
        "registered_at",
    )
    list_filter = ("language", "version")
    search_fields = ("project__root_path", "commit_hash")

    actions = ("delete_with_audit",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        # Inspection is retained for existing publications (rendered
        # read-only because has_change_permission is False).
        return True

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_with_audit(self, request, queryset):
        count = 0
        for publication in queryset:
            with transaction.atomic():
                project = publication.project
                publication.delete()
                services.record_audit(
                    project,
                    "publication.deleted",
                    {
                        "language": publication.language,
                        "version": publication.version,
                    },
                )
                count += 1
        self.message_user(request, f"Deleted {count} publication(s) with audit.")

    delete_with_audit.short_description = "Delete selected publications (audited)"

    def delete_model(self, request, obj):
        with transaction.atomic():
            project = obj.project
            obj.delete()
            services.record_audit(
                project,
                "publication.deleted",
                {"language": obj.language, "version": obj.version},
            )


@admin.register(models.UploadSession)
class UploadSessionAdmin(admin.ModelAdmin):
    """Inspect direct-upload sessions (view-only; the API owns them)."""

    list_display = (
        "project",
        "language",
        "version",
        "commit_hash",
        "key_prefix",
        "status",
        "created_at",
        "expires_at",
        "completed_at",
    )
    list_filter = ("status",)
    search_fields = ("project__root_path", "commit_hash", "key_prefix")
    readonly_fields = (
        "project",
        "language",
        "version",
        "commit_hash",
        "domain",
        "key_prefix",
        "manifest",
        "status",
        "expires_at",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return True


@admin.register(models.Redirect)
class RedirectAdmin(ServiceBackedAdmin):
    """Manage redirects through the validation and audit services."""

    list_display = (
        "from_path",
        "to_path",
        "match_type",
        "enabled",
        "project",
        "updated_at",
    )
    list_filter = ("match_type", "enabled")
    search_fields = ("from_path", "to_path")
    fields = ("match_type", "from_path", "to_path", "enabled", "project")

    def save_model(self, request, obj, form, change):
        try:
            if change:
                services.update_redirect(
                    obj,
                    from_path=obj.from_path,
                    to_path=obj.to_path,
                    match_type=obj.match_type,
                    enabled=obj.enabled,
                    project=obj.project,
                )
                super().save_model(request, obj, form, change)
                return
            created = services.create_redirect(
                obj.from_path,
                obj.to_path,
                match_type=obj.match_type,
                project=obj.project,
                enabled=obj.enabled,
            )
        except services.ServiceError as exc:
            self.message_user(request, f"Redirect not saved: {exc}", messages.ERROR)
            self._service_failed_on_add(request)
            return
        form.instance = created
        super().save_model(request, created, form, change)

    def delete_model(self, request, obj):
        try:
            services.delete_redirect(obj)
        except services.ServiceError as exc:
            self.message_user(request, f"Redirect not deleted: {exc}", messages.ERROR)

    def delete_queryset(self, request, queryset):
        for redirect in queryset:
            services.delete_redirect(redirect)


class PathMigrationForm(forms.ModelForm):
    """Creation form for a root migration request."""

    class Meta:
        model = models.PathMigration
        fields = ("project", "new_root")

    def clean_new_root(self) -> str:
        return paths.normalize_root_path(self.cleaned_data["new_root"])


@admin.register(models.PathMigration)
class PathMigrationAdmin(ServiceBackedAdmin):
    """Run or resume root migrations (copy -> switch -> delete)."""

    form = PathMigrationForm
    list_display = (
        "project",
        "old_root",
        "new_root",
        "status",
        "created_at",
        "completed_at",
    )
    list_filter = ("status",)
    search_fields = ("old_root", "new_root", "project__root_path")
    actions = ("resume_migrations",)
    fields = (
        "project",
        "new_root",
        "old_root",
        "key_mapping",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ("old_root", "status", "created_at", "updated_at", "completed_at")
        # The operation-defining fields are frozen once the migration
        # exists: resuming only replays the recorded operation.
        return (
            "project",
            "old_root",
            "new_root",
            "key_mapping",
            "status",
            "created_at",
            "updated_at",
            "completed_at",
        )

    def save_model(self, request, obj, form, change):
        storage = _storage(request)
        if storage is None:
            return
        if change:
            try:
                services.run_root_migration(obj, storage)
            except services.ServiceError as exc:
                self.message_user(request, f"Migration failed: {exc}", messages.ERROR)
                return
            super().save_model(request, obj, form, change)
            return
        try:
            migration = services.create_root_migration(obj.project, obj.new_root)
            services.run_root_migration(migration, storage)
        except services.ServiceError as exc:
            self.message_user(request, f"Migration failed: {exc}", messages.ERROR)
            self._service_failed_on_add(request)
            return
        form.instance = migration
        super().save_model(request, migration, form, change)

    def resume_migrations(self, request, queryset):
        storage = _storage(request)
        if storage is None:
            return
        resumed = 0
        for migration in queryset.exclude(status=models.PathMigration.STATUS_COMPLETED):
            try:
                services.run_root_migration(migration, storage)
                resumed += 1
            except services.ServiceError as exc:
                self.message_user(
                    request, f"Migration {migration} failed: {exc}", messages.ERROR
                )
        self.message_user(request, f"Resumed/verified {resumed} migration(s).")

    resume_migrations.short_description = "Resume/verify selected migrations"


class LayoutChangeForm(forms.ModelForm):
    """Creation form for a layout toggle (dimensions, not aliases)."""

    new_language_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput,
        label="New language dimension enabled",
    )
    new_version_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput,
        label="New version dimension enabled",
    )

    class Meta:
        model = models.LayoutChange
        fields = (
            "project",
            "new_language_enabled",
            "new_version_enabled",
            "language_label",
            "version_label",
        )

    def clean_language_label(self) -> str:
        return self.cleaned_data.get("language_label", "").strip()

    def clean_version_label(self) -> str:
        return self.cleaned_data.get("version_label", "").strip()


@admin.register(models.LayoutChange)
class LayoutChangeAdmin(ServiceBackedAdmin):
    """Run or resume layout toggles (re-key -> switch -> delete)."""

    form = LayoutChangeForm
    list_display = (
        "project",
        "status",
        "language_label",
        "version_label",
        "created_at",
        "completed_at",
    )
    list_filter = ("status",)
    search_fields = ("project__root_path",)
    actions = ("resume_layout_changes",)
    fields = (
        "project",
        "new_language_enabled",
        "new_version_enabled",
        "language_label",
        "version_label",
        "old_language_enabled",
        "old_version_enabled",
        "key_mapping",
        "created_redirect_ids",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
    )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return (
                "old_language_enabled",
                "old_version_enabled",
                "status",
                "key_mapping",
                "created_redirect_ids",
                "created_at",
                "updated_at",
                "completed_at",
            )
        # The operation-defining fields are frozen once the change exists:
        # resuming only replays the recorded operation.
        return (
            "project",
            "new_language_enabled",
            "new_version_enabled",
            "language_label",
            "version_label",
            "old_language_enabled",
            "old_version_enabled",
            "key_mapping",
            "created_redirect_ids",
            "status",
            "created_at",
            "updated_at",
            "completed_at",
        )

    def save_model(self, request, obj, form, change):
        storage = _storage(request)
        if storage is None:
            return
        if change:
            try:
                services.run_layout_change(obj, storage)
            except services.ServiceError as exc:
                self.message_user(
                    request, f"Layout change failed: {exc}", messages.ERROR
                )
                return
            super().save_model(request, obj, form, change)
            return
        try:
            change_record = services.toggle_project_layout(
                obj.project,
                language_enabled=obj.new_language_enabled,
                version_enabled=obj.new_version_enabled,
                language_label=obj.language_label or None,
                version_label=obj.version_label or None,
                storage=storage,
            )
        except services.ServiceError as exc:
            self.message_user(request, f"Layout change failed: {exc}", messages.ERROR)
            self._service_failed_on_add(request)
            return
        form.instance = change_record
        super().save_model(request, change_record, form, change)

    def resume_layout_changes(self, request, queryset):
        storage = _storage(request)
        if storage is None:
            return
        resumed = 0
        for change in queryset.exclude(status=models.LayoutChange.STATUS_COMPLETED):
            try:
                services.run_layout_change(change, storage)
                resumed += 1
            except services.ServiceError as exc:
                self.message_user(
                    request, f"Layout change {change} failed: {exc}", messages.ERROR
                )
        self.message_user(request, f"Resumed/verified {resumed} layout change(s).")

    resume_layout_changes.short_description = "Resume/verify selected layout changes"


@admin.register(models.AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only view over the immutable audit history."""

    list_display = ("created_at", "event_type", "project_root")
    list_filter = ("event_type",)
    search_fields = ("project_root", "event_type")
    readonly_fields = ("created_at", "event_type", "project_root", "project", "payload")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
