HTTP API
========

The OpenAPI schema, Swagger UI, and ReDoc endpoints are disabled.

``GET /health``
---------------

Returns HTTP 200 and ``{"status":"ok"}``. It does not require S3 settings.

Direct-upload authentication
----------------------------

Both direct-upload endpoints require these independent credentials:

* ``Authorization: Bearer <token>`` must match ``APP_PUBLISH_TOKEN``.
* ``project_secret`` in the JSON body must be non-empty and match the project
  secret after the root is claimed.

The bearer token is a deployment-wide gate. The project secret establishes
root ownership; only its salted hash is stored. The begin endpoint uses the
same nested-root ownership and disabled-dimension rules as the legacy publish
endpoint.

``POST /api/v1/uploads``
------------------------

Begins the preferred direct-upload flow and returns HTTP 201. The request body
contains:

.. code-block:: json

   {
     "commit_hash": "abc123",
     "version": "latest",
     "language": "en",
     "domain": "docs.example.com",
     "root_path": "docs",
     "project_secret": "project-secret",
     "manifest": [
       {
         "path": "index.html",
         "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
         "size": 1234
       }
     ]
   }

``commit_hash``, ``version``, ``language``, ``domain``, ``root_path``,
``project_secret``, and ``manifest`` are required. The manifest must be a
non-empty list. Each entry requires:

* ``path``: a unique, non-empty relative path. Absolute paths, backslashes,
  control characters, empty segments, ``.`` segments, and ``..`` segments are
  rejected.
* ``sha256``: exactly 64 lowercase hexadecimal characters.
* ``size``: a non-negative JSON integer; booleans and strings are rejected.

The API normalizes and sorts the manifest, authorizes or claims the project
root, creates a pending upload session, and returns:

.. code-block:: json

   {
     "upload_id": 42,
     "root_path": "docs",
     "key_prefix": "docs/en/latest",
     "expires_at": "2026-09-08T12:15:00+00:00",
     "url_ttl": 900,
     "uploads": [
       {
         "path": "index.html",
         "url": "https://storage.example/...",
         "headers": {
           "x-amz-checksum-sha256": "base64-encoded-digest"
         }
       }
     ]
   }

There is one SigV4 presigned PUT URL for each manifest path. Each URL is bound
to that exact object key, expiry, and checksum header. The uploader must PUT
the file bytes to ``url`` with every returned ``headers`` entry unchanged.
Presigned URLs are temporary credentials and should not be logged or shared.
The URLs and a pending upload session expire after ``url_ttl`` seconds.

``POST /api/v1/uploads/{upload_id}/finalize``
------------------------------------------------

Finalizes a direct upload. The request repeats the exact manifest supplied to
the begin endpoint:

.. code-block:: json

   {
     "project_secret": "project-secret",
     "manifest": [
       {
         "path": "index.html",
         "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
         "size": 1234
       }
     ]
   }

The API re-authenticates the project, rejects an expired session or changed
manifest, rechecks the project's current language/version dimensions, and
verifies that every object exists with the declared size and SHA-256. It then
atomically upserts the publication, marks the session completed, and records
audit events. A verification failure leaves the session pending and does not
register a publication; it can be retried until expiry.

First completion returns HTTP 201 with:

.. code-block:: json

   {
     "upload_id": 42,
     "root_path": "docs",
     "domain": "docs.example.com",
     "language": "en",
     "version": "latest",
     "commit_hash": "abc123",
     "registered_at": "2026-09-08T12:01:00+00:00",
     "replay": false
   }

Replaying a completed session with the same manifest returns HTTP 200 and
``replay: true`` without registering or auditing it again. A different
manifest returns 409.

Direct-upload errors
--------------------

* 401, with ``WWW-Authenticate: Bearer``: missing or malformed authorization.
* 403: wrong bearer token, wrong project secret, or an unauthorized nested
  root claim.
* 404: unknown ``upload_id`` on finalize.
* 409: root-ownership or dimension conflict; or, on finalize, expiry, manifest
  drift, differing replay, missing object, size mismatch, or checksum mismatch.
* 422: invalid request fields, paths, checksums, sizes, or an empty manifest or
  project secret.
