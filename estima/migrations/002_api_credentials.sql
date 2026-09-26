CREATE TABLE atlas_api_credentials (
    key_id uuid PRIMARY KEY,
    secret_hash bytea NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('publisher', 'reader')),
    instance_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    valid_until timestamptz,
    revoked_at timestamptz,
    superseded_by uuid REFERENCES atlas_api_credentials(key_id),
    CHECK ((role = 'publisher' AND instance_id IS NOT NULL) OR (role = 'reader' AND instance_id IS NULL))
);

CREATE INDEX atlas_api_credentials_instance_idx ON atlas_api_credentials (instance_id) WHERE instance_id IS NOT NULL;

CREATE TABLE atlas_credential_audit (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_key_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('credential_issued', 'credential_rotated', 'credential_revoked')),
    subject_key_id uuid NOT NULL,
    related_key_id uuid,
    instance_id text,
    role text NOT NULL,
    overlap_until timestamptz,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX atlas_credential_audit_instance_idx ON atlas_credential_audit (instance_id, occurred_at DESC);
