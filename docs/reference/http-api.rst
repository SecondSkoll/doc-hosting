HTTP API
========

``GET /health``
---------------

Returns HTTP 200 and ``{"status":"ok"}``. It does not require S3
configuration.

``POST /api/v1/publish``
------------------------

Registers a build and returns HTTP 201. Send ``Authorization: Bearer <token>``
and a JSON body with required string fields ``commit_hash``, ``version``,
``language``, ``domain``, and ``root_path``.

``root_path``, ``language``, and ``version`` must each be one URL-safe segment:
the first character is alphanumeric and remaining characters are alphanumeric,
dot, underscore, or hyphen. ``.`` and ``..`` are rejected.

The response contains ``root_path``, ``domain``, ``language``, ``version``,
``commit_hash``, and an ISO 8601 UTC ``registered_at`` timestamp. Registering
the same language and version replaces its previous entry; registering any
build updates the root path's domain.

Authentication and configuration responses are:

* 401 with ``WWW-Authenticate: Bearer`` for a missing or malformed header.
* 403 for a mismatched token.
* 503 when the token or required S3 configuration is absent.
* 422 for an invalid body or unsafe slug.

``GET /api/v1/versions``
------------------------

Requires the ``root_path`` query parameter and accepts an optional ``language``
filter. It returns HTTP 200 with ``root_path``, ``domain``, and ``versions``.
Each version contains ``version``, a sorted ``languages`` list, and the commit
hash from the latest registered matching build. It returns 404 for an unknown
root path and 422 for an unsafe root path or missing required parameter.

Documentation serving
---------------------

``GET /{root_path}/{language}/{version}`` returns a 307 redirect to the same
path with a trailing slash.

``GET /{root_path}/{language}/{version}/{doc_path}`` retrieves an S3 object.
A trailing slash resolves only to ``index.html``. A path without a trailing
slash first checks the exact object and then ``<path>/index.html``. The response
content type is inferred from the candidate filename, defaulting to
``application/octet-stream``. Missing objects and path traversal segments
return 404. For content requests, root path, language, and version must be safe
slugs. The trailing-slash redirect itself does not perform slug validation.

Swagger UI, ReDoc, and the OpenAPI schema endpoints are disabled.

See :doc:`storage-layout` for object keys and
:doc:`../how-to/publish-documentation` to publish a build.
