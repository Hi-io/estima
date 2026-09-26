# Collective Credential Migration

This runbook describes the access-control cutover; it does not deploy an image
or create credentials. The legacy service cannot issue managed publisher
credentials, so they cannot exist before the access-control API is running. For
a verified single-active-instance deployment, use the temporary legacy-binding
bridge below: bind the old token to that one instance, deploy, issue a managed
publisher credential, move FCAPSule Settings to it, verify publishing, then
remove the bridge. The bridge is not suitable for multiple active publishers.

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

## Temporary Single-Instance Bridge

Use this bridge only when one FCAPSule instance is actively publishing to this
Collective deployment. Compare the complete database ID list with the active
FCAPSule Settings and its persisted instance identity. Resolve unexpected IDs
or any second active publisher before proceeding. If multiple FCAPSule instances
are active, do not bind the shared legacy token; use a separately staged
credential-provisioning path or a planned write pause instead.

The bridge temporarily gives the existing legacy token publisher rights for
one verified instance while retaining its organization-wide read access. The
token cannot publish for a caller-selected or other instance. The legacy image
does not have a credential-issuance endpoint, so a managed publisher must be
issued after the access-control API is running.

1. Create the dedicated namespace-scoped `collective-admin-runtime` Secret
   with a strong `COLLECTIVE_ADMIN_TOKEN` through the approved secret manager.
   It must differ from every legacy API token and remain unavailable to
   FCAPSule pods.
2. Reconfirm the single active FCAPSule instance ID against the database and
   persisted FCAPSule identity immediately before cutover. The currently
   observed production ID above is only a local-only observation; rediscover
   it rather than copying it into configuration.
3. Temporarily create the `collective-legacy-binding` ConfigMap with its
   `COLLECTIVE_LEGACY_INSTANCE_ID` key set to that verified ID. Do not put a
   bearer token in this ConfigMap. The ConfigMap is intentionally absent from
   the repository and is not applied by the routine deployment script.
4. Deploy the access-control image. The old FCAPSule token is now a temporary
   publisher only for the bound ID; mismatched instance IDs are rejected. The
   separate admin token can issue managed credentials but cannot publish.
5. Using an approved credential-management client, call
   `POST /v1/admin/instances/{instance_id}/publisher-credentials` for the
   verified ID. Capture the one-time publisher secret directly into the
   approved secret manager. Do not print, paste, persist, or log the response.
   Record the returned key ID for later revocation.
6. Update FCAPSule Settings and its runtime Secret to use the new managed
   publisher credential, then roll the FCAPSule workload. Publisher
   credentials can both publish for their bound instance and read
   organization-wide history, preserving Collective browsing. Confirm a new
   publication succeeds, shared reads still work, and a mismatched instance ID
   is rejected.
7. Remove the temporary `collective-legacy-binding` ConfigMap and restart the
   Collective API Deployment so the environment binding is cleared. Verify the
   old token remains read-capable but can no longer publish, while the managed
   publisher continues to publish and read. Retain the old token only if it is
   still needed as a read-only credential; otherwise retire it through the
   approved secret-management process.

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
