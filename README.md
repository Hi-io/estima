# Collective

**FCAPSule investigates; Collective remembers.** Collective is a shared,
durable memory API for curated operational cases prepared by FCAPSule
instances. It stores and retrieves background without deciding what that
background means. Collective was previously named Estima; its Python module,
`/v1` API, database schema, and existing deployment names remain compatible.

Each FCAPSule instance runs its own models, analyzes evidence, decides what to
share, and interprets retrieved history. Collective stores observations
separately from hypotheses and returns both in their original categories. It does not
execute LLMs, call model providers, require model API keys, or incur model-token
usage. Multiple FCAPSule instances may use one Collective; the integration is
optional, so FCAPSule continues to work without it.

## Run Locally

Collective requires PostgreSQL 14 or newer and Python 3.11 or newer.

```sh
export DATABASE_URL='postgresql://estima:password@localhost:5432/estima'
export COLLECTIVE_ADMIN_TOKEN="$(openssl rand -hex 32)"
python -m pip install -e .
python -m uvicorn estima.app:app --host 0.0.0.0 --port 8080
```

The `estima` Python module path is retained for existing launch commands. At
startup Collective applies versioned SQL migrations and becomes ready only
after PostgreSQL is reachable. `/healthz` is unauthenticated; all data routes
require a bearer credential. `COLLECTIVE_ADMIN_TOKEN` is an operator-only
credential for provisioning and revocation; it cannot publish a case directly.
`DATABASE_URL` is the database connection setting. `ESTIMA_API_TOKEN` and
`ATLAS_API_TOKEN` remain accepted as legacy token fallbacks.

Run the container locally:

```sh
docker build -t collective-service:local .
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgresql://estima:password@host.docker.internal:5432/estima' \
  -e COLLECTIVE_ADMIN_TOKEN="$COLLECTIVE_ADMIN_TOKEN" collective-service:local
```

## API

