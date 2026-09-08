"""Unit tests for root-path normalization and segment-boundary matching."""

from __future__ import annotations

import pytest

from doc_hosting import paths


class TestNormalizeRootPath:
    def test_trims_lowercases_and_collapses_slashes(self):
        assert paths.normalize_root_path("  Project-1//Docs/ ") == "project-1/docs"

    def test_stores_no_leading_or_trailing_slash(self):
        assert paths.normalize_root_path("/docs/") == "docs"
        assert paths.normalize_root_path("A/B") == "a/b"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "/",
            "//",
            "..",
            ".",
            "../etc",
            "docs/..",
            "docs/../etc",
            "a/./b",
            "a b",
            "a$b",
            "a#b",
        ],
    )
    def test_rejects_unsafe_values(self, value):
        with pytest.raises(paths.InvalidPathError):
            paths.normalize_root_path(value)

    @pytest.mark.parametrize("value", ["api", "manage", "health", "_registry", "API"])
    def test_rejects_reserved_top_level_namespaces(self, value):
        with pytest.raises(paths.InvalidPathError):
            paths.normalize_root_path(value)

    def test_reserved_check_applies_only_to_the_first_segment(self):
        assert paths.normalize_root_path("docs/api") == "docs/api"
        assert paths.normalize_root_path("docs/manage") == "docs/manage"


class TestSegmentBoundaries:
    def test_is_ancestor_root(self):
        assert paths.is_ancestor_root("project-1", "project-1/docs")
        assert paths.is_ancestor_root("docs", "docs/en/latest")
        assert not paths.is_ancestor_root("docs", "docs")
        assert not paths.is_ancestor_root("project-1", "project-10")
        assert not paths.is_ancestor_root("project-1", "project-10/docs")

    def test_is_segment_prefix(self):
        assert paths.is_segment_prefix("docs", "docs")
        assert paths.is_segment_prefix("docs", "docs/en")
        assert not paths.is_segment_prefix("docs", "docsx")
        assert not paths.is_segment_prefix("docsx", "docs")


class TestNormalizeSitePath:
    def test_normalizes_like_roots_but_keeps_the_leading_slash(self):
        assert paths.normalize_site_path("/Docs//EN/") == "/docs/en"
        assert paths.normalize_site_path("/") == "/"

    @pytest.mark.parametrize(
        "value",
        [
            "docs",
            "https://evil.example.com/docs",
            "http://evil.example.com",
            "//evil.example.com/docs",
            "/docs/../etc",
            "/a b",
        ],
    )
    def test_rejects_non_site_absolute_paths(self, value):
        with pytest.raises(paths.InvalidPathError):
            paths.normalize_site_path(value)


class TestNormalizeRequestPath:
    def test_builds_the_matching_view(self):
        assert paths.normalize_request_path("/Docs//EN/") == "/docs/en/"
        assert paths.normalize_request_path("/docs/en") == "/docs/en"
        assert paths.normalize_request_path("/") == "/"

    def test_strips_query_and_fragment(self):
        assert paths.normalize_request_path("/docs?a=1#b") == "/docs"


class TestMatchRootPrefix:
    def test_returns_the_original_case_remainder(self):
        assert paths.match_root_prefix("/Docs/EN/File.HTML", "docs") == "/EN/File.HTML"
        assert paths.match_root_prefix("/docs", "docs") == ""
        assert paths.match_root_prefix("/docs/", "docs") == "/"

    def test_collapses_slashes_inside_the_prefix(self):
        assert paths.match_root_prefix("//Docs//EN/", "docs") == "/EN/"
        assert paths.match_root_prefix("//Docs//", "docs") == "/"

    def test_matches_only_on_segment_boundaries(self):
        assert paths.match_root_prefix("/project-10/x", "project-1") is None
        assert paths.match_root_prefix("/docsx", "docs") is None
        assert paths.match_root_prefix("/docs/en", "docs") == "/en"

    def test_multi_segment_root(self):
        assert paths.match_root_prefix("/project-1/docs/en", "project-1/docs") == "/en"
        assert paths.match_root_prefix("/project-1/doc", "project-1/docs") is None
