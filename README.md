# doc-hosting

`doc-hosting` is a proof of concept for publishing and serving static
documentation from S3-compatible storage on Juju. FastAPI provides the public
API and serving layer. A Django/PostgreSQL control plane stores projects,
mutable publications, redirects, migration and layout operations, and an
append-only audit trail. Staff manage the control plane at `/manage/`.

## Architecture and status

```text
GitHub Actions ─► scripts/publish.py ─► FastAPI ─► Django ORM ─► PostgreSQL
                         │                 │
                         └─ presigned PUT ─┴─► S3/MinIO content
users ────────────────────────────────► redirects and S3-backed pages
staff ────────────────────────────────► /manage/
```

The Juju deployment contains `doc-hosting-api`, `postgresql-k8s`, MinIO, and
`s3-integrator`. PostgreSQL is the metadata source of truth; S3 contains only
published files and any pre-migration `_registry/` JSON.

Implemented behavior includes:

- dual publish authentication: the global bearer token and a per-project
  secret;
- API-authorized direct uploads with exact-key, checksum-bound presigned URLs
  and verified, atomic finalization;
- normalized, nested project roots and mutable publication records;
- four language/version URL layouts;
- exact and prefix redirects;
- retryable root migrations and layout changes that preserve old URLs; and
- a staff-only Django management interface and read-only audit history.

The unit and integration suites cover this behavior; deployment tests and
rock/charm builds require snapd, MicroK8s, and Juju.

## Local deployment quickstart

Prerequisites are Linux on amd64, `sudo`, snapd, Git, and
[uv](https://docs.astral.sh/uv/). Setup is interactive and must not be run as
root.

```bash
uv sync --dev
uv run scripts/deploy.py all
uv run --group docs sphinx-build -b dirhtml docs docs/_build/dirhtml
uv run scripts/publish.py --env-file .juju-deploy.env \
  --build-dir docs/_build/dirhtml
```

The deploy script creates a PostgreSQL integration and writes
`.juju-deploy.env`, including `API_URL`, `ADMIN_URL`, `API_TOKEN`,
`PROJECT_SECRET`, and S3 settings. The file is ignored by Git but contains
credentials: restrict access and delete it when no longer needed. The admin
superuser is created automatically at startup with the default credentials
`admin`/`admin`. Override them with the `admin-username` and `admin-password`
charm options on the initial deployment, and change the password in the admin
interface after first login. No action, SSH session, or management command is
required.

See [Deploy the proof of concept locally](docs/how-to/deploy-the-poc-locally.rst)
for deployment, PostgreSQL, and admin sign-in steps.

## Publish credentials

Every publication requires two independent secrets:

- `API_TOKEN` is the deployment-wide token sent as
  `Authorization: Bearer ...`.
- `PROJECT_SECRET` is sent as the JSON `project_secret`. The first fully
  authenticated publication claims the normalized root and stores only a
  salted hash. Later publications must provide the same secret.

For GitHub Actions, configure `DOC_HOSTING_API_TOKEN` and
`DOC_HOSTING_PROJECT_SECRET` as repository or environment secrets. Never put
either value in workflow YAML, logs, repository variables, or committed env
files. The workflow and publisher need no S3 credentials: the API supplies
short-lived, path-restricted upload URLs. Use TLS whenever credentials cross
an untrusted network.

See [Publish documentation](docs/how-to/publish-documentation.rst) for all
workflow settings and nested-root ownership rules.

## Documentation

- [How-to guides](docs/how-to/index.rst): deploy, publish, operate, and test.
- [Reference](docs/reference/index.rst): API, configuration, management, data
  model, layouts, and components.

Build the docs with:

```bash
uv sync --group docs
uv run --group docs sphinx-build --fail-on-warning --keep-going \
  -b dirhtml docs docs/_build
```

## Security boundaries

- `/manage/` uses Django staff authentication and CSRF protection. Configure
  allowed hosts and trusted HTTPS origins before exposing it.
- The local deployment uses HTTP and is not production-ready. Bind port
  forwards to localhost.
- Server-side S3 credentials permit storage access and must be treated as
  secrets. Presigned upload URLs are also temporary credentials and must not be
  logged or shared.
- The charm's `publish-token` is a plain configuration string visible to
  authorized Juju operators. Use access controls appropriate to that risk.
- The charm's `admin-password` is a plain configuration string visible to
  authorized Juju operators, and its default (`admin`) is public knowledge.
  It is only authoritative until the first login: change the password in the
  admin interface immediately, after which the config value no longer
  affects the account.
- SQLite is only a local/test fallback. Use PostgreSQL for deployment and for
  concurrency guarantees.
