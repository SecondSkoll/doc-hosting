Storage layout
==============

Documentation objects
---------------------

Published pages use this key layout:

.. code-block:: text

   {root_path}/{language}/{version}/<page>

For example, a ``dirhtml`` reference landing page can be stored as
``docs/en/latest/reference/index.html`` and served at
``/docs/en/latest/reference/``. If ``S3_PATH`` is configured, its stripped
value is prepended to every key.

The publisher assigns content types using the filename and uses
``application/octet-stream`` when no type is known. The server independently
infers the response media type from the requested candidate.

Registry objects
----------------

Each root path has one registry object:

.. code-block:: text

   _registry/{root_path}.json

``S3_PATH`` also prefixes registry keys. Registry objects have
``application/json`` content type and this structure:

.. code-block:: json

   {
     "root_path": "docs",
     "domain": "localhost",
     "builds": [
       {
         "language": "en",
         "version": "latest",
         "commit_hash": "abc123",
         "registered_at": "2026-09-08T12:00:00+00:00"
       }
     ]
   }

Registering the same language and version replaces that build entry. See
:doc:`http-api` for registration and serving behavior and
:doc:`../how-to/publish-documentation` for the upload task.
