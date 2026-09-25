# Estima

**FCAPSule investigates; Estima remembers.** Estima is a shared, durable
memory API for curated operational cases prepared by FCAPSule instances. It
stores and retrieves background without deciding what that background means.

Each FCAPSule instance runs its own models, analyzes evidence, decides what to
share, and interprets retrieved history. Estima stores observations separately
from hypotheses and returns both in their original categories. It does not
execute LLMs, call model providers, require model API keys, or incur model-token
usage. Multiple FCAPSule instances may use one Estima; the integration is
optional, so FCAPSule continues to work without it.

## Run Locally

Estima requires PostgreSQL 14 or newer and Python 3.11 or newer.

```sh
export DATABASE_URL='postgresql://estima:password@localhost:5432/estima'
export ESTIMA_API_TOKEN="$(openssl rand -hex 32)"
python -m pip install -e .
python -m uvicorn estima.app:app --host 0.0.0.0 --port 8080
```

At startup Estima applies versioned SQL migrations and becomes ready only
after PostgreSQL is reachable. `/healthz` is unauthenticated; all data routes
require `Authorization: Bearer $ESTIMA_API_TOKEN`. `DATABASE_URL` is the
database connection setting. For existing Atlas installs, `ATLAS_API_TOKEN`
is temporarily accepted as a fallback; prefer `ESTIMA_API_TOKEN` for new
deployments.

Run the container locally:

```sh
docker build -t estima-service:local .
docker run --rm -p 8080:8080 \
  -e DATABASE_URL='postgresql://estima:password@host.docker.internal:5432/estima' \
  -e ESTIMA_API_TOKEN="$ESTIMA_API_TOKEN" estima-service:local
```

## API

The versioned `/v1` wire contract remains compatible with the existing
FCAPSule client:

- `POST /v1/cases` stores a versioned case envelope with `instance_id`,
  `episode_id`, `revision`, timezone-aware `observed_at`, `scope`, `summary`,
  `observations`, and `hypotheses`. Replaying the same
  `(instance_id, episode_id, revision)` payload returns the same case; a
  different payload for that key returns `409`.
- `GET /v1/cases/{id}` returns a stored case.
- `POST /v1/search` retrieves latest revisions by scope, time, text, or exact
  fingerprint. Ranking scores are heuristics, not probabilities.
- `GET /v1/patterns` returns repeated typed observations; `GET
  /v1/patterns/{id}` returns the aggregate and member cases. Co-occurrence is
  not reported as a shared cause.

Observation facts and unverified hypotheses use separate fields and remain
separate in storage and responses. Estima does not infer causes, resolutions,
or interpretations. Scope includes environment, cluster, namespace, service,
workload, CNFC ID, and VNFC ID.

Bodies are limited to 40 KiB; observations must be scalar, and bounds reject
secret-looking fields/values and nested raw telemetry. These checks are
defense in depth, not a substitute for upstream data minimization, TLS,
authorization, backups, and secret management. Authentication uses a shared
service token; it is not per-instance authorization.

## Shared Deployment

Kubernetes manifests live in `deploy/kubernetes/estima/`. The API image is
built from this repository's Dockerfile and published to
`ghcr.io/hi-io/estima` by `.github/workflows/publish-image.yml` on `main` and
version tags. Deploy a pinned image tag or digest with:

```sh
ESTIMA_IMAGE_REF="ghcr.io/hi-io/estima:sha-$(git rev-parse HEAD)" \
  deploy/kubernetes/estima/apply.sh
```

The long-lived compatibility profile keeps the current `fcapsule-atlas`
namespace and checks for the existing Atlas database objects. It deploys
Estima alongside the Atlas API, waits for Estima readiness, then adds the new
`estima` Service and repoints legacy `atlas` DNS to Estima. It never applies
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
to `estima-postgres` and use database/user `estima`; its
`ATLAS_API_TOKEN` key is a temporary compatibility detail. Never apply the
fresh database files over an existing database as a way to rename it.

`deploy/kubernetes/estima-test/` provides an isolated A/B integration profile
for the synthetic `fcapsule-atlas-test` database and PVC. Its test cases must
not be mixed into the shared Estima database.

## Development

Install the project and run all unit and PostgreSQL integration tests:

```sh
python -m pip install -e '.[test]'
ESTIMA_TEST_DATABASE_URL='postgresql://estima:password@localhost:5432/estima_test' \
  python -m unittest discover -s tests -v
```

Without `ESTIMA_TEST_DATABASE_URL`, PostgreSQL-specific tests are skipped; CI
starts PostgreSQL and runs them. The integration test database must be
disposable because its migration creates the compatibility tables.
