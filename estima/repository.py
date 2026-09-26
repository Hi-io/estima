from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalize import observation_pattern_id, search_document


MIGRATIONS = Path(__file__).with_name("migrations")
MAX_SEARCH_CANDIDATES = 500
MAX_PATTERN_MEMBERS = 10
SCOPE_KEYS = ("environment", "cluster", "namespace", "service", "workload", "cnfc_id", "vnfc_id")
TOKEN_RE = re.compile(r"[a-z0-9_]+")
LIFECYCLE_WRITE_LOCK = 620018272
EPISODE_LOCK_SEED = 620018273
MAX_RETENTION_DAYS = 3650


class IdempotencyConflict(ValueError):
    pass


class InvalidCursor(ValueError):
    pass


class CredentialUnavailable(ValueError):
    pass


class EpisodeUnavailable(ValueError):
    pass


def _encode_cursor(value: dict[str, Any]) -> str:
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, kind: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", cursor):
        raise InvalidCursor("Invalid pagination cursor")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise InvalidCursor("Invalid pagination cursor") from None
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise InvalidCursor("Invalid pagination cursor")
    return value


def _cursor_time_id(cursor: str, kind: str) -> tuple[datetime, uuid.UUID]:
    value = _decode_cursor(cursor, kind)
    try:
        observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        case_id = uuid.UUID(value["id"])
    except (KeyError, AttributeError, TypeError, ValueError):
        raise InvalidCursor("Invalid pagination cursor") from None
    if observed_at.tzinfo is None:
        raise InvalidCursor("Invalid pagination cursor")
    return observed_at, case_id


def _search_cursor_key(cursor: str) -> tuple[float, str, str]:
    value = _decode_cursor(cursor, "search")
    try:
        score = float(value["score"])
        observed_at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        case_id = str(uuid.UUID(value["id"]))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise InvalidCursor("Invalid pagination cursor") from None
    if not math.isfinite(score) or not 0 <= score <= 1 or observed_at.tzinfo is None:
        raise InvalidCursor("Invalid pagination cursor")
    return score, _utc(observed_at) or "", case_id


def _utc(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def public_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "schema_version": row["schema_version"],
        "normalization_version": row["normalization_version"],
        "instance_id": row["instance_id"],
        "episode_id": row["episode_id"],
        "revision": row["revision"],
        "observed_at": _utc(row["observed_at"]),
        "scope": row["scope"],
        "summary": row["summary"],
        "observations": row["observations"],
        "hypotheses": row["hypotheses"],
        "fingerprint": row["fingerprint"],
    }


class PostgresEstimaRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL", "")
        if not self.dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, connect_timeout=3, row_factory=dict_row)

    @staticmethod
    def _lock_lifecycle_write(conn: Any, instance_id: str, episode_id: str) -> None:
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LIFECYCLE_WRITE_LOCK,))
        lock_key = json.dumps([instance_id, episode_id], ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
            (lock_key, EPISODE_LOCK_SEED),
        )

    def migrate(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS atlas_schema_migrations "
                "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute("SELECT pg_advisory_xact_lock(620018271)")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                existing = conn.execute(
                    "SELECT 1 FROM atlas_schema_migrations WHERE version = %s", (version,)
                ).fetchone()
                if existing:
                    continue
                for statement in path.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute("INSERT INTO atlas_schema_migrations (version) VALUES (%s)", (version,))

    def healthcheck(self) -> bool:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM atlas_schema_migrations LIMIT 1").fetchone()
        return True

    @staticmethod
    def _credential_digest(secret: str) -> bytes:
        return hashlib.sha256(secret.encode("utf-8")).digest()

    def authenticate_token(self, secret: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT key_id, role, instance_id FROM atlas_api_credentials
                   WHERE secret_hash = %s AND revoked_at IS NULL
                     AND (valid_until IS NULL OR valid_until > clock_timestamp())""",
                (self._credential_digest(secret),),
            ).fetchone()
        if row is None:
            return None
        return {"key_id": str(row["key_id"]), "role": row["role"], "instance_id": row["instance_id"]}

    def create_credential(
        self,
        *,
        instance_id: str | None,
        role: str,
        actor_key_id: str,
    ) -> dict[str, Any]:
        if role not in {"publisher", "reader"} or (role == "publisher") != (instance_id is not None):
            raise ValueError("Invalid credential role or instance binding")
        key_id = uuid.uuid4()
        secret = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO atlas_api_credentials (key_id, secret_hash, role, instance_id)
                   VALUES (%s, %s, %s, %s)""",
                (key_id, self._credential_digest(secret), role, instance_id),
            )
            conn.execute(
                """INSERT INTO atlas_credential_audit
                   (actor_key_id, action, subject_key_id, instance_id, role)
                   VALUES (%s, 'credential_issued', %s, %s, %s)""",
                (actor_key_id, key_id, instance_id, role),
            )
        return {
            "key_id": str(key_id),
            "secret": secret,
            "role": role,
            "instance_id": instance_id,
        }

    def rotate_credential(
        self,
        *,
        key_id: str,
        actor_key_id: str,
        overlap_seconds: int,
    ) -> dict[str, Any]:
        try:
            old_key_id = uuid.UUID(key_id)
        except (ValueError, AttributeError):
            raise CredentialUnavailable("Credential cannot be rotated") from None
        new_key_id = uuid.uuid4()
        secret = secrets.token_urlsafe(32)
        with self._connect() as conn:
            current = conn.execute(
                """SELECT role, instance_id, superseded_by FROM atlas_api_credentials
                   WHERE key_id = %s AND revoked_at IS NULL
                     AND (valid_until IS NULL OR valid_until > clock_timestamp())
                   FOR UPDATE""",
                (old_key_id,),
            ).fetchone()
            if current is None or current["superseded_by"] is not None:
                raise CredentialUnavailable("Credential cannot be rotated")
            if current["role"] != "publisher" or not current["instance_id"]:
                raise CredentialUnavailable("Only a publisher credential can be rotated")
            overlap_until = conn.execute(
                "SELECT clock_timestamp() + (%s * interval '1 second') AS value",
                (overlap_seconds,),
            ).fetchone()["value"]
            conn.execute(
                """INSERT INTO atlas_api_credentials (key_id, secret_hash, role, instance_id)
                   VALUES (%s, %s, %s, %s)""",
                (new_key_id, self._credential_digest(secret), current["role"], current["instance_id"]),
            )
            conn.execute(
                """UPDATE atlas_api_credentials
                   SET valid_until = %s, superseded_by = %s WHERE key_id = %s""",
                (overlap_until, new_key_id, old_key_id),
            )
            conn.execute(
                """INSERT INTO atlas_credential_audit
                   (actor_key_id, action, subject_key_id, related_key_id, instance_id, role, overlap_until)
                   VALUES (%s, 'credential_rotated', %s, %s, %s, %s, %s)""",
                (actor_key_id, new_key_id, old_key_id, current["instance_id"], current["role"], overlap_until),
            )
        return {
            "key_id": str(new_key_id),
            "secret": secret,
            "role": current["role"],
            "instance_id": current["instance_id"],
            "old_credential_valid_until": overlap_until.isoformat().replace("+00:00", "Z"),
        }

    def revoke_credential(self, *, key_id: str, actor_key_id: str) -> None:
        try:
            parsed_key_id = uuid.UUID(key_id)
        except (ValueError, AttributeError):
            return
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE atlas_api_credentials
                   SET revoked_at = clock_timestamp(), valid_until = clock_timestamp()
                   WHERE key_id = %s AND revoked_at IS NULL
                   RETURNING instance_id, role""",
                (parsed_key_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    """INSERT INTO atlas_credential_audit
                       (actor_key_id, action, subject_key_id, instance_id, role)
                       VALUES (%s, 'credential_revoked', %s, %s, %s)""",
                    (actor_key_id, parsed_key_id, row["instance_id"], row["role"]),
                )

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                self._latest_cte("TRUE") + """
                SELECT
                    (SELECT count(*) FROM (
                        SELECT 1 FROM atlas_cases GROUP BY instance_id, episode_id
                    ) episodes) AS episodes,
                    (SELECT count(*) FROM atlas_cases) AS revisions,
                    (SELECT count(DISTINCT p.pattern_id)
                     FROM latest_cases c
                     JOIN atlas_case_patterns p ON p.case_id = c.id) AS patterns
                """
            ).fetchone()
        return {key: int(row[key]) for key in ("episodes", "revisions", "patterns")}

    def create_case(self, case: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        case_id = uuid.uuid4()
        document = search_document(case)
        values = (
            case_id,
            case["schema_version"],
            case["normalization_version"],
            case["instance_id"],
            case["episode_id"],
            case["revision"],
            case["observed_at"],
            Jsonb(case["scope"]),
            case["summary"],
            Jsonb(case["observations"]),
            Jsonb(case["hypotheses"]),
            case["fingerprint"],
            document,
        )
        with self._connect() as conn:
            self._lock_lifecycle_write(conn, case["instance_id"], case["episode_id"])
            withdrawn = conn.execute(
                """SELECT 1 FROM atlas_episode_tombstones
                   WHERE instance_id = %s AND episode_id = %s""",
                (case["instance_id"], case["episode_id"]),
            ).fetchone()
            if withdrawn is not None:
                raise EpisodeUnavailable("This episode is no longer available")
            inserted = conn.execute(
                """INSERT INTO atlas_cases
                   (id, schema_version, normalization_version, instance_id, episode_id, revision, observed_at,
                    scope, summary, observations, hypotheses, fingerprint, search_document)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (instance_id, episode_id, revision) DO NOTHING
                   RETURNING id""",
                values,
            ).fetchone()
            if inserted:
                unique_patterns: dict[str, dict[str, Any]] = {}
                for observation in case["observations"]:
                    pattern_id = observation_pattern_id(observation)
                    unique_patterns[pattern_id] = observation
                for pattern_id, observation in unique_patterns.items():
                    conn.execute(
                        """INSERT INTO atlas_case_patterns
                           (case_id, pattern_id, kind, key, value, unit)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (case_id, pattern_id, observation["kind"], observation["key"],
                         Jsonb(observation["value"]), observation["unit"]),
                    )
                return {"case": public_case({**case, "id": case_id}), "created": True}

            existing = conn.execute(
                """SELECT * FROM atlas_cases
                   WHERE instance_id = %s AND episode_id = %s AND revision = %s""",
                (case["instance_id"], case["episode_id"], case["revision"]),
            ).fetchone()
            existing_case = public_case(existing)
            if any(existing_case[key] != value for key, value in case.items()):
                raise IdempotencyConflict("This idempotency key already has a different case payload")
            return {"case": existing_case, "created": False}

    def withdraw_episode(self, *, instance_id: str, episode_id: str, actor_key_id: str) -> int:
        with self._connect() as conn:
            self._lock_lifecycle_write(conn, instance_id, episode_id)
            tombstone = conn.execute(
                """INSERT INTO atlas_episode_tombstones
                   (instance_id, episode_id, reason, actor_key_id)
                   VALUES (%s, %s, 'publisher', %s)
                   ON CONFLICT (instance_id, episode_id) DO NOTHING
                   RETURNING instance_id""",
                (instance_id, episode_id, actor_key_id),
            ).fetchone()
            deleted = conn.execute(
                "DELETE FROM atlas_cases WHERE instance_id = %s AND episode_id = %s",
                (instance_id, episode_id),
            ).rowcount
            if tombstone is not None:
                conn.execute(
                    """INSERT INTO atlas_case_lifecycle_audit
                       (actor_key_id, action, instance_id, episode_id, deleted_case_count)
                       VALUES (%s, 'episode_withdrawn', %s, %s, %s)""",
                    (actor_key_id, instance_id, episode_id, deleted),
                )
        return int(deleted)

    def purge_expired_cases(self, *, retention_days: int) -> int:
        if not 1 <= retention_days <= MAX_RETENTION_DAYS:
            raise ValueError(f"retention_days must be between 1 and {MAX_RETENTION_DAYS}")
        deleted_total = 0
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_WRITE_LOCK,))
            while True:
                episodes = conn.execute(
                    """SELECT instance_id, episode_id
                       FROM atlas_cases
                       GROUP BY instance_id, episode_id
                       HAVING max(created_at) <= clock_timestamp() - (%s * interval '1 day')
                       ORDER BY instance_id, episode_id
                       LIMIT 500""",
                    (retention_days,),
                ).fetchall()
                if not episodes:
                    break
                for episode in episodes:
                    instance_id = episode["instance_id"]
                    episode_id = episode["episode_id"]
                    tombstone = conn.execute(
                        """INSERT INTO atlas_episode_tombstones
                           (instance_id, episode_id, reason, actor_key_id)
                           VALUES (%s, %s, 'retention', 'system-retention')
                           ON CONFLICT (instance_id, episode_id) DO NOTHING
                           RETURNING instance_id""",
                        (instance_id, episode_id),
                    ).fetchone()
                    deleted = conn.execute(
                        "DELETE FROM atlas_cases WHERE instance_id = %s AND episode_id = %s",
                        (instance_id, episode_id),
                    ).rowcount
                    deleted_total += int(deleted)
                    if deleted and tombstone is not None:
                        conn.execute(
                            """INSERT INTO atlas_case_lifecycle_audit
                               (actor_key_id, action, instance_id, episode_id, deleted_case_count, retention_days)
                               VALUES ('system-retention', 'episode_retention_expired', %s, %s, %s, %s)""",
                            (instance_id, episode_id, deleted, retention_days),
                        )
        return deleted_total

    @staticmethod
    def _filters(
        scope: dict[str, str] | None = None,
        instance_id: str | None = None,
        observed_after: datetime | None = None,
        before: datetime | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instance_id:
            clauses.append("c.instance_id = %s")
            params.append(instance_id)
        for key in SCOPE_KEYS:
            if scope and scope.get(key) is not None:
                clauses.append("c.scope ->> %s = %s")
                params.extend((key, scope[key]))
        if observed_after:
            clauses.append("c.observed_at >= %s")
            params.append(observed_after)
        if before:
            clauses.append("c.observed_at <= %s")
            params.append(before)
        return (" AND ".join(clauses) if clauses else "TRUE", params)

    @staticmethod
    def _latest_cte(where: str) -> str:
        return f"""WITH latest_cases AS (
            SELECT DISTINCT ON (c.instance_id, c.episode_id) c.*
            FROM atlas_cases c
            WHERE {where}
            ORDER BY c.instance_id, c.episode_id, c.revision DESC, c.observed_at DESC
        )"""

    def search(
        self,
        *,
        scope: dict[str, str] | None = None,
        query: str | None = None,
        fingerprint: str | None = None,
        instance_id: str | None = None,
        observed_after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        cursor_key = _search_cursor_key(cursor) if cursor else None
        where, params = self._filters(scope, instance_id, observed_after, before)
        terms = list(dict.fromkeys(TOKEN_RE.findall((query or "").casefold())))[:20]
        text_clause = ""
        if terms or fingerprint:
            parts: list[str] = []
            if fingerprint:
                parts.append("c.fingerprint = %s")
                params.append(fingerprint)
            if terms:
                parts.append("c.search_document ILIKE ANY(%s)")
                params.append([f"%{term}%" for term in terms])
            text_clause = f" AND ({' OR '.join(parts)})"
        order = "c.observed_at DESC"
        if fingerprint:
            order = "(c.fingerprint = %s) DESC, c.observed_at DESC"
            params.append(fingerprint)
        sql = self._latest_cte(where) + f"""
            SELECT c.* FROM latest_cases c
            WHERE TRUE{text_clause}
            ORDER BY {order}
            LIMIT %s
        """
        params.append(MAX_SEARCH_CANDIDATES)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        scored = []
        for row in rows:
            record = public_case(row)
            text = row["search_document"].casefold()
            exact_fingerprint = bool(fingerprint and row["fingerprint"] == fingerprint)
            matched = sum(1 for term in terms if term in text)
            coverage = matched / len(terms) if terms else 0.0
            phrase_bonus = 0.25 if query and " ".join(query.casefold().split()) in text else 0.0
            score = 1.0 if exact_fingerprint else min(1.0, coverage * 0.75 + phrase_bonus)
            relation = "fingerprint_match" if exact_fingerprint else (
                "lexical_similarity" if score > 0 else "recent_in_scope"
            )
            scored.append({**record, "score": round(score, 4), "relation": relation})
        scored.sort(key=lambda item: (item["score"], item["observed_at"] or "", item["id"]), reverse=True)
        if cursor_key is not None:
            scored = [item for item in scored if (item["score"], item["observed_at"] or "", item["id"]) < cursor_key]
        page = scored[:limit]
        has_more = len(scored) > limit
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_cursor({
                "kind": "search", "score": last["score"],
                "observed_at": last["observed_at"], "id": last["id"],
            })
        return {"cases": page, "limit": limit, "has_more": has_more, "next_cursor": next_cursor}

    def list_cases(
        self,
        *,
        scope: dict[str, str] | None = None,
        query: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        where, params = self._filters(scope)
        terms = list(dict.fromkeys(TOKEN_RE.findall((query or "").casefold())))[:20]
        match_clause = ""
        if terms:
            match_clause = " AND c.search_document ILIKE ANY(%s)"
            params.append([f"%{term}%" for term in terms])
        cursor_clause = ""
        if cursor:
            observed_at, case_id = _cursor_time_id(cursor, "cases")
            cursor_clause = " AND (c.observed_at, c.id) < (%s, %s)"
            params.extend((observed_at, case_id))
        sql = self._latest_cte(where) + f"""
            SELECT c.* FROM latest_cases c
            WHERE TRUE{match_clause}{cursor_clause}
            ORDER BY c.observed_at DESC, c.id DESC
            LIMIT %s
        """
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_cursor({
                "kind": "cases", "observed_at": _utc(last["observed_at"]), "id": str(last["id"]),
            })
        return {
            "cases": [public_case(row) for row in page],
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def list_patterns(
        self,
        *,
        scope: dict[str, str] | None = None,
        query: str | None = None,
        before: datetime | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        where, params = self._filters(scope, before=before)
        terms = list(dict.fromkeys(TOKEN_RE.findall((query or "").casefold())))[:20]
        match_clause = ""
        if terms:
            match_clause = " AND c.search_document ILIKE ANY(%s)"
            params.append([f"%{term}%" for term in terms])
        sql = self._latest_cte(where) + f"""
            SELECT p.pattern_id, p.kind, p.key, p.value, p.unit,
                   count(*) AS case_count,
                   count(DISTINCT c.instance_id) AS instance_count,
                   min(c.observed_at) AS first_seen,
                   max(c.observed_at) AS last_seen
            FROM latest_cases c
            JOIN atlas_case_patterns p ON p.case_id = c.id
            WHERE TRUE{match_clause}
            GROUP BY p.pattern_id, p.kind, p.key, p.value, p.unit
            HAVING count(*) >= 2
            ORDER BY case_count DESC, last_seen DESC, p.pattern_id
            LIMIT %s
        """
        params.append(limit + 1)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {
            "patterns": [self._public_pattern(row) for row in rows[:limit]],
            "limit": limit,
            "has_more": len(rows) > limit,
        }

    @staticmethod
    def _public_pattern(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["pattern_id"].strip(),
            "kind": row["kind"],
            "key": row["key"],
            "value": row["value"],
            "unit": row["unit"],
            "case_count": row["case_count"],
            "instance_count": row["instance_count"],
            "first_seen": _utc(row["first_seen"]),
            "last_seen": _utc(row["last_seen"]),
            "similarity": "same_observation",
            "interpretation": "Observed co-occurrence only; this is not evidence of a shared cause.",
        }

    def get_pattern(self, pattern_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{24}", pattern_id):
            return None
        with self._connect() as conn:
            aggregate_sql = self._latest_cte("TRUE") + """
                SELECT p.pattern_id, p.kind, p.key, p.value, p.unit,
                       count(*) AS case_count,
                       count(DISTINCT c.instance_id) AS instance_count,
                       min(c.observed_at) AS first_seen,
                       max(c.observed_at) AS last_seen
                FROM latest_cases c JOIN atlas_case_patterns p ON c.id = p.case_id
                WHERE p.pattern_id = %s
                GROUP BY p.pattern_id, p.kind, p.key, p.value, p.unit
            """
            aggregate = conn.execute(
                aggregate_sql,
                (pattern_id,),
            ).fetchone()
            if not aggregate:
                return None
            rows = conn.execute(
                self._latest_cte("TRUE") + """
                    SELECT c.* FROM latest_cases c
                    JOIN atlas_case_patterns p ON p.case_id = c.id
                    WHERE p.pattern_id = %s
                    ORDER BY c.observed_at DESC
                    LIMIT %s
                """,
                (pattern_id, MAX_PATTERN_MEMBERS + 1),
            ).fetchall()
        return {
            "pattern": self._public_pattern(aggregate),
            "cases": [public_case(row) for row in rows[:MAX_PATTERN_MEMBERS]],
            "has_more": len(rows) > MAX_PATTERN_MEMBERS,
        }

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        try:
            parsed = uuid.UUID(case_id)
        except (ValueError, AttributeError):
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM atlas_cases WHERE id = %s", (parsed,)).fetchone()
        return None if row is None else public_case(row)
