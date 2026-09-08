Configuration
=============

Charm configuration and integrations
------------------------------------

.. list-table::
   :header-rows: 1

   * - Name
     - Requirement
     - Effect
   * - ``publish-token``
     - Required string
     - Exposed as ``APP_PUBLISH_TOKEN``.
   * - ``allowed-hosts``
     - Optional comma-separated string
     - Exposed as ``APP_ALLOWED_HOSTS`` for Django ``ALLOWED_HOSTS``.
   * - ``csrf-trusted-origins``
     - Optional comma-separated string
     - Exposed as ``APP_CSRF_TRUSTED_ORIGINS``; origins include their scheme.
   * - ``s3`` integration
     - Required; limit one
     - Supplies S3 settings. The charm blocks without it.
   * - ``postgresql`` integration
     - Required; limit one
     - Supplies the control-plane PostgreSQL connection.

Application environment
-----------------------

.. list-table::
   :header-rows: 1

   * - Variable
     - Required/default
     - Meaning
   * - ``S3_ACCESS_KEY``
     - Required for storage operations
     - S3 access key.
   * - ``S3_SECRET_KEY``
     - Required for storage operations
     - S3 secret key.
   * - ``S3_BUCKET``
     - Required for storage operations
     - Bucket containing documentation objects.
   * - ``S3_ENDPOINT``
     - Optional; AWS default
     - S3-compatible endpoint URL.
   * - ``S3_REGION``
     - ``us-east-1``
     - S3 region.
   * - ``S3_PATH``
     - Empty
     - Prefix prepended to application storage keys.
   * - ``S3_ADDRESSING_STYLE``
     - ``path``
     - Preferred addressing-style hint.
   * - ``S3_URI_STYLE``
     - ``path``
     - Fallback addressing-style hint.
   * - ``APP_PUBLISH_TOKEN``
     - Required to publish
     - Deployment-wide bearer credential.
   * - ``POSTGRESQL_DB_CONNECT_STRING``
     - Deployment database URL
     - Preferred PostgreSQL connection URL.
   * - ``DATABASE_URL``
     - Optional fallback
     - Used when ``POSTGRESQL_DB_CONNECT_STRING`` is absent.
   * - ``APP_SECRET_KEY``
     - Required outside development
     - Preferred Django signing key; ``DJANGO_SECRET_KEY`` is the fallback.
   * - ``APP_ALLOWED_HOSTS``
     - Required for usable admin outside development
     - Comma-separated host names; ``ALLOWED_HOSTS`` is the fallback.
   * - ``APP_CSRF_TRUSTED_ORIGINS``
     - Empty
     - Comma-separated trusted origins; ``CSRF_TRUSTED_ORIGINS`` is fallback.
   * - ``DOC_HOSTING_REDIRECT_CACHE_TTL``
     - ``30`` seconds
     - Non-negative redirect-cache lifetime; invalid values use 30 seconds.
   * - ``DOC_HOSTING_DEV``
     - Disabled
     - Explicit local/test mode. Truthy values permit insecure key/host defaults.
   * - ``DOC_HOSTING_SQLITE_PATH``
     - ``doc-hosting.sqlite3`` in repository root
     - SQLite path when neither PostgreSQL URL exists.
   * - ``DOC_HOSTING_STATIC_ROOT``
     - ``staticfiles`` in repository root
     - Fallback location for Django admin static assets.

SQLite is a local/test fallback, not the deployed control plane. Without a
Django signing key outside explicit development mode, startup fails. Without
allowed hosts, Django trusts no host. Configure HTTPS at the ingress and list
its full origin (for example, ``https://docs.example.com``) in the CSRF setting.

Addressing normalization checks ``S3_ADDRESSING_STYLE`` before
``S3_URI_STYLE``. Case and whitespace are ignored, underscores become hyphens,
and ``-hosted`` is removed. ``virtual`` and ``virtualhost`` select virtual
addressing; every other non-empty value selects path addressing.

Publisher settings
------------------

``scripts/publish.py`` reads environment values before its optional env file.
It requires ``API_URL``, ``API_TOKEN``, ``PROJECT_SECRET``, ``S3_ACCESS_KEY``,
``S3_SECRET_KEY``, and ``S3_BUCKET``. ``S3_ENDPOINT`` is optional and
``S3_REGION`` defaults to ``us-east-1``. ``DOC_DOMAIN`` supplies the domain
unless ``--domain`` is passed.

``scripts/deploy.py`` writes ``.juju-deploy.env`` with ``API_URL``,
``ADMIN_URL``, ``API_TOKEN``, ``PROJECT_SECRET``, ``S3_ENDPOINT``,
``S3_ACCESS_KEY``, ``S3_SECRET_KEY``, ``S3_BUCKET``, and ``S3_REGION``.

GitHub Actions settings
-----------------------

The publish workflow maps variables ``DOC_HOSTING_API_URL``,
``DOC_HOSTING_S3_ENDPOINT``, ``DOC_HOSTING_S3_BUCKET``, and
``DOC_HOSTING_DOMAIN``. It maps secrets ``DOC_HOSTING_API_TOKEN``,
``DOC_HOSTING_PROJECT_SECRET``, ``DOC_HOSTING_S3_ACCESS_KEY``, and
``DOC_HOSTING_S3_SECRET_KEY``.

Keep all generated env files and credentials private. The charm's
``publish-token`` is a plain configuration value visible to authorized Juju
operators; the project secret is stored by the application only as a hash.
