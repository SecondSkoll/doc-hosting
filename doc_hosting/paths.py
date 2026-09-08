"""Path normalization and segment-boundary matching for the doc host.

Root paths are normalized, safe, multi-segment prefixes such as
``project-1/docs``: surrounding whitespace is trimmed, the value is
lowercased, repeated slashes collapse, no leading or trailing slash is
stored, empty/dot/traversal segments are rejected, every segment must be a
single URL-safe slug, and the top-level namespaces ``api``, ``manage``,
``health`` and ``_registry`` are reserved.  Matching is always on segment
boundaries, so ``project-1`` and ``project-10`` are unrelated roots.
"""

from __future__ import annotations

import re

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESERVED_SEGMENTS = ("api", "manage", "health", "_registry")


class InvalidPathError(ValueError):
    """Raised when a path cannot be normalized to a safe value."""


def is_safe_segment(segment: str) -> bool:
    """Return whether ``segment`` is a single safe URL path segment."""
    return bool(segment) and segment not in (".", "..") and bool(SLUG_RE.match(segment))


def normalize_root_path(raw: str) -> str:
    """Normalize a root path such as ``Project-1//Docs/`` to ``project-1/docs``.

    Raises:
        InvalidPathError: when the result would be empty, unsafe, or reserved.
    """
    if not isinstance(raw, str):
        raise InvalidPathError("root path must be a string")
    segments = [segment for segment in raw.strip().lower().split("/") if segment]
    if not segments:
        raise InvalidPathError("root path must contain at least one segment")
    for segment in segments:
        if not is_safe_segment(segment):
            raise InvalidPathError(
                f"invalid root path segment: {segment!r} must be a single URL-safe segment"
            )
    if segments[0] in RESERVED_SEGMENTS:
        raise InvalidPathError(
            f"root path segment {segments[0]!r} is reserved for the hosting platform"
        )
    return "/".join(segments)


def normalize_site_path(raw: str) -> str:
    """Normalize a same-site absolute path such as ``/Docs//EN/`` to ``/docs/en``.

    The site root normalizes to ``/``.  Segment rules match
    :func:`normalize_root_path` (including lowercasing), but the value must
    carry a leading slash.

    Raises:
        InvalidPathError: when the value is not a same-site absolute path.
    """
    if not isinstance(raw, str):
        raise InvalidPathError("path must be a string")
    value = raw.strip()
    if not value.startswith("/"):
        raise InvalidPathError(f"path must be site-absolute (start with '/'): {raw!r}")
    if "://" in value:
        raise InvalidPathError(f"path must not contain a scheme or host: {raw!r}")
    if value.startswith("//"):
        raise InvalidPathError(f"path must not be protocol-relative: {raw!r}")
    segments = [segment for segment in value.lower().split("/") if segment]
    for segment in segments:
        if not is_safe_segment(segment):
            raise InvalidPathError(
                f"invalid path segment: {segment!r} must be a single URL-safe segment"
            )
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def normalize_request_path(raw: str) -> str:
    """Normalize a request URL path for matching: lowercase, collapsed slashes.

    Unlike :func:`normalize_site_path` this never raises: it is only used to
    build the lookup view of an incoming request.  A single trailing slash is
    preserved (it matters for index resolution), and the site root is ``/``.
    """
    value = raw.split("?", 1)[0].split("#", 1)[0]
    if not value.startswith("/"):
        value = "/" + value
    segments = [segment for segment in value.lower().split("/") if segment]
    trailing = value.endswith("/")
    normalized = "/" + "/".join(segments)
    if normalized == "/":
        return "/"
    return normalized + ("/" if trailing else "")


def is_segment_prefix(prefix: str, path: str) -> bool:
    """Return whether ``prefix`` contains ``path`` on segment boundaries.

    Both values are root-style (no leading slash) or site-style (leading
    slash); they must use the same convention.
    """
    if prefix == path:
        return True
    return path.startswith(prefix + "/")


def is_ancestor_root(ancestor: str, root: str) -> bool:
    """Return whether normalized root ``ancestor`` contains root ``root``."""
    return ancestor != root and is_segment_prefix(ancestor, root)


def split_request_path(raw_path: str, segments: list[str]) -> str | None:
    """Return the part of ``raw_path`` after ``segments`` (original case).

    The prefix must match case-insensitively on segment boundaries,
    tolerating collapsed slashes.  The remainder keeps the original case
    (with collapsed leading slashes) and any trailing slash; it is ``""``
    when the request exactly matches the prefix.  Returns ``None`` when
    the prefix does not match.
    """
    position = 0
    for segment in segments:
        while position < len(raw_path) and raw_path[position] == "/":
            position += 1
        end = position + len(segment)
        if raw_path[position:end].lower() != segment:
            return None
        position = end
    if position < len(raw_path) and raw_path[position] != "/":
        return None
    remainder = raw_path[position:]
    if remainder.startswith("/"):
        remainder = remainder.lstrip("/")
        remainder = "/" + remainder if remainder else "/"
    return remainder


def match_root_prefix(raw_path: str, root: str) -> str | None:
    """Return the request remainder after ``root``, or ``None`` without a match."""
    return split_request_path(raw_path, root.split("/"))
