Use cases
=========

This page describes the major end-to-end interaction sequences of the system.

.. note:: Assumed deployment topology

   A content cache is assumed to be deployed in front of this solution.
   Requests for specific path prefixes reach ``doc-hosting-api`` through a
   cross-model connection from the cache's model. Publication traffic to
   ``POST /api/v1/*`` and management traffic to ``/manage/`` are assumed to
   bypass the cache.

   This topology is an assumption, not product behavior. The application emits
   no cache directives and provides no cache-invalidation interface, so cache
   freshness and invalidation are policies of the cache deployment. This is
   separate from the application's in-process redirect cache, whose lifetime is
   controlled by ``DOC_HOSTING_REDIRECT_CACHE_TTL`` and defaults to 30 seconds.

Publication
-----------

Direct upload requires both the deployment-wide bearer token and the project's
shared secret. The first authenticated publication claims an unclaimed root,
and the API returns one presigned, exact-key, checksum-bound PUT URL for each
manifest entry. Finalization verifies every object before atomically registering
the publication. The upload session and URLs expire after the configured
``url_ttl``, which defaults to 900 seconds.

.. mermaid::
   :alt: Direct-upload publication sequence from manifest creation through presigned uploads, verification, atomic registration, and replay handling.

   sequenceDiagram
       accTitle: Direct-upload publication
       accDescr: Publication proceeds from manifest creation through authenticated session creation, presigned uploads, object verification, atomic registration, and replay handling.
       participant Publisher as Publisher (scripts/publish.py or CI)
       participant API as doc-hosting-api
       participant DB as PostgreSQL control plane
       participant S3 as S3 storage
       Publisher->>Publisher: Compute manifest (path, SHA-256, size)
       Publisher->>API: POST /api/v1/uploads<br/>bearer token, project_secret, manifest
       API->>API: Validate request and credentials
       alt Authentication or validation fails
           API-->>Publisher: 401, 403, or 422
       else Valid request
           API->>DB: Resolve or claim root
           Note over API,DB: First claim audits project.claimed<br/>Wrong secret: 403, shadowing: 409
           API->>DB: Assert dimensions, create pending UploadSession<br/>Audit upload.begun
           Note over API,DB: Dimension conflict: 409<br/>Expiry = url_ttl
           API->>S3: Presign exact-key, checksum-bound PUT per entry
           API-->>Publisher: 201 upload_id, key_prefix, expires_at,<br/>url_ttl, uploads[]
           loop Each manifest entry
               Publisher->>S3: PUT file with returned checksum headers
               Note over Publisher,S3: A mismatching body is rejected
           end
           Publisher->>API: POST /api/v1/uploads/{upload_id}/finalize<br/>same credentials and manifest
           API->>API: Re-authenticate, check expiry and manifest drift
           API->>DB: Recheck dimensions
           API->>S3: Verify existence, size, and SHA-256 per object
           alt Verification or session conflict
               API-->>Publisher: 409, pending session remains retryable until expiry
           else First completion
               API->>DB: Atomic publication upsert and session completion<br/>Audit publication.upserted and upload.completed
               API-->>Publisher: 201 replay: false
           else Completed session, identical manifest
               API-->>Publisher: 200 replay: true
           else Completed session, differing manifest
               API-->>Publisher: 409
           end
       end

Supported failure and replay branches are:

* 401, 403, and 422 report authentication, authorization, or request-validation
  failures.
* 409 reports ownership, dimension, expiry, manifest-drift, or object-verification
  failures. A verification failure leaves the session pending and retryable
  until expiry.
* A finalize replay with an identical manifest returns 200 and ``replay: true``.
* A finalize replay with a differing manifest returns 409.

Consumption
-----------

Under the assumed topology, a reader requests a routed path through the content
cache. A cache miss is forwarded over the cross-model connection to
``doc-hosting-api``, which resolves redirects before matching the longest
registered root and parsing its active layout. The application then fetches the
exact S3 object or the corresponding ``index.html`` object and infers its media
type from the filename. Cache storage and freshness remain cache-deployment
policy because the application sends no cache directives.

.. mermaid::
   :alt: Content consumption sequence showing an assumed content-cache hit or cross-model forwarded miss, redirect handling, layout parsing, and S3 retrieval.

   sequenceDiagram
       accTitle: Content consumption through the assumed cache
       accDescr: A cache hit returns content directly, while a miss for a routed prefix crosses the assumed model connection before redirect resolution, layout parsing, and S3 retrieval.
       participant Reader
       participant Cache as Content cache (assumed)
       participant API as doc-hosting-api
       participant DB as PostgreSQL control plane
       participant S3 as S3 storage
       Reader->>Cache: GET /{path}
       alt Cache hit
           Note over Cache: Freshness follows cache policy,<br/>the product sends no cache directives
           Cache-->>Reader: 200 cached body
       else Cache miss for a routed path prefix
           Cache->>API: Forward over assumed cross-model connection
           API->>DB: Resolve enabled redirects via in-process cache
           alt Redirect match
               Note over API,DB: Exact beats prefix, longest prefix wins,<br/>suffix preserved, chain limit is 10 hops
               API-->>Cache: 301 final target
               Cache-->>Reader: 301 final target
           else Redirect loop or more than 10 hops
               API-->>Cache: 508
               Cache-->>Reader: 508
           else No redirect
               API->>DB: Find longest registered-root match
               alt No registered root
                   API-->>Cache: 404
               else Registered root
                   API->>API: Parse remainder against active layout
                   alt Layout root lacks trailing slash
                       API-->>Cache: 307 trailing-slash target
                   else Traversal or incomplete layout
                       API-->>Cache: 404
                   else Valid content path
                       API->>S3: Fetch exact object, then path/index.html
                       alt Object found
                           S3-->>API: Object body
                           API-->>Cache: 200 body with inferred media type
                           Note over Cache: Cache may store according to its policy
                           Cache-->>Reader: 200 body
                       else Object missing
                           S3-->>API: Not found
                           API-->>Cache: 404 without internal S3 key
                       end
                   end
               end
               Cache-->>Reader: 307 or 404 response
           end
       end

The serving outcomes are:

* 301 returns the final target for redirect chains of no more than 10 hops.
* 508 reports a redirect loop or a chain that exceeds the hop limit.
* 307 adds the trailing slash to a layout root.
* 404 reports an unknown root, incomplete layout, traversal path, or missing
  object without exposing the internal S3 key.

Version query
-------------

The version query is public and unauthenticated. It requires ``root_path`` and
accepts an optional ``language`` filter, then returns the project's normalized
root, domain, grouped versions, and active layout.

.. mermaid::
   :alt: Public version-query sequence showing root normalization, project lookup, and the grouped version response.

   sequenceDiagram
       accTitle: Public version query
       accDescr: The API normalizes the requested root, looks up the project, and returns grouped versions and layout details or an error.
       participant Client as Client (public, unauthenticated)
       participant API as doc-hosting-api
       participant DB as PostgreSQL control plane
       Client->>API: GET /api/v1/versions?root_path=...&language=...
       API->>API: Normalize root_path
       alt Root is missing or invalid
           API-->>Client: 422
       else Valid root
           API->>DB: Look up project and publications
           alt Unknown root
               DB-->>API: No project
               API-->>Client: 404
           else Project found
               DB-->>API: Project and grouped publications
               API-->>Client: 200 root_path, domain, versions[], layout
               Note over API,Client: Each version has version, sorted languages,<br/>and the latest matching commit hash
           end
       end

The endpoint returns 422 when ``root_path`` is missing or invalid, and 404 when
the normalized root is unknown.

Solution management
-------------------

The ``/manage/`` Django admin is restricted to staff users and protects mutation
forms with CSRF. Management operations change live control-plane metadata and
may copy or delete S3 objects. Root migrations and layout changes use the
resumable ``pending``, ``switched``, and ``completed`` states. A root migration
is the representative sequence below.

.. mermaid::
   :alt: Staff root-migration management sequence showing validation, object copying, atomic metadata switching, deletion, state transitions, and resume behavior.

   sequenceDiagram
       accTitle: Root migration through solution management
       accDescr: A staff request is validated, objects are copied, metadata is switched atomically, and copied old keys are deleted through resumable operation states.
       participant Operator as Operator (staff)
       participant Admin as /manage/ Django admin
       participant Services as Control-plane services
       participant DB as PostgreSQL control plane
       participant S3 as S3 storage
       Operator->>Admin: Staff login and CSRF-protected form submission
       Admin->>Services: Create path migration (project and new root)
       Services->>DB: Validate destination, root overlaps, and competing operations
       alt Initial validation fails
           Services-->>Admin: Reject without creating an operation
       else Migration created as pending
           Services->>DB: Audit migration.requested
           Services->>DB: Validate redirect conflicts and stale-operation guards
           alt Run validation fails
               Services-->>Admin: Operation stays pending
           else Run validation passes
               Services->>S3: Copy old-prefix objects to new keys
               alt Copy fails
                   Services-->>Admin: Operation stays pending
               else Copy succeeds
                   Services->>DB: Atomically switch project root,<br/>rewrite redirects, add old-to-new redirect, audit
                   Services->>DB: Set state to switched
                   Services->>S3: Delete exactly the copied old keys
                   alt Deletion fails
                       Services-->>Admin: Operation stays switched
                   else Deletion succeeds
                       Services->>DB: Set state to completed and audit
                       Services-->>Admin: Completed migration
                   end
               end
           end
       end
       Note over Operator,Services: A non-completed operation can be resumed<br/>subject to stale-operation and conflict guards

Layout changes follow the same state machine. Redirect creation, update, and
deletion follow a validate-save-audit pattern. Audit events are read-only.

Related information
-------------------

* :doc:`http-api` describes endpoint fields and errors.
* :doc:`storage-layout` describes control-plane records and S3 key layouts.
* :doc:`management` describes management permissions and operation states.
* :doc:`configuration` lists credentials and time-to-live settings.
* :doc:`../how-to/publish-documentation` provides the publishing procedure.
* :doc:`../how-to/manage-control-plane` provides control-plane procedures.
