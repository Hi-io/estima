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
define a new Collective database and PVC and are not a data migration.

The `/v1` API routes remain compatible, with additive stats and pagination
endpoints. Collective prefers `COLLECTIVE_API_TOKEN` and accepts
`ESTIMA_API_TOKEN` and `ATLAS_API_TOKEN` for existing deployments. No LLM,
model SDK, model API key, or model call exists in the service. Existing
FCAPSule instances continue to make all model calls.

## Rollout

1. Confirm the PVC is Bound and take a database backup using the cluster's
   approved backup process. Do not delete, rename, or recreate the PVC.
2. Record the current counts. `GET /v1/stats` is the authoritative API view;
   these equivalent queries distinguish episodes, revisions, and current
   distinct patterns:

   ```sql
   SELECT count(*) AS revisions FROM atlas_cases;
   SELECT count(*) AS episodes
   FROM (SELECT DISTINCT instance_id, episode_id FROM atlas_cases) e;
   WITH latest_cases AS (
     SELECT DISTINCT ON (instance_id, episode_id) id
     FROM atlas_cases
     ORDER BY instance_id, episode_id, revision DESC, observed_at DESC
   )
   SELECT count(DISTINCT p.pattern_id) AS patterns
   FROM latest_cases c
   JOIN atlas_case_patterns p ON p.case_id = c.id;
   ```

3. Publish a Collective image from this repository's `main` branch, a version
   tag, or the manual workflow for a fully tested SHA. The manual workflow
   verifies the latest `Tests` run succeeded for the requested SHA. Both
   `ghcr.io/hi-io/collective` and the compatible `ghcr.io/hi-io/estima` image
   are published. Pin the deployment to an immutable GHCR image tag or digest.
4. Run the additive rollout from a Collective repository checkout:

   ```bash
   COLLECTIVE_IMAGE_REF="ghcr.io/hi-io/collective:sha-$(git rev-parse HEAD)" \
     deploy/kubernetes/estima/apply.sh
   ```

   The script requires the existing Secrets, PostgreSQL Service, and PVC. It
   creates `deployment/estima`, waits for `/healthz` readiness, and only then
   applies Service `estima` and changes Service `atlas` to select Collective. It
   never applies the database or PVC manifests and never removes the old API.
5. Verify `service/estima` and the legacy
   `http://atlas.fcapsule-atlas.svc.cluster.local:8080` both resolve to the
   healthy Collective pod. Compare the saved database counts, then perform
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

## Fresh Collective Database

For a new installation only, `storage.yaml` and `postgres.yaml` provide a
separate `estima-postgres` Service, database/user `estima`, and
`estima-postgres-data` PVC. Replace the StorageClass placeholder in
`storage.yaml`, create the namespace-scoped Secrets out of band, and apply the
database resources before deploying the API. Set `DATABASE_URL` in
`atlas-runtime` to `postgresql://estima:<url-encoded-password>@estima-postgres.<namespace>.svc.cluster.local:5432/estima`.
The Secret name is retained by the current Kubernetes API profile for
compatibility; use `COLLECTIVE_API_TOKEN` for new installs. The API continues
to accept `ESTIMA_API_TOKEN` and `ATLAS_API_TOKEN` during the transition.

## Isolated Cross-Instance Test

`deploy/kubernetes/estima-test/` targets only the existing
`fcapsule-atlas-test` namespace and its synthetic database/PVC. Its A/B
FCAPSule pods have isolated state volumes and share that test Collective. The
script requires a Collective image ref and immutable FCAPSule source commit:

```bash
COLLECTIVE_IMAGE_REF="ghcr.io/hi-io/collective:sha-$(git rev-parse HEAD)" \
FCAPSULE_SOURCE_REF=REPLACE_WITH_40_CHARACTER_COMMIT_SHA \
  deploy/kubernetes/estima-test/apply.sh
```

The test profile does not apply PostgreSQL or PVC manifests during updates.
Its test-only `atlas` Service is repointed after Collective is ready so existing
client configuration remains usable.