* 503: the publish token or required server-side S3 settings are unavailable.

``POST /api/v1/publish`` (legacy register-only)
------------------------------------------------

Upserts the current publication for a project, language, and version and
returns HTTP 201. It does not upload or verify objects. It remains available
for callers that arrange storage separately; new publishers should use the
direct-upload flow.

Authentication
~~~~~~~~~~~~~~

Both credentials are mandatory:

* ``Authorization: Bearer <token>`` must match ``APP_PUBLISH_TOKEN``.
* ``project_secret`` in the JSON body must be non-empty and must match the
  project secret after the root is claimed.

Request fields
~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - Field
     - Type
     - Constraint
   * - ``commit_hash``
     - string
     - Required.
   * - ``version``
     - string
     - Required URL-safe segment.
   * - ``language``
     - string
     - Required URL-safe segment.
   * - ``domain``
     - string
     - Required; replaces the project's current domain when changed.
   * - ``root_path``
     - string
     - Required normalized project root; may contain multiple segments.
   * - ``project_secret``
     - string
     - Required and non-empty.

A URL-safe segment begins with an alphanumeric character and then contains
only alphanumeric characters, dots, underscores, or hyphens. ``.`` and ``..``
are invalid. Root paths are trimmed, lowercased, stripped of leading/trailing
slashes, and have repeated slashes collapsed. Their first segment cannot be
``api``, ``manage``, ``health``, or ``_registry``.

Ownership uses segment boundaries. For example, ``project-1`` does not own
``project-10``. A new nested root such as ``project-1/guides`` requires a
secret matching an existing ancestor. A new parent that would shadow an
existing descendant is rejected. If legacy import created an unclaimed
project, its first fully authenticated publication adopts the supplied secret.

The response contains ``root_path``, ``domain``, ``language``, ``version``,
``commit_hash``, ``registered_at`` (ISO 8601), and boolean ``claimed``. A
repeat publication replaces the existing publication row for the same
project/language/version pair and creates another audit event.

Errors
~~~~~~

* 401, with ``WWW-Authenticate: Bearer``: missing or malformed authorization.
* 403: wrong bearer token, wrong project secret, or a nested claim with no
  matching ancestor secret.
* 409: a claim shadows an existing descendant, a concurrent claim won, or a
  language/version conflicts with a disabled dimension's sole label.
* 422: invalid fields, root path, language, version, or empty project secret.
* 503: the bearer token or required S3 settings are unavailable.

``GET /api/v1/versions``
------------------------

The public endpoint requires ``root_path`` and accepts optional ``language``.
It returns normalized ``root_path``, ``domain``, ``versions``, and ``layout``.
Each version entry contains ``version``, sorted ``languages``, and the commit
hash of the latest matching publication encountered. ``layout`` contains:

.. code-block:: json

   {
     "language_enabled": true,
     "version_enabled": true,
     "language_label": "",
     "version_label": ""
   }

It returns 404 for an unknown root and 422 for an invalid or missing root.

Redirect and content serving
----------------------------

Requests first resolve enabled redirects, then use the longest matching
registered root and that project's active layout.

* Exact redirects match one normalized path only.
* Prefix redirects match on segment boundaries, preserve the remaining suffix,
  and use the longest matching prefix; exact matches take priority.
* Redirect chains resolve to the final target and return 301. A loop or chain
  beyond 10 hops returns 508.
* Redirects are same-site absolute paths. External and protocol-relative
  targets are unsupported.

The four content layouts are:

.. list-table::
   :header-rows: 1

   * - Language
     - Version
     - URL and S3 key prefix
   * - enabled
     - enabled
     - ``/{root}/{language}/{version}/``
   * - enabled
     - disabled
     - ``/{root}/{language}/``
   * - disabled
     - enabled
     - ``/{root}/{version}/``
   * - disabled
     - disabled
     - ``/{root}/``

A layout root without its trailing slash returns 307. A trailing slash loads
``index.html``. Other paths try the exact object and then ``<path>/index.html``.
Missing, incomplete-layout, unclaimed, and traversal paths return 404 without
exposing an internal S3 key.

See :doc:`storage-layout` for persistence and
:doc:`../how-to/publish-documentation` for the publishing procedure.
