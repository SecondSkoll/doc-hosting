"""Unit tests for the Django bootstrap: settings parsing and fail-closed config."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from doc_hosting import db
from doc_hosting.django_project import settings as django_settings


class TestPostgresUrlParsing:
    def test_parses_a_full_url(self):
        config = django_settings.parse_postgres_url(
            "postgresql://user:p%40ss@postgres.example.com:5432/docdb"
        )
        assert config == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "docdb",
            "USER": "user",
            "PASSWORD": "p@ss",
            "HOST": "postgres.example.com",
            "PORT": "5432",
        }

    def test_parses_without_port_or_credentials(self):
        config = django_settings.parse_postgres_url("postgres://localhost/dbname")
        assert config["NAME"] == "dbname"
        assert config["HOST"] == "localhost"
        assert config["PORT"] == ""
        assert config["USER"] == ""
        assert config["PASSWORD"] == ""


class TestDatabasesFromEnv:
    def test_postgresql_connect_string_wins(self, monkeypatch):
        monkeypatch.setenv(
            "POSTGRESQL_DB_CONNECT_STRING", "postgresql://u:p@h:5432/db"
        )
        monkeypatch.delenv("DATABASE_URL", raising=False)
        databases = django_settings._databases_from_env()
        assert databases["default"]["ENGINE"] == "django.db.backends.postgresql"

    def test_database_url_fallback(self, monkeypatch):
        monkeypatch.delenv("POSTGRESQL_DB_CONNECT_STRING", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
        databases = django_settings._databases_from_env()
        assert databases["default"]["ENGINE"] == "django.db.backends.postgresql"

    def test_sqlite_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("POSTGRESQL_DB_CONNECT_STRING", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("DOC_HOSTING_SQLITE_PATH", str(tmp_path / "x.sqlite3"))
        databases = django_settings._databases_from_env()
        assert databases["default"]["ENGINE"] == "django.db.backends.sqlite3"
        assert databases["default"]["NAME"].endswith("x.sqlite3")


class TestSecretKey:
    def test_app_secret_key_env_wins(self, monkeypatch):
        monkeypatch.setenv("APP_SECRET_KEY", "charm-injected")
        monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        assert django_settings._secret_key() == "charm-injected"

    def test_django_secret_key_is_honoured(self, monkeypatch):
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        monkeypatch.setenv("DJANGO_SECRET_KEY", "ops-provided")
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        assert django_settings._secret_key() == "ops-provided"

    def test_fails_closed_outside_dev_mode(self, monkeypatch):
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
            django_settings._secret_key()

    def test_dev_mode_must_be_explicit_and_truthy(self, monkeypatch):
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)
        monkeypatch.delenv("DJANGO_SECRET_KEY", raising=False)
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("DOC_HOSTING_DEV", value)
            with pytest.raises(ImproperlyConfigured):
                django_settings._secret_key()
        monkeypatch.setenv("DOC_HOSTING_DEV", "1")
        assert (
            django_settings._secret_key()
            == "doc-hosting-insecure-development-secret-key"
        )


class TestAllowedHosts:
    def test_configured_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_ALLOWED_HOSTS", "docs.example.com, api.example.com")
        monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        assert django_settings._allowed_hosts() == [
            "docs.example.com",
            "api.example.com",
        ]

    def test_allowed_hosts_env_name_is_honoured(self, monkeypatch):
        monkeypatch.delenv("APP_ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("ALLOWED_HOSTS", "docs.example.com")
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        assert django_settings._allowed_hosts() == ["docs.example.com"]

    def test_unconfigured_fails_closed_outside_dev_mode(self, monkeypatch):
        monkeypatch.delenv("APP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("DOC_HOSTING_DEV", raising=False)
        assert django_settings._allowed_hosts() == []

    def test_dev_mode_permits_the_local_test_fallback(self, monkeypatch):
        monkeypatch.delenv("APP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
        monkeypatch.setenv("DOC_HOSTING_DEV", "1")
        assert django_settings._allowed_hosts() == ["*"]


def test_ensure_admin_user_no_op_without_config(django_db, monkeypatch):
    """Neither admin variable set: no user is created."""
    from django.contrib.auth import get_user_model

    monkeypatch.delenv("APP_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("APP_ADMIN_PASSWORD", raising=False)
    db.ensure_admin_user()
    assert get_user_model().objects.count() == 0


def test_ensure_admin_user_requires_both_values(django_db, monkeypatch):
    """Exactly one admin variable set: provisioning fails, no user created."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv("APP_ADMIN_USERNAME", "ops")
    monkeypatch.delenv("APP_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ImproperlyConfigured, match="APP_ADMIN_PASSWORD"):
        db.ensure_admin_user()
    assert get_user_model().objects.count() == 0

    monkeypatch.delenv("APP_ADMIN_USERNAME", raising=False)
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "secret-pass")
    with pytest.raises(ImproperlyConfigured, match="APP_ADMIN_USERNAME"):
        db.ensure_admin_user()
    assert get_user_model().objects.count() == 0


def test_ensure_admin_user_creates_superuser(django_db, monkeypatch):
    """Both admin variables set: a superuser with the password exists."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv("APP_ADMIN_USERNAME", "ops")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "secret-pass")
    db.ensure_admin_user()
    user = get_user_model().objects.get(username="ops")
    assert user.is_staff is True
    assert user.is_superuser is True
    assert user.check_password("secret-pass")


def test_ensure_admin_user_never_resets_existing_password(django_db, monkeypatch):
    """Creation-only idempotency: an existing user's password is kept."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv("APP_ADMIN_USERNAME", "ops")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "secret-pass")
    db.ensure_admin_user()
    user = get_user_model().objects.get(username="ops")
    # Simulate a password change made through the admin interface.
    user.set_password("changed-in-admin")
    user.save()

    db.ensure_admin_user()

    user.refresh_from_db()
    assert user.check_password("changed-in-admin")
    assert not user.check_password("secret-pass")


def test_ensure_database_ready_runs_admin_provisioning(django_db, monkeypatch):
    """ensure_database_ready() provisions the superuser at startup."""
    from django.contrib.auth import get_user_model

    # ensure_database_ready() is once per process; reset the flag so the
    # bootstrap (migrate + provisioning) runs again inside this test.
    monkeypatch.setattr(db, "_ready", False)
    monkeypatch.setenv("APP_ADMIN_USERNAME", "ops")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "secret-pass")
    db.ensure_database_ready()
    user = get_user_model().objects.get(username="ops")
    assert user.is_superuser is True
    assert user.check_password("secret-pass")
