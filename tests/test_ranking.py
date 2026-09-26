from __future__ import annotations

import json
import unittest

from estima.ranking import rank_search_candidates
from scripts.benchmark_search_ranking import DEFAULT_FIXTURE, evaluate


class SearchRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
        cls.report = evaluate(cls.fixture)
        cls.baseline = {item["id"]: item for item in cls.report["baseline"]["per_query"]}
        cls.candidate = {item["id"]: item for item in cls.report["candidate"]["per_query"]}

    def test_candidate_ranker_improves_positive_relevance_over_baseline(self) -> None:
        baseline = self.report["baseline"]["metrics"]
        candidate = self.report["candidate"]["metrics"]
        self.assertGreater(candidate["recall_at_k"], baseline["recall_at_k"])
        self.assertGreater(candidate["mrr"], baseline["mrr"])
        self.assertGreater(candidate["mean_ndcg_at_k"], baseline["mean_ndcg_at_k"])
        self.assertLess(
            candidate["nonrelevant_contradictory_results_at_1"],
            baseline["nonrelevant_contradictory_results_at_1"],
        )
        self.assertEqual(candidate["healthy_negative_results_at_k"], 0)
        self.assertEqual(candidate["stale_revision_leaks"], 0)

    def test_benchmark_reproduces_existing_baseline_snapshot(self) -> None:
        metrics = self.report["baseline"]["metrics"]
        self.assertEqual(metrics["precision_at_k"], 0.25)
        self.assertEqual(metrics["recall_at_k"], 0.5)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["stale_revision_leaks"], 0)

    def test_same_fingerprint_does_not_put_nonrelevant_contradiction_first(self) -> None:
        result = self.candidate["pool-fingerprint"]
        self.assertEqual(result["episode_ids"][:2], ["pool-direct", "pool-paraphrase"])
        self.assertEqual(result["top_k_grades"][:2], [3, 2])

    def test_lexical_query_retrieves_paraphrase_ahead_of_negative_pool_mentions(self) -> None:
        result = self.candidate["pool-lexical-only"]
        self.assertEqual(result["episode_ids"][:2], ["pool-direct", "pool-paraphrase"])
        self.assertEqual(result["top_k_grades"][:2], [3, 2])

    def test_oauth_cases_remain_relevant_without_hypothesis_text(self) -> None:
        result = self.candidate["oauth-cause"]
        self.assertEqual(set(result["episode_ids"][:2]), {"oauth-expired", "pool-contradictory"})
        self.assertEqual(result["top_k_grades"][:2], [3, 3])

    def test_latest_revision_filter_still_prevents_stale_term_leaks(self) -> None:
        result = self.candidate["superseded-stale-terms"]
        self.assertTrue(result["empty_result"])
        self.assertEqual(result["stale_revision_leaks"], 0)
        self.assertEqual(self.report["candidate"]["metrics"]["stale_revision_leaks"], 0)

    def test_hypotheses_do_not_contribute_to_lexical_score(self) -> None:
        cases = [
            {
                "episode_id": "hypothesis-only",
                "summary": "Database issue under investigation",
                "scope": {},
                "observations": [],
                "hypotheses": [{"statement": "OAuth certificate expired"}],
            },
            {
                "episode_id": "summary-only",
                "summary": "OAuth certificate expired",
                "scope": {},
                "observations": [],
                "hypotheses": [],
            },
        ]
        ranked = rank_search_candidates(cases, query="OAuth certificate expired")
        by_episode = {case["episode_id"]: case for case in ranked}
        self.assertGreater(by_episode["summary-only"]["score"], by_episode["hypothesis-only"]["score"])

    def test_count_state_requires_numeric_metric_and_compatible_unit(self) -> None:
        cases = [
            {
                "episode_id": "count-positive",
                "summary": "Pool waiters observed",
                "scope": {},
                "observations": [{"kind": "metric", "key": "db_pool_waiters", "value": 2, "unit": "requests"}],
            },
            {
                "episode_id": "duration-only",
                "summary": "Pool waiters observed",
                "scope": {},
                "observations": [{"kind": "metric", "key": "db_pool_waiters", "value": 2, "unit": "ms"}],
            },
            {
                "episode_id": "text-number",
                "summary": "Pool waiters observed",
                "scope": {},
                "observations": [{"kind": "metric", "key": "db_pool_waiters", "value": "2", "unit": "requests"}],
            },
        ]
        ranked = rank_search_candidates(cases, query="pool wait")
        self.assertEqual(ranked[0]["episode_id"], "count-positive")
        self.assertEqual(ranked[0]["relation"], "typed_observation_match")

    def test_explicit_absence_query_prefers_zero_count_observation(self) -> None:
        cases = [
            {
                "episode_id": "waiters-present",
                "summary": "Pool waiters observed",
                "scope": {},
                "observations": [{"kind": "metric", "key": "db_pool_waiters", "value": 2, "unit": "requests"}],
            },
            {
                "episode_id": "waiters-absent",
                "summary": "No pool waiters",
                "scope": {},
                "observations": [{"kind": "metric", "key": "db_pool_waiters", "value": 0, "unit": "requests"}],
            },
        ]
        ranked = rank_search_candidates(cases, query="no pool wait")
        self.assertEqual(ranked[0]["episode_id"], "waiters-absent")

    def test_candidate_pool_has_a_fixed_upper_bound(self) -> None:
        cases = [{"episode_id": str(index)} for index in range(501)]
        with self.assertRaises(ValueError):
            rank_search_candidates(cases, query="pool wait")


if __name__ == "__main__":
    unittest.main()
