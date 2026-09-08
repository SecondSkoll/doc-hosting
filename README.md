# doc-hosting

A proof of concept for replacing a hosted documentation service with a
self-hosted platform on Canonical's Juju ecosystem. The intended architecture
uses charmed components throughout: a GitHub workflow builds documentation,
optionally including a PDF, uploads the artefacts to S3-compatible storage and
registers the build with a publishing API. The deployed service then serves
the built documentation to users.

## Scope of the PoC (single charm)

This project adds **one charm, `doc-hosting-api`** — the API layer — packaged
as a [12-factor charm](https://documentation.ubuntu.com/charmcraft/)
(FastAPI on a chiselled Ubuntu rock). It uses the API as S3 middleware rather
than deploying a separate content-cache charm:

- **Ingestion API** — `POST /api/v1/publish`: registers a build (commit hash,
  version tag, language, domain, root path) in the documentation registry.
  Bearer-authenticated with the charm's `publish-token` configuration.
- **Version API** — `GET /api/v1/versions?root_path=...[&language=...]`:
  lists the registered versions and languages for a root path (public).
- **Doc serving** — `GET /{root_path}/{language}/{version}/...`: serves the
  built static documentation straight from the S3 bucket. Object keys mirror
  the URL paths (`{root_path}/{language}/{version}/<page>`), so any path
  published to the bucket is immediately served.

Registry metadata (projects, versions, languages) is persisted as JSON
objects under the reserved `_registry/` prefix in the same bucket. A separate
PostgreSQL-backed registry is deliberately deferred for the proof of concept.

## Architecture

```
GitHub Actions ──build docs──► scripts/publish.py ──upload──► MinIO (S3 bucket)
      │                              │
      │                              └──POST /api/v1/publish──► doc-hosting-api charm
      │                                                            (FastAPI, 12-factor)
      ▼
users ───────────GET /docs/en/latest/───────────────served from bucket────┘
```

Juju applications (microk8s):

| Application     | Role |
| --------------- | ---- |
| `doc-hosting-api` | the FastAPI API layer (this repo: `charm/` + `rockcraft.yaml`) |
| `s3-integrator` (track 2) | provides the `s3` interface to the charm, configured with the MinIO endpoint/credentials; creates the bucket |
| `minio` (`latest/edge`) | the S3 storage backend |

> **Why s3-integrator?** The 12-factor charm's `s3` integration (paas-charm)
> requires a *bucket* in the relation data. MinIO's own `s3-credentials`
> endpoint only publishes endpoint/credentials, so the direct integration
> would leave the charm blocked. The `s3-integrator` charm (track 2) fills
> the gap: it is configured with the MinIO endpoint and credentials and
> publishes the complete connection information, creating the configured
> bucket on the way.

## Quickstart

Prerequisites: a Linux host (amd64) with `sudo`, snapd and
[uv](https://docs.astral.sh/uv/) installed.

Run the setup from an interactive terminal so its `sudo` password prompts
(including those required to enable addons under strict MicroK8s) are visible;
do not run the whole script as root. On the first run, setup may add your
account to the strict MicroK8s `snap_microk8s` group and ask you to log out and
back in before rerunning the command.

Setup waits for the Kubernetes node, DNS and hostpath storage provisioner before
bootstrapping Juju. It uses `public.ecr.aws/juju` for controller images, allows
30 minutes for first-time pulls and prints Kubernetes scheduling/storage events
if the controller pod does not start.

An existing MicroK8s installation must already be on Kubernetes 1.34. Setup
refuses to jump an older cluster directly to `1.34-strict/stable`; Kubernetes
upgrades must be performed one minor release at a time, or a disposable local
cluster can be purged and recreated.

```bash
# 1. install the tooling, build the rock + charm, deploy everything
uv sync --dev
uv run scripts/deploy.py all        # setup + build + deploy (10-20 min on first run)

# 2. build and publish the documentation
make docs-html DOCS_BUILDDIR=_build/dirhtml
uv run scripts/publish.py --env-file .juju-deploy.env --build-dir docs/_build/dirhtml

# 3. read the served documentation
source .juju-deploy.env
curl "$API_URL/docs/en/latest/"
curl "$API_URL/api/v1/versions?root_path=docs"
```

`scripts/deploy.py` writes `.juju-deploy.env` (gitignored) with the
connection details: `API_URL`, `API_TOKEN`, `S3_ENDPOINT`, `S3_ACCESS_KEY`,
`S3_SECRET_KEY`, `S3_BUCKET`. If the cluster IPs are not reachable from your
machine, use the port-forward commands the script prints.

Run individual stages with `uv run scripts/deploy.py setup`,
`uv run scripts/deploy.py build`, or `uv run scripts/deploy.py deploy`. Use
`--debug` to stream subprocess output (including `rockcraft pack`) directly to
the console, for example `uv run scripts/deploy.py all --debug`. Use
`uv run scripts/deploy.py teardown` to destroy the model; add `--controller`
to also destroy the Juju controller. The latter also recovers an interrupted
bootstrap where the controller namespace exists in MicroK8s but is missing
from the local Juju client, after verifying that the namespace is marked as a
Juju controller.

For the complete procedure, including port forwarding and verification, see
[Deploy the proof of concept locally](docs/how-to/deploy-the-poc-locally.rst).

## Tests

```bash
uv run pytest tests/unit -v                     # unit tests (moto-mocked S3)
uv run pytest tests/integration -v -m integration  # end-to-end test (needs microk8s/Juju)
```

Integration tests require a bootstrapped controller first:
`uv run scripts/deploy.py setup`. They build the real docs fixture, publish
it through `scripts/publish.py` against the deployed MinIO/API and assert
the content is served — the same publish path the GitHub workflow uses.
Set `CHARM_FILE` and `APP_IMAGE` to reuse already-packed artefacts and skip
the build.

## GitHub workflow

[`.github/workflows/publish-docs.yaml`](.github/workflows/publish-docs.yaml)
builds the docs with Sphinx (`dirhtml`) and publishes them via
`scripts/publish.py` on every push to `main`, on `v*` tags (the tag name
becomes the published version, otherwise `latest` is published) and on
manual dispatch. Configure these repository variables and secrets:

| Setting | Type | Content |
| ------- | ---- | ------- |
| `DOC_HOSTING_API_URL` | variable | `http://<api-address>:8080` of the deployed service |
| `DOC_HOSTING_API_TOKEN` | secret | the charm's `publish-token` value |
| `DOC_HOSTING_S3_ENDPOINT` | variable | MinIO endpoint reachable from the runner |
| `DOC_HOSTING_S3_BUCKET` | secret-safe variable | the bucket name (e.g. `doc-hosting`) |
| `DOC_HOSTING_S3_ACCESS_KEY` | secret | MinIO access key |
| `DOC_HOSTING_S3_SECRET_KEY` | secret | MinIO secret key |
| `DOC_HOSTING_DOMAIN` | variable | serving domain to register (optional) |

GitHub-hosted runners cannot reach a microk8s cluster on a private network:
either expose the deployment or use a self-hosted runner. The integration test
covers the `scripts/publish.py` publish → serve path, but it requires a live
microk8s/Juju environment and is not run in CI.

Unit tests run in CI via
[`.github/workflows/test.yaml`](.github/workflows/test.yaml); integration
tests are intentionally not run there (they need microk8s/Juju on the host).

The same test workflow also builds the rock and charm declared in
[`artifacts.yaml`](artifacts.yaml). After a successful run for a commit on
`main`, [`.github/workflows/publish-edge.yaml`](.github/workflows/publish-edge.yaml)
publishes those artifacts to the `latest/edge` Charmhub channel. Configure a
`charmhub-edge` GitHub environment with a `CHARMCRAFT_AUTH` secret containing
an exported Charmcraft token with permission to upload and release
`doc-hosting-api`. The environment can use protection rules to require approval
before publication.

## Repository layout

```
app.py                  ASGI entrypoint (app:app) packed into the rock
doc_hosting/            the API layer (settings, S3 storage, FastAPI server)
rockcraft.yaml          the 12-factor FastAPI rock definition
charm/                  the doc-hosting-api charm (fastapi-framework extension)
scripts/deploy.py       Juju/microk8s setup, build and deploy automation
scripts/publish.py      upload docs to S3 + register with the ingestion API
docs/how-to/            task-oriented deployment, publishing, build and test guides
docs/reference/         API, configuration, component and storage reference
docs/Makefile           Canonical Sphinx Stack build and check orchestration
LOCAL_TESTING.md        pointer to the local deployment guide
tests/unit/             API unit tests (moto)
tests/integration/      end-to-end publish-and-serve test (jubilant)
```

Note: `rockcraft.yaml` contains two documented build fixes for the
fastapi-framework extension's python plugin — pip cannot install data scripts
(e.g. jmespath's `jp.py`, pulled in via boto3) inside the chiselled usr-merge
layout, and the repository's virtual `pyproject.toml` must be hidden from the
plugin's `pip install .` step. See the comments in `rockcraft.yaml`.

## Design and implementation status

The target publishing flow is:

1. A push, tag, or manual GitHub Actions event builds the documentation and,
  optionally, a PDF.
2. The workflow uploads the build artefacts to URL-shaped paths in an S3
  bucket.
3. The workflow posts the commit hash, version, language, domain, and root path
  to the registry API.
4. Newly uploaded content becomes available through the serving layer.

For readers, paths beneath the main domain can be routed to the relevant Juju
model or environment. The serving layer reads static HTML and assets from S3.
A future JavaScript add-on can query the public versions API to present
alternate versions, languages, or PDF downloads in a selector.

| Capability | Proof-of-concept status |
| ---------- | ----------------------- |
| Build, S3 upload, and metadata registration | Implemented by `scripts/publish.py` and `.github/workflows/publish-docs.yaml` |
| S3-backed serving middleware | Implemented by `doc_hosting/server.py` |
| Charmed registry microservice | Implemented as the `doc-hosting-api` 12-factor charm |
| Ingestion and versions APIs | Implemented |
| Version and language selector | Deferred; the versions API supplies its data |
| Reusable GitHub Action | Deferred; this repository currently provides a workflow |
| Production routing and content cache | Deferred; HAProxy or `traefik-k8s` and a content-cache charm are candidates |
| Dedicated metadata database | Deferred; PostgreSQL K8s is the intended production candidate |
| Monitoring | Deferred; Canonical Observability Stack (COS) is the intended candidate |
| Management dashboard | Deferred; a Django application is a candidate |
| Alternative object storage | MinIO is used; Ceph RADOS Gateway is a production candidate |
| Safe build expiry, removal, and migration | Deferred |

All production components are expected to be deployed as charms. The final
topology can separate routing, content caching, the registry API, object
storage, metadata storage, observability, and management UI while retaining
the same publishing contract.

## Security considerations

Publishing must be restricted to authenticated projects. The proof of concept
uses a bearer token, while a production deployment should validate repository
or GitHub organisation provenance, use organisation-managed credentials, or
limit publication access to trusted self-hosted runners. S3 credentials should
be scoped to the project's upload paths.

The read-only versions API must remain reachable by browser-side JavaScript,
but mutation endpoints must require authentication. A production deployment
must also use TLS and move credentials into Juju secrets.

Security notes: the `publish-token` charm config is a plain string (visible
via `juju config`); a production deployment should move it to a Juju secret
(the 12-factor extension supports secret-typed config options). MinIO runs
without TLS inside the cluster for the PoC.
