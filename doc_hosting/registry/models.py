"""Control-plane models for the doc-hosting service.

* ``Project``: a claimed root path with its hashed shared secret and the
  active URL layout (which slug dimensions are enabled).
* ``Publication``: the mutable current build per (project, language,
  version); republishing upserts this single row.
* ``Redirect``: exact/prefix same-site redirects managed through services
  with loop, reserved-namespace and root-shadow protections.
* ``PathMigration``: retryable root migration state machine (copy ->
  transactional switch -> delete).
* ``LayoutChange``: retryable layout toggle state machine with the S3
  re-keying map.
* ``AuditEvent``: immutable, append-only history of every control-plane
  mutation; never updated or deleted by the application.
"""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.db import models
from django.utils import timezone


def _check_password(value: str, encoded: str) -> bool:
    if not encoded:
        return False
    from django.contrib.auth.hashers import check_password

    return check_password(value, encoded)


class Project(models.Model):
    """A documentation project claimed at a normalized root path."""

    root_path = models.CharField(max_length=512, unique=True, db_index=True)
    domain = models.CharField(max_length=255, blank=True, default="")
    # Salted, irreversible hash of the shared project secret (Django hasher
    # format).  Empty until the first authenticated publication claims the
    # root (or forever, when only the global bearer token is used).
    secret_hash = models.CharField(max_length=255, blank=True, default="")
    language_enabled = models.BooleanField(default=True)
    version_enabled = models.BooleanField(default=True)
    # Sole label used while the matching dimension is disabled.
    language_label = models.CharField(max_length=255, blank=True, default="")
    version_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["root_path"]

    def __repr__(self) -> str:
        return f"<Project {self.root_path}>"

    def __str__(self) -> str:
        return self.root_path

    def set_secret(self, secret: str) -> None:
        """Store a salted hash of the shared project secret."""
        if not secret:
            raise ValueError("project secret must not be empty")
        self.secret_hash = make_password(secret)

    def check_secret(self, secret: str) -> bool:
        """Return whether ``secret`` matches the stored project secret."""
        return _check_password(secret or "", self.secret_hash)

    @property
    def secret_claimed(self) -> bool:
        return bool(self.secret_hash)


class Publication(models.Model):
    """The current published build for one (language, version) pair."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="publications"
    )
    language = models.CharField(max_length=255)
    version = models.CharField(max_length=255)
    commit_hash = models.CharField(max_length=255)
    registered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "language", "version"], name="uniq_publication"
            )
        ]
        ordering = ["registered_at", "id"]

    def __repr__(self) -> str:
        return f"<Publication {self.project_id}:{self.language}/{self.version}>"

    def __str__(self) -> str:
        return f"{self.project.root_path}: {self.language}/{self.version}"


class Redirect(models.Model):
    """A same-site redirect: exact match or segment-boundary prefix."""

    MATCH_EXACT = "exact"
    MATCH_PREFIX = "prefix"
    MATCH_CHOICES = ((MATCH_EXACT, "exact"), (MATCH_PREFIX, "prefix"))

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="redirects",
        null=True,
        blank=True,
    )
    match_type = models.CharField(max_length=16, choices=MATCH_CHOICES, default=MATCH_EXACT)
    from_path = models.CharField(max_length=2048, db_index=True)
    to_path = models.CharField(max_length=2048)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["from_path", "match_type"], name="uniq_redirect"
            )
        ]
        ordering = ["from_path"]

    def __repr__(self) -> str:
        return f"<Redirect {self.match_type} {self.from_path} -> {self.to_path}>"

    def __str__(self) -> str:
        return f"{self.from_path} -> {self.to_path} ({self.match_type})"


class PathMigration(models.Model):
    """Root migration state machine (retryable copy -> switch -> delete)."""

    STATUS_PENDING = "pending"
    STATUS_SWITCHED = "switched"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "pending (copy/switch outstanding)"),
        (STATUS_SWITCHED, "switched (old keys pending deletion)"),
        (STATUS_COMPLETED, "completed"),
    )

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="path_migrations"
    )
    old_root = models.CharField(max_length=512)
    new_root = models.CharField(max_length=512)
    # {source_key: destination_key} snapshot of the S3 copy step, persisted
    # with the switch so retries of the delete step remove exactly the
    # copied old source keys (even for upward migrations where the new root
    # lives inside the old prefix).
    key_mapping = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __repr__(self) -> str:
        return f"<PathMigration {self.old_root} -> {self.new_root} ({self.status})>"

    def __str__(self) -> str:
        return f"{self.old_root} -> {self.new_root} ({self.status})"


class LayoutChange(models.Model):
    """Layout toggle state machine with the computed S3 re-keying map."""

    STATUS_PENDING = "pending"
    STATUS_SWITCHED = "switched"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "pending (copy/switch outstanding)"),
        (STATUS_SWITCHED, "switched (old keys pending deletion)"),
        (STATUS_COMPLETED, "completed"),
    )

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="layout_changes"
    )
    old_language_enabled = models.BooleanField()
    old_version_enabled = models.BooleanField()
    new_language_enabled = models.BooleanField()
    new_version_enabled = models.BooleanField()
    language_label = models.CharField(max_length=255, blank=True, default="")
    version_label = models.CharField(max_length=255, blank=True, default="")
    # {source_key: destination_key} snapshot computed when the change was
    # requested; retrying the change replays copies and deletions from it.
    key_mapping = models.JSONField(default=dict, blank=True)
    # IDs of the prefix redirects created so old-layout URLs redirect to
    # their new-layout locations; a later change replaces them.
    created_redirect_ids = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __repr__(self) -> str:
        old = _layout_name(self.old_language_enabled, self.old_version_enabled)
        new = _layout_name(self.new_language_enabled, self.new_version_enabled)
        return f"<LayoutChange {self.project_id}: {old} -> {new} ({self.status})>"

    def __str__(self) -> str:
        old = _layout_name(self.old_language_enabled, self.old_version_enabled)
        new = _layout_name(self.new_language_enabled, self.new_version_enabled)
        return f"{self.project.root_path}: {old} -> {new} ({self.status})"


def _layout_name(language_enabled: bool, version_enabled: bool) -> str:
    parts = []
    if language_enabled:
        parts.append("language")
    if version_enabled:
        parts.append("version")
    return "+".join(parts) if parts else "root-only"


class AuditEvent(models.Model):
    """Immutable, append-only history of control-plane operations."""

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    project_root = models.CharField(max_length=512, blank=True, default="")
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __repr__(self) -> str:
        return f"<AuditEvent {self.event_type} {self.project_root}>"

    def __str__(self) -> str:
        return f"{self.event_type}: {self.project_root or 'global'}"
