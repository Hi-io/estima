# Collective Credential Migration

This runbook prepares the existing Kubernetes deployment for the access-control
revision. It does not deploy an image or create credentials. Do not roll out the
access-control image until every publishing FCAPSule has a publisher credential
bound to its verified instance ID and its runtime Secret has been updated.

## Trust Boundary

- A publisher credential is bound server-side to one instance. It can publish
  only for that instance and can read organization-wide Collective history.
- A reader credential can read organization-wide history but cannot publish.
- The admin credential issues and revokes credentials; it is not a publisher
  credential and must never be configured as an FCAPSule token.
- Existing `COLLECTIVE_API_TOKEN`, `ESTIMA_API_TOKEN`, and `ATLAS_API_TOKEN`
  fallbacks remain read-capable but are read-only unless the operator
  deliberately configures `COLLECTIVE_LEGACY_INSTANCE_ID` for a genuine
  single-instance migration. An unbound token does not become a publisher.

The shared production `atlas-runtime` Secret currently has `DATABASE_URL` and
`ATLAS_API_TOKEN`. The Deployment reads the admin token from a separate,
optional `collective-admin-runtime` Secret so it is not added to the legacy
runtime Secret used by other workloads. Its absence does not prevent startup or
grant write access. Provision it out of band using the approved secret manager.
Never put credentials in this repository, ConfigMaps, shell history, command
arguments, or CI logs.

## Verify Instance IDs

Before provisioning, use an authorized read-only database session to enumerate
stored instance IDs:

```sql
SELECT DISTINCT instance_id FROM atlas_cases ORDER BY instance_id;
```

Compare the complete result with the configured or persisted
`FCAPSULE_ESTIMA_INSTANCE_ID` for every active FCAPSule publisher. Resolve any
missing, extra, or mismatched ID before proceeding. Do not infer an instance ID
from a case payload supplied by a caller, and do not use an ID copied from an
old manifest without rechecking both the database and FCAPSule configuration.

The currently observed production value is
`fcapsule-9ddb5d57-d0e2-4f13-88d5-8c8d36f5089c`. This is a local-only observation
from the current deployment, not a service default or reusable manifest value;
rediscover and verify it at migration time. The test profile's explicitly
configured IDs are `estima-dev-a` and `estima-dev-b` and apply only to its
isolated test database.

## Provision And Rotate

1. Create the dedicated namespace-scoped `collective-admin-runtime` Secret
   with a strong `COLLECTIVE_ADMIN_TOKEN` through the approved secret manager.
   It must be different from every legacy API token and remain unavailable to
   FCAPSule pods.
2. For each verified instance ID, call
   `POST /v1/admin/instances/{instance_id}/publisher-credentials` using an
   approved credential-management client. The response contains a one-time
   publisher secret. Capture it directly into the approved secret manager;
   do not print, paste, persist, or log the response. Keep the returned key ID
   with the deployment record so it can be revoked later.
3. Update that instance's FCAPSule runtime Secret with its own publisher
   credential and roll only that FCAPSule workload. FCAPSule uses the same
   token for publishing and Collective browsing; publisher credentials retain
   organization-wide read access. Do not share a publisher key between
   instances.
4. Confirm that the rotated instance can publish its own cases and browse
   cases from other instances. Confirm a mismatched instance ID is rejected.
   Only then proceed with the Collective access-control image rollout.
5. Keep the old unbound token available for read-only rollback during the
   migration window. It cannot publish on the new image. After all clients are
   verified, retire the old token through the approved secret-management
   process.

For a controlled rotation of a managed publisher, use
`POST /v1/credentials/rotate` with that publisher credential. The old and new
keys overlap for five minutes by default (configurable up to one hour); revoke
the old key with `DELETE /v1/admin/credentials/{key_id}` when appropriate.
Issuance, rotation, and revocation audit metadata contains key IDs, roles,
instance IDs, actions, and timestamps, never bearer secrets.

## Kubernetes References

`deploy/kubernetes/estima/api-deployment.yaml` reads
`COLLECTIVE_ADMIN_TOKEN` optionally from the dedicated
`collective-admin-runtime` Secret. It also has an
optional ConfigMap reference for `COLLECTIVE_LEGACY_INSTANCE_ID`; no such
ConfigMap is included or applied by default. Leave it absent for the normal
per-instance publisher migration. Only create it temporarily after confirming
the deployment is genuinely single-instance, and remove it after moving to a
managed publisher credential. Never set it to cover multiple publishers.

The isolated test profile references separate optional Secrets
`fcapsule-estima-publisher-dev-a` and
`fcapsule-estima-publisher-dev-b`, each with a `TOKEN` key. Create them out of
band with credentials issued for exactly `estima-dev-a` and `estima-dev-b` in
the test database. If either Secret is absent, that pod has no publisher token;
the manifest does not fall back to the shared legacy token or grant write
access. Do not use production credentials in the test namespace.
