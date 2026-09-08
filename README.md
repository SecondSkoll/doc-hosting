# doc-hosting

`doc-hosting` is a proof of concept for publishing and serving static
documentation from S3-compatible storage on Juju. FastAPI provides the public
API and serving layer. A Django/PostgreSQL control plane stores projects,
mutable publications, redirects, migration and layout operations, and an
append-only audit trail. Staff manage the control plane at `/manage/`.

## Architecture and status

```text
GitHub Actions ─► scripts/publish.py ─► S3/MinIO content
                         │
                         └─► FastAPI ─► Django ORM ─► PostgreSQL
                                │
users ──────────────────────────┴─► redirects and S3-backed pages
staff ─────────────────────────────► /manage/
```

The Juju deployment contains `doc-hosting-api`, `postgresql-k8s`, MinIO, and
`s3-integrator`. PostgreSQL is the metadata source of truth; S3 contains only
published files and any pre-migration `_registry/` JSON.

Implemented behavior includes:

- dual publish authentication: the global bearer token and a per-project
  secret;
- normalized, nested project roots and mutable publication records;
- four language/version URL layouts;
- exact and prefix redirects;
- retryable root migrations and layout changes that preserve old URLs; and
- a staff-only Django management interface and read-only audit history.

The unit suite verifies this behavior. The latest local review ran 214 tests
successfully, with one PostgreSQL-only concurrency test skipped because the
local run used SQLite. The integration suite and rock/charm builds require
snapd, MicroK8s, and Juju and were not run in that review environment.

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
credentials: restrict access and delete it when no longer needed. No admin
password is provisioned automatically; create a superuser manually before
using `/manage/`.

See [Deploy the proof of concept locally](docs/how-to/deploy-the-poc-locally.rst)
for deployment, PostgreSQL, and superuser steps.

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
files. Use TLS whenever credentials cross an untrusted network.

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
- S3 credentials permit content uploads and must be treated as secrets.
- The charm's `publish-token` is a plain configuration string visible to
  authorized Juju operators. Use access controls appropriate to that risk.
- SQLite is only a local/test fallback. Use PostgreSQL for deployment and for
  concurrency guarantees.
