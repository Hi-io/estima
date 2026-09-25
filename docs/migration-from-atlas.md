# In-Place Migration From Atlas

This profile upgrades the existing Atlas API without moving or rebuilding its
PostgreSQL data. For the current cluster, the live database contains 152
distinct episodes and 305 revisions; counts may increase before migration.

## Preserved Storage Contract

The compatibility profile keeps the existing namespace `fcapsule-atlas`,
PostgreSQL Service `atlas-postgres`, PVC `atlas-postgres-data`, database/user
`atlas`, runtime Secret `atlas-runtime`, and PostgreSQL Secret `atlas-postgres`.
It keeps the `atlas_cases`, `atlas_case_patterns`, and
`atlas_schema_migrations` tables unchanged. Do not apply this repository's
fresh-install `storage.yaml` or `postgres.yaml` during migration: those files
define a new Estima database and PVC and are not a data migration.

The `/v1` API request and response schema is unchanged. Estima recognizes the
existing `ATLAS_API_TOKEN` runtime Secret key; the new process receives it as
`ESTIMA_API_TOKEN`. No LLM, model SDK, model API key, or model call exists in
the service. Existing FCAPSule instances continue to make all model calls.

## Rollout

1. Confirm the PVC is Bound and take a database backup using the cluster's
   approved backup process. Do not delete, rename, or recreate the PVC.
2. Record the current counts:

   ```sql
   SELECT count(*) AS revisions FROM atlas_cases;
   SELECT count(*) AS episodes
   FROM (SELECT DISTINCT instance_id, episode_id FROM atlas_cases) e;
   SELECT count(*) AS patterns FROM atlas_case_patterns;
   ```

3. Publish an Estima image from this repository's `main` branch or a version
   tag. Pin the deployment to an immutable GHCR image tag or digest.
4. Run the additive rollout from an Estima repository checkout:

   ```bash
   ESTIMA_IMAGE_REF="ghcr.io/hi-io/estima:sha-$(git rev-parse HEAD)" \
     deploy/kubernetes/estima/apply.sh
   ```

   The script requires the existing Secrets, PostgreSQL Service, and PVC. It
   creates `deployment/estima`, waits for `/healthz` readiness, and only then
   applies Service `estima` and changes Service `atlas` to select Estima. It
   never applies the database or PVC manifests and never removes the old API.
5. Verify `service/estima` and the legacy
   `http://atlas.fcapsule-atlas.svc.cluster.local:8080` both resolve to the
   healthy Estima pod. Compare the saved database counts, then perform
   read-only searches for known retained cases through `/v1/search`.
6. Keep `deployment/atlas` available until all FCAPSule clients and smoke tests
   are confirmed. The `atlas` Service is a compatibility alias and should only
   be retired in a separate change after clients have moved to
   `http://estima.fcapsule-atlas.svc.cluster.local:8080`.

Before retiring the old Atlas Deployment, rollback is possible by restoring
the old selector while that Deployment still exists:

```bash
kubectl -n fcapsule-atlas patch service atlas --type merge \
  -p '{"spec":{"selector":{"app.kubernetes.io/name":"atlas"}}}'
```

Do not run the cleanup script as part of migration. It removes runtime
workloads and Services, not persistent volumes, but would interrupt clients.

## Fresh Estima Database

For a new installation only, `storage.yaml` and `postgres.yaml` provide a
separate `estima-postgres` Service, database/user `estima`, and
`estima-postgres-data` PVC. Replace the StorageClass placeholder in
`storage.yaml`, create the namespace-scoped Secrets out of band, and apply the
database resources before deploying the API. Set `DATABASE_URL` in
`atlas-runtime` to `postgresql://estima:<url-encoded-password>@estima-postgres.<namespace>.svc.cluster.local:5432/estima`.
The Secret name is retained by the current Kubernetes API profile for
compatibility; the API token key can be named `ATLAS_API_TOKEN` during
transition. New Secret conventions can be introduced separately from the
live data move.

## Isolated Cross-Instance Test

`deploy/kubernetes/estima-test/` targets only the existing
`fcapsule-atlas-test` namespace and its synthetic database/PVC. Its A/B
FCAPSule pods have isolated state volumes and share that test Estima. The
script requires an Estima image ref and immutable FCAPSule source commit:

```bash
ESTIMA_IMAGE_REF="ghcr.io/hi-io/estima:sha-$(git rev-parse HEAD)" \
FCAPSULE_SOURCE_REF=REPLACE_WITH_40_CHARACTER_COMMIT_SHA \
  deploy/kubernetes/estima-test/apply.sh
```

The test profile does not apply PostgreSQL or PVC manifests during updates.
Its test-only `atlas` Service is repointed after Estima is ready so existing
client configuration remains usable.
