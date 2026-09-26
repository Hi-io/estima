CREATE TABLE atlas_episode_tombstones (
    instance_id text NOT NULL,
    episode_id text NOT NULL,
    reason text NOT NULL CHECK (reason IN ('publisher', 'retention')),
    actor_key_id text NOT NULL,
    tombstoned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (instance_id, episode_id)
);

CREATE TABLE atlas_case_lifecycle_audit (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_key_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('episode_withdrawn', 'episode_retention_expired')),
    instance_id text NOT NULL,
    episode_id text NOT NULL,
    deleted_case_count integer NOT NULL CHECK (deleted_case_count >= 0),
    retention_days integer CHECK (retention_days BETWEEN 1 AND 3650),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((action = 'episode_withdrawn' AND retention_days IS NULL)
        OR (action = 'episode_retention_expired' AND retention_days IS NOT NULL))
);

CREATE INDEX atlas_episode_tombstones_time_idx ON atlas_episode_tombstones (tombstoned_at DESC);
CREATE INDEX atlas_case_lifecycle_audit_instance_idx
    ON atlas_case_lifecycle_audit (instance_id, occurred_at DESC);
