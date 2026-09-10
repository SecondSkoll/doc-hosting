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
   * - ``admin-username``
     - Optional string; default ``admin``
     - Exposed as ``APP_ADMIN_USERNAME``; username of the superuser created
       automatically at application startup. Creation-only: an existing
       user is never modified.
   * - ``admin-password``
     - Optional string; default ``admin``
     - Exposed as ``APP_ADMIN_PASSWORD``; initial password of the
       auto-created superuser. A plain config value visible to authorized
       Juju operators; change it via the admin interface after first login.
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
   * - ``DOC_HOSTING_UPLOAD_URL_TTL``
     - ``900`` seconds
     - Positive integer lifetime for presigned PUT URLs and upload sessions.
       Missing, invalid, or non-positive values use 900 seconds.
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
   * - ``APP_ADMIN_USERNAME``
     - Optional; charm default ``admin``
     - Username for the admin superuser provisioned at startup. Both admin
       variables unset: no user is created. Only one set: startup fails.
   * - ``APP_ADMIN_PASSWORD``
     - Optional; charm default ``admin``
     - Password for the provisioned superuser (stored only as a Django
       hash). Creation happens once; later config changes never alter an
       existing user's password.
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
It requires ``API_URL``, ``API_TOKEN``, and ``PROJECT_SECRET``. ``DOC_DOMAIN``
supplies the domain unless ``--domain`` is passed. It does not read S3
credentials; the API returns short-lived presigned upload URLs.

``scripts/deploy.py`` writes ``.juju-deploy.env`` with ``API_URL``,
``ADMIN_URL``, ``API_TOKEN``, ``PROJECT_SECRET``, ``S3_ENDPOINT``,
``S3_ACCESS_KEY``, ``S3_SECRET_KEY``, ``S3_BUCKET``, and ``S3_REGION``.
The S3 values support deployment operations and are not required by the
publisher.

GitHub Actions settings
-----------------------

The publish workflow maps variables ``DOC_HOSTING_API_URL`` and
``DOC_HOSTING_DOMAIN``. It maps secrets ``DOC_HOSTING_API_TOKEN`` and
``DOC_HOSTING_PROJECT_SECRET``. No S3 setting or credential is exposed to the
workflow.

Keep all generated env files and credentials private. The charm's
``publish-token`` is a plain configuration value visible to authorized Juju
operators; the project secret is stored by the application only as a hash.
