Configuration
=============

Charm configuration and integration
-----------------------------------

.. list-table::
   :header-rows: 1

   * - Name
     - Requirement
     - Application effect
   * - ``publish-token``
     - Required string
     - Exposed as ``APP_PUBLISH_TOKEN`` and required for publishing.
   * - ``s3`` relation
     - Required, limit one
     - Supplies the S3 settings. The charm remains blocked without it.

Application environment
-----------------------

.. list-table::
   :header-rows: 1

   * - Variable
     - Required/default
     - Meaning
   * - ``S3_ACCESS_KEY``
     - Required
     - S3 access key.
   * - ``S3_SECRET_KEY``
     - Required
     - S3 secret key.
   * - ``S3_BUCKET``
     - Required
     - Bucket containing documentation and registries.
   * - ``S3_ENDPOINT``
     - Optional; AWS default when absent
     - S3-compatible endpoint URL.
   * - ``S3_REGION``
     - ``us-east-1``
     - S3 region.
   * - ``S3_PATH``
     - Empty
     - Prefix prepended to every application storage key.
   * - ``S3_ADDRESSING_STYLE``
     - ``path``
     - Preferred addressing-style hint.
   * - ``S3_URI_STYLE``
     - ``path``
     - Fallback addressing-style hint.
   * - ``APP_PUBLISH_TOKEN``
     - Optional at settings load
     - Bearer token; publishing returns 503 if absent.

Addressing normalization checks ``S3_ADDRESSING_STYLE`` first and
``S3_URI_STYLE`` second. Case and surrounding whitespace are ignored,
underscores become hyphens, and ``-hosted`` is removed. ``virtual`` and
``virtualhost`` select virtual addressing; every other non-empty value selects
path addressing. The default is path addressing.

Missing required S3 variables raise a settings error; endpoints that need
storage return HTTP 503 while ``/health`` remains available.

Publisher settings
------------------

``scripts/publish.py`` reads environment variables before its optional env
file. It requires ``API_URL``, ``API_TOKEN``, ``S3_ACCESS_KEY``,
``S3_SECRET_KEY``, and ``S3_BUCKET``. ``S3_ENDPOINT`` is optional and
``S3_REGION`` defaults to ``us-east-1``. ``DOC_DOMAIN`` supplies the domain
unless ``--domain`` is passed. CLI values control root path, language, version,
commit hash, and domain as described in :doc:`../how-to/publish-documentation`.

``scripts/deploy.py`` generates ``.juju-deploy.env`` with ``API_URL``,
``API_TOKEN``, ``S3_ENDPOINT``, ``S3_ACCESS_KEY``, ``S3_SECRET_KEY``,
``S3_BUCKET``, and ``S3_REGION``. It writes the API URL on port 8080 and region
``us-east-1``.

GitHub Actions settings
-----------------------

The publish workflow maps variables ``DOC_HOSTING_API_URL``,
``DOC_HOSTING_S3_ENDPOINT``, ``DOC_HOSTING_S3_BUCKET``, and
``DOC_HOSTING_DOMAIN`` to publisher settings. It maps secrets
``DOC_HOSTING_API_TOKEN``, ``DOC_HOSTING_S3_ACCESS_KEY``, and
``DOC_HOSTING_S3_SECRET_KEY``.

Security constraints
--------------------

The local deployment uses HTTP. The charm's publish token is a plain string
visible through ``juju config``. Keep generated env files and credentials
private, bind local forwards only to localhost, and do not expose the proof of
concept as a production service.
