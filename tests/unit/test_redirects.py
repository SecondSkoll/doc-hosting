"""Unit tests for the redirect services: matching rules and protections."""

from __future__ import annotations

import pytest
from conftest import register_build

from doc_hosting.registry import models, services


@pytest.fixture()
def redirect_ready(project):
    """Provide a claimed ``docs`` project (some tests need its root registered)."""
    return project


def create(from_path, to_path, match_type=services.models.Redirect.MATCH_EXACT, **kwargs):
    return services.create_redirect(from_path, to_path, match_type=match_type, **kwargs)


def test_exact_redirect_matches_only_the_exact_path(client):
    create("/docs/en/latest/old", "/docs/en/latest/new")
    response = client.get("/docs/en/latest/old", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/docs/en/latest/new"
    # The trailing-slash variant does not match an exact redirect.
    assert client.get("/docs/en/latest/old/").status_code == 404


def test_exact_wins_over_prefix(client):
    create("/a/b", "/exact-target", match_type=models.Redirect.MATCH_PREFIX)
    create("/a/b", "/winner")
    assert services.resolve_redirect("/a/b") == "/winner"
    assert services.resolve_redirect("/a/b/c") == "/exact-target/c"


def test_longest_prefix_wins(client):
    create("/a", "/short", match_type=models.Redirect.MATCH_PREFIX)
    create("/a/b", "/long", match_type=models.Redirect.MATCH_PREFIX)
    assert services.resolve_redirect("/a/b/c") == "/long/c"
    assert services.resolve_redirect("/a/c") == "/short/c"


def test_prefix_suffix_preservation_including_trailing_slash(client):
    create("/old", "/new", match_type=models.Redirect.MATCH_PREFIX)
    assert services.resolve_redirect("/old") == "/new"
    assert services.resolve_redirect("/old/") == "/new/"
    assert services.resolve_redirect("/old/x/y") == "/new/x/y"
    # Original request case is preserved for the suffix.
    assert services.resolve_redirect("/Old/File.HTML") == "/new/File.HTML"


def test_prefix_matches_on_segment_boundaries(client):
    create("/docs", "/w", match_type=models.Redirect.MATCH_PREFIX)
    assert services.resolve_redirect("/docsx/en") is None
    assert services.resolve_redirect("/docs/en") == "/w/en"


def test_downward_redirect_self_exclusion(client):
    # A prefix redirect whose destination is inside its own source subtree
    # must not fire for requests already at or below the destination.
    create("/docs", "/docs/new", match_type=models.Redirect.MATCH_PREFIX)
    assert services.resolve_redirect("/docs/x") == "/docs/new/x"
    assert services.resolve_redirect("/docs/new/x") is None
    assert services.resolve_redirect("/docs/new") is None


def test_same_site_absolute_paths_only(client):
    for to_path in ("https://evil.example.com/x", "//evil.example.com/x", "relative"):
        with pytest.raises(services.ServiceError) as excinfo:
            create("/somewhere", to_path)
        assert excinfo.value.status_code == 422
    with pytest.raises(services.ServiceError) as excinfo:
        create("no-leading-slash", "/x")
    assert excinfo.value.status_code == 422


def test_reserved_namespace_is_rejected(client):
    for from_path in ("/api/x", "/manage", "/health", "/_registry/x"):
        with pytest.raises(services.ServiceError) as excinfo:
            create(from_path, "/docs")
        assert excinfo.value.status_code == 422
    with pytest.raises(services.ServiceError) as excinfo:
        create("/", "/docs", match_type=models.Redirect.MATCH_PREFIX)
    assert excinfo.value.status_code == 422


def test_registered_root_shadow_is_rejected(redirect_ready):
    with pytest.raises(services.ServiceError) as excinfo:
        create("/docs", "/elsewhere")
    assert excinfo.value.status_code == 409
    with pytest.raises(services.ServiceError) as excinfo:
        create("/docs", "/elsewhere", match_type=models.Redirect.MATCH_PREFIX)
    assert excinfo.value.status_code == 409
    # An unrelated prefix that does not capture the root is fine.
    create("/doc", "/docs", match_type=models.Redirect.MATCH_PREFIX)
    # A prefix capturing a nested registered root is rejected too.
    register_build(root_path="docs/guides")
    with pytest.raises(services.ServiceError) as excinfo:
        create("/docs/guides", "/elsewhere", match_type=models.Redirect.MATCH_PREFIX)
    assert excinfo.value.status_code == 409


def test_self_redirect_rejected(client):
    with pytest.raises(services.ServiceError) as excinfo:
        create("/a", "/a")
    assert excinfo.value.status_code == 422
    with pytest.raises(services.ServiceError) as excinfo:
        create("/a", "/a", match_type=models.Redirect.MATCH_PREFIX)
    assert excinfo.value.status_code == 422


def test_loop_rejected_at_creation(client):
    create("/a", "/b")
    with pytest.raises(services.ServiceError) as excinfo:
        create("/b", "/a")
    assert excinfo.value.status_code == 409


def test_creation_hop_budget_rejected(client):
    # Build a chain of 10 redirects in reverse so each creation passes.
    for index in range(10, 0, -1):
        create(f"/x{index}", f"/x{index + 1}")
    # Chaining one more redirect in front would need 11 hops: rejected.
    with pytest.raises(services.ServiceError) as excinfo:
        create("/x0", "/x1")
    assert excinfo.value.status_code == 409


def test_runtime_hop_budget_returns_508(client):
    # A chain deeper than the runtime budget, written directly to the ORM
    # (bypassing the creation guard): resolution reports a loop.
    models.Redirect.objects.bulk_create(
        models.Redirect(
            match_type=models.Redirect.MATCH_EXACT,
            from_path=f"/c{index}",
            to_path=f"/c{index + 1}",
        )
        for index in range(30)
    )
    services.invalidate_redirect_cache()
    response = client.get("/c0", follow_redirects=False)
    assert response.status_code == 508


def test_runtime_cycle_is_detected_as_a_revisit(client):
    # A two-redirect cycle written directly to the ORM (bypassing the
    # creation guard) is reported deterministically the moment a path is
    # revisited, instead of spinning through the hop budget.
    models.Redirect.objects.create(
        match_type=models.Redirect.MATCH_EXACT, from_path="/a1", to_path="/a2"
    )
    models.Redirect.objects.create(
        match_type=models.Redirect.MATCH_EXACT, from_path="/a2", to_path="/a1"
    )
    services.invalidate_redirect_cache()
    with pytest.raises(services.RedirectLoopError):
        services.resolve_redirect("/a1")
    response = client.get("/a1", follow_redirects=False)
    assert response.status_code == 508


def test_loop_validation_uses_fresh_database_rows(client, monkeypatch):
    # A redirect written by another process (straight to the database)
    # must be visible to loop validation even while the in-process TTL
    # resolution cache still holds an older (empty) snapshot.
    monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", "60")
    assert services.resolve_redirect("/b") is None  # warms the cache
    models.Redirect.objects.create(
        match_type=models.Redirect.MATCH_EXACT, from_path="/b", to_path="/a"
    )
    with pytest.raises(services.ServiceError) as excinfo:
        create("/a", "/b")
    assert excinfo.value.status_code == 409


def test_runtime_chain_resolution_follows_multiple_hops(client):
    create("/a", "/b")
    create("/b", "/c")
    assert services.resolve_redirect("/a") == "/c"


def test_cache_invalidation_on_update_and_delete(client):
    redirect = create("/a", "/b")
    assert services.resolve_redirect("/a") == "/b"
    services.update_redirect(redirect, to_path="/c")
    assert services.resolve_redirect("/a") == "/c"
    services.delete_redirect(redirect)
    assert services.resolve_redirect("/a") is None


def test_disabled_redirects_are_skipped(client):
    create("/a", "/b", enabled=False)
    assert services.resolve_redirect("/a") is None


def test_redirect_creation_is_audited(client):
    redirect = create("/a", "/b")
    assert models.AuditEvent.objects.filter(event_type="redirect.created").exists()
    services.update_redirect(redirect, to_path="/c")
    assert models.AuditEvent.objects.filter(event_type="redirect.updated").exists()
    services.delete_redirect(redirect)
    assert models.AuditEvent.objects.filter(event_type="redirect.deleted").exists()


def test_duplicate_redirect_conflicts(client):
    create("/a", "/b")
    with pytest.raises(services.ServiceError) as excinfo:
        create("/a", "/c")
    assert excinfo.value.status_code == 409
    # The same source with a different match type is a separate rule.
    create("/a", "/d", match_type=models.Redirect.MATCH_PREFIX)


class TestRedirectCacheTTL:
    def test_ttl_defaults_and_validates(self, monkeypatch):
        monkeypatch.delenv("DOC_HOSTING_REDIRECT_CACHE_TTL", raising=False)
        assert services._redirect_cache_ttl() == services.DEFAULT_REDIRECT_CACHE_TTL_SECONDS
        monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", "1.5")
        assert services._redirect_cache_ttl() == 1.5
        monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", "0")
        assert services._redirect_cache_ttl() == 0.0
        # Non-finite, negative or garbage values fall back to the default.
        for value in ("abc", "-1", "inf", "nan", ""):
            monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", value)
            assert services._redirect_cache_ttl() == (
                services.DEFAULT_REDIRECT_CACHE_TTL_SECONDS
            )

    def test_zero_ttl_sees_writes_from_other_processes(self, client, monkeypatch):
        # With a zero TTL the cache reloads on every resolution, so a
        # redirect written directly to the database (as another process
        # would) is picked up without an explicit invalidation.
        monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", "0")
        assert services.resolve_redirect("/late") is None
        models.Redirect.objects.create(
            match_type=models.Redirect.MATCH_EXACT,
            from_path="/late",
            to_path="/target",
        )
        assert services.resolve_redirect("/late") == "/target"

    def test_finite_ttl_bounds_staleness_and_converges(self, client, monkeypatch):
        # A finite TTL keeps recently loaded state (bounded staleness)...
        monkeypatch.setenv("DOC_HOSTING_REDIRECT_CACHE_TTL", "60")
        assert services.resolve_redirect("/late") is None
        models.Redirect.objects.create(
            match_type=models.Redirect.MATCH_EXACT,
            from_path="/late",
            to_path="/target",
        )
        assert services.resolve_redirect("/late") is None
        # ...and once the TTL expires the same reload converges without an
        # explicit invalidation.
        services._redirect_cache["loaded_at"] -= 61.0
        assert services.resolve_redirect("/late") == "/target"