The `/v1` route and case payload contract remain wire-compatible with the
existing FCAPSule client; publishers must use an instance-bound credential as
described under [Credential Migration And Trust Boundary](#credential-migration-and-trust-boundary):

- `POST /v1/cases` stores a versioned case envelope with `instance_id`,
  `episode_id`, `revision`, timezone-aware `observed_at`, `scope`, `summary`,
  `observations`, and `hypotheses`. Replaying the same
  `(instance_id, episode_id, revision)` payload returns the same case; a
  different payload for that key returns `409`.
- `GET /v1/stats` returns exact distinct totals: `episodes` counts distinct
  `(instance_id, episode_id)` pairs, `revisions` counts stored case revisions,
  and `patterns` counts distinct patterns represented by the latest revision
  of each episode.
- `GET /v1/cases` lists the latest revision for each episode. It accepts
  `limit` (default 20, maximum 50), an opaque `cursor`, optional `scope` JSON,
  and `query`. Its response contains `cases`, `limit`, `has_more`, and
  `next_cursor`; pass the cursor with the same filters to get the next page.
- `GET /v1/cases/{id}` returns a stored case.
- `POST /v1/search` retrieves latest revisions by scope, time, text, or exact
  fingerprint. Ranking scores are heuristics, not probabilities. Search accepts
  `limit` up to 50 and an optional `cursor`; its response adds `next_cursor` to
  the existing `cases`, `limit`, and `has_more` fields. Candidate evaluation
  remains bounded to 500 records.
- `GET /v1/patterns` returns repeated typed observations; `GET
  /v1/patterns/{id}` returns the aggregate and member cases. Co-occurrence is
  not reported as a shared cause.
- `POST /v1/admin/instances/{instance_id}/publisher-credentials` provisions a
  publisher bound to that instance. `POST /v1/admin/reader-credentials`
  provisions a read-only organization reader. The generated secret is returned
  once; only its hash is stored.
- `POST /v1/credentials/rotate` rotates the authenticated publisher's key. The
  replacement retains the same instance binding; both keys work during the
  configured overlap (five minutes by default, at most one hour), after which
  the old key is rejected. `DELETE /v1/admin/credentials/{key_id}` revokes a
  key immediately. Credential issuance, rotation, and revocation write
  non-secret audit metadata.

Observation facts and unverified hypotheses use separate fields and remain
separate in storage and responses. Collective does not infer causes, resolutions,
or interpretations. Scope includes environment, cluster, namespace, service,
workload, CNFC ID, and VNFC ID.

Bodies are limited to 40 KiB; observations must be scalar, and bounds reject
secret-looking fields/values and nested raw telemetry. These checks are
defense in depth, not a substitute for upstream data minimization, TLS,
backups, and secret management.

## Credential Migration And Trust Boundary

Publisher credentials are bound server-side to one `instance_id`. A matching
`instance_id` remains in the case envelope for wire compatibility; a mismatch
returns `403` and is never stored. Publishers can read shared history across
all instances in the organization. Reader credentials have the same read scope
but cannot publish. Admin credentials can issue/revoke credentials and read,
but cannot publish a caller-selected instance.

`COLLECTIVE_ADMIN_TOKEN` is the separate operator credential. It must be
strong, kept out of FCAPSule instance configuration, and differ from the
legacy token. The legacy `COLLECTIVE_API_TOKEN` (or `ESTIMA_API_TOKEN` /
`ATLAS_API_TOKEN`) remains organization-read capable. It is read-only by
default because the old deployment-wide token does not identify a publisher.
For a genuine single-instance migration only, setting
`COLLECTIVE_LEGACY_INSTANCE_ID` binds that legacy token to exactly one instance
for publishing; any other payload `instance_id` receives `403`. Do not set this
to make a shared multi-instance token impersonate several instances. Provision
per-instance publisher credentials with the admin API and update each
FCAPSule's existing Collective token setting instead. Until those new
credentials are issued and FCAPSule clients are rotated, an unbound old token
can continue reading but its writes will be rejected. Do not deploy the
access-control revision to an active shared instance before that migration is
prepared. An environment-bound legacy token is not a managed database key and
cannot use the self-rotation endpoint; issue a managed publisher key and remove
the legacy binding to complete that migration.

This release retains the current single-organization deployment model. All
authenticated readers, including publishers, can inspect cross-instance cases
and patterns. Credentials do not create organization isolation; do not expose
one service/database to multiple organizations without adding organization
ownership to every case, pattern, detail lookup, and mutation first. The admin
credential is a trusted control-plane boundary: its holder can provision
publisher credentials for any instance. Audit rows record key IDs, actions,
instance, role, and time, never bearer secrets.

## Shared Deployment

Kubernetes manifests remain in `deploy/kubernetes/estima/` to preserve current
deployment paths and DNS. The API image is built from this repository's
Dockerfile and published as both `ghcr.io/hi-io/collective` and the compatible
`ghcr.io/hi-io/estima` by `.github/workflows/publish-image.yml` on `main` and
version tags. The workflow also supports a manual publish for a full commit
SHA, and requires the latest `Tests` run for that SHA to pass. GitHub exposes
manual dispatch after the workflow revision is present on the default branch.
Deploy a pinned image tag or digest with:

```sh
COLLECTIVE_IMAGE_REF="ghcr.io/hi-io/collective:sha-$(git rev-parse HEAD)" \
  deploy/kubernetes/estima/apply.sh
```

`ESTIMA_IMAGE_REF` and `ghcr.io/hi-io/estima` images remain accepted during
the transition. Deployment and Service names (`estima`, `atlas`), the
namespace, database, PVC, tables, and FCAPSule `FCAPSULE_ESTIMA_*` settings
remain compatible.

The long-lived compatibility profile keeps the current `fcapsule-atlas`
namespace and checks for the existing Atlas database objects. It deploys
Collective alongside the Atlas API, waits for Collective readiness, then
applies the existing `estima` Service and repoints legacy `atlas` DNS to
Collective. It never applies
the database or PVC manifests during that migration. See
[`docs/migration-from-atlas.md`](docs/migration-from-atlas.md) before applying
it.

For a fresh database, `postgres.yaml` and `storage.yaml` define the
`estima-postgres` Service, `estima` database/user, and
`estima-postgres-data` PVC. They are deliberately excluded from the routine
Kustomize/apply path: set an approved durable StorageClass and apply them only
for a new installation with `deploy/kubernetes/estima/install-fresh.sh`.
Create the `estima-postgres` Secret and `atlas-runtime` Secret through the
approved secret manager first. The runtime Secret's `DATABASE_URL` must point
to `estima-postgres` and use database/user `estima`. Configure a strong
`COLLECTIVE_ADMIN_TOKEN` for credential management; retain
`COLLECTIVE_API_TOKEN` only as a read-compatible legacy key or a deliberately
single-instance-bound publisher during migration. `ESTIMA_API_TOKEN` and
`ATLAS_API_TOKEN` remain read-compatible fallbacks. Never apply the fresh
database files over an existing database as a way to rename it.

`deploy/kubernetes/estima-test/` provides an isolated A/B integration profile
for the synthetic `fcapsule-atlas-test` database and PVC. Its test cases must
not be mixed into the shared Collective database.

## Development

Install the project and run all unit and PostgreSQL integration tests:

```sh
python -m pip install -e '.[test]'
COLLECTIVE_TEST_DATABASE_URL='postgresql://estima:password@localhost:5432/estima_test' \
  python -m unittest discover -s tests -v
```

`ESTIMA_TEST_DATABASE_URL` and `ATLAS_TEST_DATABASE_URL` remain accepted as
legacy test settings. Without a database URL, PostgreSQL-specific tests are
skipped; CI starts PostgreSQL and runs them. The integration test database must be
disposable because its migration creates the compatibility tables.
