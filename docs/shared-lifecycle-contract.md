# Shared Case Lifecycle Contract (Deliveries 13-14 Proposal)

Status: contract and acceptance vectors only. This document proposes behavior
for future service work; it does not add routes, migrations, or credentials.

## Identity and Read Scope

Collective currently serves one trusted organization. Preserve that simple
deployment model: authenticated readers can search and inspect shared history
across all instances in that organization. Do not silently narrow reads to the
requesting instance; cross-instance recall is the product behavior. If the
service later hosts multiple organizations, every case, pattern aggregate,
detail lookup, and deletion must be organization-scoped before it is exposed.

Publishing identity is different from read scope. A publisher credential is
bound server-side to one `instance_id` and may publish only for that instance.
Never derive authority from the `instance_id` in a body, path, query, or an
outbox event. For compatibility, a matching `instance_id` may remain in the
case envelope; a mismatch is `403`. Organization readers may filter results by
`instance_id`, but that filter never grants publishing or deletion authority.

Recommended principal capabilities:

| Principal | Publish | Read shared history | Withdraw shared data | Rotate credentials |
| --- | --- | --- | --- | --- |
| Instance publisher | Own instance only | Organization-wide | Own instance only | Own instance only |
| Organization reader | No | Organization-wide | No | No |
| Organization admin | Explicitly granted | Organization-wide | Any instance, audited | Manage instance credentials |

The existing deployment-wide bearer token is not an instance identity. Keep
any compatibility use explicitly privileged and temporary; never let a caller
choose a tenant by changing the case payload while presenting that token as a
publisher credential. All data endpoints, including direct case and pattern
lookups, must enforce the same organization boundary.

## Publish and Read

Keep `POST /v1/cases` idempotent on
`(authenticated instance_id, episode_id, revision)`: an identical replay is a
no-op; a different payload under the same key is `409`. A successful new
revision is visible to organization readers. Case listing, detail, search,
pattern membership, and pattern counts are shared organization reads; optional
instance filters only narrow that set. A publisher may read across the
organization because shared history is consumed by every participating
instance.

For self-service withdrawal, use
`DELETE /v1/instances/{instance_id}/episodes/{episode_id}`. The path's instance
must match the authenticated publisher; an admin override requires an audit
reason. Existing read routes retain their shared-org meaning. Provisioning and
rotating credentials belongs to the credential control plane, not the case
payload: it issues an instance-bound replacement, exposes the secret once,
records a bounded old/new overlap, and then revokes the old credential.

The source outbox is at-least-once. Each event has a stable event/idempotency
key, and retrying it with a rotated credential does not change its publisher
identity or create a second revision. Server ordering must be based on stored
revision/tombstone state, never producer wall-clock timestamps.

## Withdrawal, Local Deletion, and Tombstones

`DELETE /v1/instances/{instance_id}/episodes/{episode_id}` is a withdrawal
from Collective's shared copy, not a remote command to erase the originating
FCAPSule's local database. An instance publisher may withdraw only its own
instance's episode. An organization admin may withdraw another instance's
episode only with an audited actor and reason. Repeating the same authorized
withdrawal is idempotent (`204`). The service removes the episode's visible
case revisions and derived pattern membership atomically with recording a
tombstone.

Local forgetting is a separate source-instance operation. It removes local
memory according to local policy and must durably suppress any queued or later
reconstructed publish for the forgotten episode. It retains the withdrawal
outbox event until acknowledged while cancelling or suppressing pending publish
events for that episode. A UI or API action called "delete everywhere" may
explicitly combine local forgetting with a shared withdrawal event; ordinary
shared withdrawal must not erase local evidence.

An episode tombstone is keyed by `(instance_id, episode_id)`, survives deletion
of case rows and indexes, and permanently rejects later publish/replay for
that episode (`410 Gone`). A fresh episode uses a fresh `episode_id`. Do not
expire tombstones while any offline producer, retained outbox, backup, or
retry path can replay old events; retaining the minimal tombstone indefinitely
is the simplest contract. Tombstone checks and case insertion must be
serialized so a publish racing a withdrawal cannot resurrect data. If future
requirements allow restoring the same episode ID, define an explicit
admin-authorized generation/restore protocol first; do not infer restoration
from a larger revision or timestamp.

## Credential Rotation

Credentials resolve to a stable principal (`organization_id`, `instance_id`,
capabilities); rotating a secret must not change that identity or rewrite
queued outbox records. Rotation returns the replacement secret once, accepts
old and new credentials for a short documented overlap, then revokes the old
credential. After revocation the old credential gets `401`; other instances'
credentials and pending idempotent events remain unaffected. Never log or
return stored secrets. A replacement credential retains exactly the old
principal's scope; privilege changes require a separate audited operation.
Credential metadata should carry a non-secret key ID so operators can revoke
one key and audit rotation without recording bearer values.

## Acceptance Matrix

`tests/fixtures/shared_lifecycle_contract.json` is the machine-readable
acceptance vector. A future API conformance test should exercise it against
the real repository and verify these outcomes:

- A publisher can publish as its bound instance but receives `403` for a
  different `instance_id`; an organization reader can read cases from both.
- A reader cannot publish or withdraw. A publisher cannot withdraw another
  instance's episode. An admin withdrawal of another instance is audited.
- Withdrawal removes the episode and its pattern contributions; duplicate
  withdrawal is harmless; an older delayed outbox publish after withdrawal
  receives `410` and leaves the episode absent; a new episode remains writable.
- Rotation preserves principal binding, permits the documented overlap,
  revokes the old key after the overlap, and does not duplicate a retried
  outbox event.
- When more than one organization is introduced, same-org cross-instance reads
  continue to work while every cross-org read/write/delete attempt is denied.

The accompanying fixture test checks that these vectors remain complete and
well-formed. It does not claim that the current API implements this proposal.
