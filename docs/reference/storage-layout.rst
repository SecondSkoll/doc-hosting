Data model and storage
======================

PostgreSQL control plane
------------------------

The deployed source of truth is PostgreSQL. Django defines these records:

.. list-table::
   :header-rows: 1

   * - Model
     - Stored behavior
   * - ``Project``
     - Unique normalized root, domain, hashed project secret, active dimension
       flags, disabled-dimension labels, and timestamps.
   * - ``Publication``
     - Mutable current commit and registration time, unique for each project,
       language, and version.
   * - ``UploadSession``
     - Project, publication dimensions, commit, domain, logical key prefix,
       sorted manifest, expiry, timestamps, and ``pending`` or ``completed``
       status for an API-authorized direct upload.
   * - ``Redirect``
     - Unique source/match-type pair, target, enabled flag, and optional project.
   * - ``PathMigration``
     - Old/new roots, copied-key mapping, and ``pending``, ``switched``, or
       ``completed`` state.
   * - ``LayoutChange``
     - Old/new flags, labels, key mapping, generated redirect IDs, and operation
       state.
   * - ``AuditEvent``
     - Event type, project/root association, JSON payload, and timestamp.
       Application and admin access is read-only.

Beginning a direct upload appends ``upload.begun``. Successful finalization
atomically replaces the current publication row for its
project/language/version, completes the session, and appends
``publication.upserted`` and ``upload.completed``. Failed verification does
not register a publication. Other control-plane changes also append events.
Audit payloads contain neither project secrets, bearer tokens, nor presigned
URLs.

S3 object layouts
-----------------

S3 stores published files, not current metadata. The active project flags
select one of four key prefixes:

.. code-block:: text

   {root}/{language}/{version}/<page>
   {root}/{language}/<page>
   {root}/{version}/<page>
   {root}/<page>

If ``S3_PATH`` is configured, its stripped value prefixes every application
key. For direct uploads, the API creates one exact-key, checksum-bound
presigned PUT URL per manifest file. Finalization verifies object existence,
size, and SHA-256 before registering the build. The server infers response
media types from filenames.

Legacy registry import
----------------------

The previous implementation stored metadata as
``_registry/{root_path}.json``. Import it with:

.. code-block:: bash

   uv run manage.py import_legacy_registry

The command reads current S3 settings, normalizes roots, validates language and
version labels, and upserts projects and publications. It is idempotent and
does not modify S3. Invalid records are skipped; the command's summary counts
projects and newly created publications, while detailed skipped records are
available from the importer return value rather than command output.

Imported projects have no secret. Their first publication with a valid global
bearer token and non-empty project secret claims the project and stores the
secret hash.

The importer expects legacy JSON of this form:

.. code-block:: json

   {
     "root_path": "docs",
     "domain": "docs.example.com",
     "builds": [
       {
         "language": "en",
         "version": "latest",
         "commit_hash": "abc123",
         "registered_at": "2026-09-08T12:00:00+00:00"
       }
     ]
   }

See :doc:`http-api` for serving behavior and :doc:`management` for operation
records.
