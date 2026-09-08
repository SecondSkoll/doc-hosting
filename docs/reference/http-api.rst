HTTP API
========

The OpenAPI schema, Swagger UI, and ReDoc endpoints are disabled.

``GET /health``
---------------

Returns HTTP 200 and ``{"status":"ok"}``. It does not require S3 settings.

``POST /api/v1/publish``
------------------------

Upserts the current publication for a project, language, and version. It
returns HTTP 201.

Authentication
~~~~~~~~~~~~~~

Both credentials are mandatory:

* ``Authorization: Bearer <token>`` must match ``APP_PUBLISH_TOKEN``.
* ``project_secret`` in the JSON body must be non-empty and must match the
  project secret after the root is claimed.

The bearer token is a deployment-wide gate. The project secret establishes
root ownership; only its salted hash is stored.

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
