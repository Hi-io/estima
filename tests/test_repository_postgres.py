from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from estima.normalize import normalize_case, observation_pattern_id
from estima.repository import EpisodeUnavailable, IdempotencyConflict, PostgresEstimaRepository


DSN = (
    os.environ.get("COLLECTIVE_TEST_DATABASE_URL")
    or os.environ.get("ESTIMA_TEST_DATABASE_URL")
    or os.environ.get("ATLAS_TEST_DATABASE_URL")
)


def payload(instance: str, episode: str, revision: int, at: datetime, observation: tuple[str, str, int], fingerprint: str | None = None) -> dict:
    kind, key, value = observation
    return {
        "instance_id": instance,
        "episode_id": episode,
        "revision": revision,
        "observed_at": at.isoformat(),
        "scope": {"environment": "test", "cluster": "estima-test-cluster"},
        "summary": f"Recorded {key} observation",
        "observations": [{"kind": kind, "key": key, "value": value, "unit": "count", "source": "test"}],
        "hypotheses": [{"statement": "Unverified test hypothesis", "confidence": None, "supporting_refs": []}],
        "fingerprint": fingerprint,
    }


@unittest.skipUnless(DSN, "set COLLECTIVE_TEST_DATABASE_URL to a disposable PostgreSQL database")
class PostgresRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = PostgresEstimaRepository(DSN)
        self.repo.migrate()
        self.prefix = f"estima-test-{uuid.uuid4().hex}"
        self.cluster = f"cluster-{uuid.uuid4().hex}"

    def tearDown(self) -> None:
        with self.repo._connect() as conn:
            conn.execute("DELETE FROM atlas_cases WHERE instance_id LIKE %s", (f"{self.prefix}%",))
            conn.execute("DELETE FROM atlas_episode_tombstones WHERE instance_id LIKE %s", (f"{self.prefix}%",))
            conn.execute("DELETE FROM atlas_case_lifecycle_audit WHERE instance_id LIKE %s", (f"{self.prefix}%",))

    def add_case(self, episode: str, revision: int, at: datetime, observation: tuple[str, str, int], fingerprint: str | None = None, instance_suffix: str = "a") -> dict:
        case = normalize_case(payload(f"{self.prefix}-{instance_suffix}", episode, revision, at, observation, fingerprint))
        case["scope"]["cluster"] = self.cluster
        return self.repo.create_case(case)["case"]

    def add_search_case(
        self,
        episode: str,
        at: datetime,
        summary: str,
        observations: list[dict],
        *,
        fingerprint: str | None = None,
        instance_suffix: str = "a",
        hypotheses: list[dict] | None = None,
    ) -> dict:
        case = normalize_case({
            "instance_id": f"{self.prefix}-{instance_suffix}",
            "episode_id": episode,
            "revision": 1,
            "observed_at": at.isoformat(),
            "scope": {"environment": "test", "cluster": self.cluster},
            "summary": summary,
            "observations": observations,
            "hypotheses": hypotheses or [],
            "fingerprint": fingerprint,
        })
        return self.repo.create_case(case)["case"]

    def test_idempotency_is_immutable_and_revisions_are_preserved(self) -> None:
        now = datetime.now(timezone.utc)
        original = normalize_case(payload(f"{self.prefix}-a", "ep", 1, now, ("metric", "restarts", 2)))
        original["scope"]["cluster"] = self.cluster
        first = self.repo.create_case(original)
        replay = self.repo.create_case(original)
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["case"]["id"], replay["case"]["id"])
        changed = {**original, "summary": "A different payload"}
        with self.assertRaises(IdempotencyConflict):
            self.repo.create_case(changed)
        second = self.add_case("ep", 2, now + timedelta(seconds=1), ("metric", "memory", 5))
        self.assertNotEqual(first["case"]["id"], second["id"])
        self.assertEqual(self.repo.get_case(first["case"]["id"])["revision"], 1)
        self.assertEqual(self.repo.get_case(second["id"])["revision"], 2)

    def test_retention_expires_inactive_episodes_and_prevents_old_retries(self) -> None:
        now = datetime.now(timezone.utc)
        old_first = self.add_case("expired", 1, now, ("metric", "legacy", 1))
        old_latest = self.add_case("expired", 2, now + timedelta(seconds=1), ("metric", "shared", 7))
        retained = self.add_case("retained", 1, now + timedelta(seconds=2), ("metric", "shared", 7))
        with self.repo._connect() as conn:
            conn.execute(
                "UPDATE atlas_cases SET created_at = clock_timestamp() - interval '2 days' "
                "WHERE instance_id = %s AND episode_id = %s",
                (f"{self.prefix}-a", "expired"),
            )

        deleted = self.repo.purge_expired_cases(retention_days=1)

        self.assertEqual(deleted, 2)
        self.assertIsNone(self.repo.get_case(old_first["id"]))
        self.assertIsNone(self.repo.get_case(old_latest["id"]))
        self.assertEqual(self.repo.get_case(retained["id"])["id"], retained["id"])
        result = self.repo.search(instance_id=f"{self.prefix}-a", query="shared", limit=10)
        self.assertEqual([case["episode_id"] for case in result["cases"]], ["retained"])
        self.assertEqual(self.repo.list_patterns(scope={"cluster": self.cluster})["patterns"], [])
        with self.assertRaises(EpisodeUnavailable):
            self.add_case("expired", 1, now, ("metric", "legacy", 1))

        with self.repo._connect() as conn:
            tombstone = conn.execute(
                "SELECT reason, actor_key_id FROM atlas_episode_tombstones "
                "WHERE instance_id = %s AND episode_id = 'expired'",
                (f"{self.prefix}-a",),
            ).fetchone()
            audit = conn.execute(
                "SELECT action, deleted_case_count, retention_days FROM atlas_case_lifecycle_audit "
                "WHERE instance_id = %s AND episode_id = 'expired'",
                (f"{self.prefix}-a",),
            ).fetchone()
        self.assertEqual(tombstone, {"reason": "retention", "actor_key_id": "system-retention"})
        self.assertEqual(audit["action"], "episode_retention_expired")
        self.assertEqual(audit["deleted_case_count"], 2)
        self.assertEqual(audit["retention_days"], 1)

    def test_patterns_count_latest_revision_once_and_expose_cooccurrence_only(self) -> None:
        now = datetime.now(timezone.utc)
        self.add_case("episode-1", 1, now, ("metric", "restarts", 3), instance_suffix="1")
        self.add_case("episode-1", 2, now + timedelta(seconds=1), ("metric", "memory", 7), instance_suffix="1")
        self.add_case("episode-2", 1, now + timedelta(seconds=2), ("metric", "memory", 7), instance_suffix="2")
        self.add_case("episode-3", 1, now + timedelta(seconds=3), ("metric", "memory", 7), instance_suffix="3")

        patterns = self.repo.list_patterns(scope={"cluster": self.cluster}, limit=10)["patterns"]
        pattern = next(item for item in patterns if item["key"] == "memory" and item["value"] == 7)
        self.assertEqual(pattern["case_count"], 3)
        self.assertEqual(pattern["instance_count"], 3)
        self.assertEqual(pattern["similarity"], "same_observation")
        self.assertIn("not evidence of a shared cause", pattern["interpretation"])
        detail = self.repo.get_pattern(pattern["id"])
        self.assertEqual(detail["pattern"]["case_count"], len(detail["cases"]))
        self.assertFalse(detail["has_more"])
        self.assertNotIn("episode-1", {case["episode_id"] for case in detail["cases"] if case["revision"] == 1})

        old_pattern_id = observation_pattern_id({"kind": "metric", "key": "restarts", "value": 3, "unit": "count"})
        self.assertIsNone(self.repo.get_pattern(old_pattern_id))

    def test_stats_are_distinct_and_case_pages_follow_latest_revisions(self) -> None:
        before = self.repo.stats()
        now = datetime.now(timezone.utc)
        self.add_case("episode-1", 1, now, ("metric", f"{self.prefix}-old", 1))
        first = self.add_case("episode-1", 2, now + timedelta(seconds=1), ("metric", f"{self.prefix}-shared", 7))
        self.add_case("episode-2", 1, now + timedelta(seconds=2), ("metric", f"{self.prefix}-shared", 7))
        self.add_case("episode-3", 1, now + timedelta(seconds=3), ("metric", f"{self.prefix}-shared", 7))

        after = self.repo.stats()
        self.assertEqual(after["episodes"] - before["episodes"], 3)
        self.assertEqual(after["revisions"] - before["revisions"], 4)
        self.assertEqual(after["patterns"] - before["patterns"], 1)

        page1 = self.repo.list_cases(scope={"cluster": self.cluster}, limit=1)
        page2 = self.repo.list_cases(scope={"cluster": self.cluster}, limit=1, cursor=page1["next_cursor"])
        page3 = self.repo.list_cases(scope={"cluster": self.cluster}, limit=1, cursor=page2["next_cursor"])
        cases = [page["cases"][0] for page in (page1, page2, page3)]
        self.assertTrue(page1["has_more"])
        self.assertTrue(page2["has_more"])
        self.assertFalse(page3["has_more"])
        self.assertIsNone(page3["next_cursor"])
        self.assertEqual(len({case["id"] for case in cases}), 3)
        episode_one = next(case for case in cases if case["episode_id"] == "episode-1")
        self.assertEqual(episode_one["revision"], 2)
        self.assertEqual(episode_one["id"], first["id"])

    def test_search_cursor_pages_are_stable_and_complete(self) -> None:
        now = datetime.now(timezone.utc)
        self.add_case("search-1", 1, now, ("metric", f"{self.prefix}-one", 1))
        self.add_case("search-2", 1, now + timedelta(seconds=1), ("metric", f"{self.prefix}-two", 2))
        self.add_case("search-3", 1, now + timedelta(seconds=2), ("metric", f"{self.prefix}-three", 3))

        page1 = self.repo.search(scope={"cluster": self.cluster}, limit=1)
        page2 = self.repo.search(scope={"cluster": self.cluster}, limit=1, cursor=page1["next_cursor"])
        page3 = self.repo.search(scope={"cluster": self.cluster}, limit=1, cursor=page2["next_cursor"])

        cases = [page["cases"][0] for page in (page1, page2, page3)]
        self.assertTrue(page1["has_more"])
        self.assertTrue(page2["has_more"])
        self.assertFalse(page3["has_more"])
        self.assertEqual(len({case["id"] for case in cases}), 3)

    def test_exact_fingerprint_is_ranked_before_newer_lexical_candidates(self) -> None:
        now = datetime.now(timezone.utc)
        old = self.add_case("old-exact", 1, now - timedelta(hours=2), ("metric", "restarts", 3), "rare-fingerprint", "old")
        self.add_case("new-a", 1, now - timedelta(minutes=1), ("metric", "restarts", 3), "other-a", "new-a")
        self.add_case("new-b", 1, now, ("metric", "restarts", 3), "other-b", "new-b")

        import estima.repository as repository_module
        original_cap = repository_module.MAX_SEARCH_CANDIDATES
        repository_module.MAX_SEARCH_CANDIDATES = 1
        try:
            result = self.repo.search(
                scope={"cluster": self.cluster}, query="restarts", fingerprint="rare-fingerprint", limit=1
            )
        finally:
            repository_module.MAX_SEARCH_CANDIDATES = original_cap
        self.assertEqual(result["cases"][0]["id"], old["id"])
        self.assertEqual(result["cases"][0]["relation"], "fingerprint_match")

    def test_ranker_flag_preserves_legacy_order_and_enables_typed_relevance(self) -> None:
        now = datetime.now(timezone.utc)
        direct = self.add_search_case(
            "pool-direct", now - timedelta(minutes=5),
            "Checkout transactions stalled while PostgreSQL connection pool waiters increased sharply",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 42, "unit": "requests"}],
        )
        healthy = self.add_search_case(
            "pool-healthy", now,
            "Checkout PostgreSQL connection pool is healthy with no waiters and normal latency",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 0, "unit": "requests"}],
        )

        request = {
            "scope": {"cluster": self.cluster},
            "query": "checkout postgres pool wait",
            "limit": 2,
        }
        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "false"}):
            legacy = self.repo.search(**request)
        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "true"}):
            ranked = self.repo.search(**request)

        self.assertEqual(legacy["cases"][0]["id"], healthy["id"])
        self.assertEqual(ranked["cases"][0]["id"], direct["id"])
        self.assertEqual(ranked["cases"][0]["relation"], "typed_observation_match")

    def test_enabled_ranker_preserves_instance_filter(self) -> None:
        now = datetime.now(timezone.utc)
        visible = self.add_search_case(
            "visible", now - timedelta(minutes=1), "Pool waiters recorded",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 2, "unit": "requests"}],
            instance_suffix="visible",
        )
        self.add_search_case(
            "other-instance", now, "Checkout PostgreSQL pool waiters recorded with more detail",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 20, "unit": "requests"}],
            instance_suffix="other",
        )

        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "true"}):
            result = self.repo.search(
                scope={"cluster": self.cluster},
                instance_id=f"{self.prefix}-visible",
                query="pool wait",
                limit=10,
            )

        self.assertEqual([case["id"] for case in result["cases"]], [visible["id"]])

    def test_enabled_ranker_does_not_compare_waiter_counts_to_durations(self) -> None:
        now = datetime.now(timezone.utc)
        count_case = self.add_search_case(
            "count-valued", now - timedelta(minutes=1), "Pool waiters recorded",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 2, "unit": "requests"}],
        )
        self.add_search_case(
            "duration-valued", now, "Pool waiters recorded",
            [{"kind": "metric", "key": "db_pool_waiters", "value": 250, "unit": "ms"}],
        )

        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "true"}):
            result = self.repo.search(
                scope={"cluster": self.cluster}, query="pool wait", limit=2,
            )

        self.assertEqual(result["cases"][0]["id"], count_case["id"])
        self.assertEqual(result["cases"][0]["relation"], "typed_observation_match")

    def test_enabled_ranker_does_not_invent_results_for_empty_candidate_set(self) -> None:
        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "true"}):
            result = self.repo.search(
                scope={"cluster": self.cluster}, query="missing-marker-xylophone", limit=10,
            )

        self.assertEqual(result["cases"], [])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_hypothesis_only_terms_do_not_enter_search_candidates(self) -> None:
        self.add_search_case(
            "hypothesis-only", datetime.now(timezone.utc), "A routine observation was recorded",
            [{"kind": "metric", "key": "pod_restarts", "value": 1, "unit": "count"}],
            hypotheses=[{"statement": "TLS certificate chain expired"}],
        )

        with patch.dict(os.environ, {"COLLECTIVE_SEARCH_RANKING_ENABLED": "true"}):
            result = self.repo.search(
                scope={"cluster": self.cluster}, query="TLS certificate chain expired", limit=10,
            )

        self.assertEqual(result["cases"], [])


if __name__ == "__main__":
    unittest.main()
