from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.evaluate_search_relevance import DEFAULT_FIXTURE, evaluate


class SearchRelevanceEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
        cls.report = evaluate(cls.fixture)
        cls.by_query = {item["id"]: item for item in cls.report["per_query"]}

    def test_exact_fingerprint_can_rank_a_contradictory_case_above_supporting_evidence(self) -> None:
        result = self.by_query["pool-fingerprint"]
        self.assertEqual(result["episode_ids"][0], "pool-contradictory")
        self.assertEqual(result["top_k_grades"][0], 0)
        self.assertEqual(result["episode_ids"][1], "pool-direct")

    def test_lexical_results_can_put_healthy_or_different_cause_cases_ahead_of_relevant_history(self) -> None:
        result = self.by_query["pool-lexical-only"]
        self.assertNotIn("pool-paraphrase", result["episode_ids"][:3])
        self.assertGreaterEqual(result["healthy_negative_in_top_k"], 1)

    def test_superseded_revision_does_not_match_using_only_its_old_terms(self) -> None:
        result = self.by_query["superseded-stale-terms"]
        self.assertTrue(result["empty_result"])
        self.assertEqual(result["stale_revision_in_results"], 0)

    def test_report_contains_standard_ranking_metrics_and_false_positive_counts(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(self.report["engine"], "PostgresEstimaRepository.search")
        self.assertEqual(metrics["expected_empty_queries"], 1)
        self.assertGreater(metrics["contradictory_results_at_k"], 0)
        self.assertGreater(metrics["healthy_negative_results_at_k"], 0)
        self.assertGreaterEqual(metrics["mrr"], 0.0)
        self.assertLessEqual(metrics["mrr"], 1.0)


if __name__ == "__main__":
    unittest.main()
