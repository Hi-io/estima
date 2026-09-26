from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estima.normalize import normalize_case, search_document
from estima.repository import MAX_SEARCH_CANDIDATES, TOKEN_RE, PostgresEstimaRepository

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "search_relevance_v1.json"


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _FixtureCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FixtureConnection:
    def __init__(self, repository: "FixtureSearchRepository") -> None:
        self.repository = repository

    def __enter__(self) -> "_FixtureConnection":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _sql: str, _params: list[Any]) -> _FixtureCursor:
        return _FixtureCursor(self.repository.sql_candidates())


class FixtureSearchRepository(PostgresEstimaRepository):
    """Run the production search scorer/order against a deterministic fixture row source."""

    def __init__(self, fixture_cases: list[dict[str, Any]]) -> None:
        super().__init__("postgresql://fixture/offline")
        self.rows: list[dict[str, Any]] = []
        self.request: dict[str, Any] = {}
        for index, item in enumerate(fixture_cases):
            case = normalize_case(item["payload"])
            self.rows.append({
                **case,
                "id": f"fixture-{index:04d}",
                "search_document": search_document(case),
            })

    def _connect(self) -> _FixtureConnection:
        return _FixtureConnection(self)

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.request = kwargs
        return super().search(**kwargs)

    def sql_candidates(self) -> list[dict[str, Any]]:
        request = self.request
        scope = request.get("scope") or {}
        rows = [row for row in self.rows if all(row["scope"].get(key) == value for key, value in scope.items())]
        if request.get("instance_id"):
            rows = [row for row in rows if row["instance_id"] == request["instance_id"]]

        after = _time(request.get("observed_after"))
        before = _time(request.get("before"))
        if after:
            rows = [row for row in rows if _time(row["observed_at"]) >= after]
        if before:
            rows = [row for row in rows if _time(row["observed_at"]) <= before]

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["instance_id"], row["episode_id"])
            previous = latest.get(key)
            rank = (row["revision"], _time(row["observed_at"]))
            if previous is None or rank > (previous["revision"], _time(previous["observed_at"])):
                latest[key] = row
        rows = list(latest.values())

        terms = list(dict.fromkeys(TOKEN_RE.findall((request.get("query") or "").casefold())))[:20]
        fingerprint = (request.get("fingerprint") or "").casefold()
        if terms or fingerprint:
            rows = [row for row in rows if row["fingerprint"] == fingerprint or
                    any(term in row["search_document"] for term in terms)]

        rows.sort(key=lambda row: (
            bool(fingerprint and row["fingerprint"] == fingerprint),
            _time(row["observed_at"]),
        ), reverse=True)
        return rows[:MAX_SEARCH_CANDIDATES]


def _dcg(grades: list[int], k: int) -> float:
    return sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades[:k], start=1))


def _query_result(query: dict[str, Any], ranked: list[dict[str, Any]], k: int) -> dict[str, Any]:
    judgments = query.get("judgments", {})
    grades = [int(judgments.get(case["episode_id"], 0)) for case in ranked]
    relevant_count = sum(int(grade) >= 2 for grade in judgments.values())
    relevant_ranks = [rank for rank, grade in enumerate(grades, start=1) if grade >= 2]
    top_grades = grades[:k]
    ideal = sorted((int(value) for value in judgments.values()), reverse=True)
    dcg = _dcg(top_grades, k)
    ideal_dcg = _dcg(ideal, k)
    case_classes = query.get("case_classes", {})
    return {
        "id": query["id"],
        "category": query.get("category", "unspecified"),
        "returned": len(ranked),
        "episode_ids": [case["episode_id"] for case in ranked],
        "top_k_grades": top_grades,
        "precision_at_k": sum(grade >= 2 for grade in top_grades) / k,
        "recall_at_k": (sum(grade >= 2 for grade in top_grades) / relevant_count) if relevant_count else None,
        "reciprocal_rank": (1 / relevant_ranks[0]) if relevant_ranks else 0.0,
        "ndcg_at_k": (dcg / ideal_dcg) if ideal_dcg else None,
        "expected_empty": bool(query.get("expected_empty", False)),
        "empty_result": not ranked,
        "contradictory_in_top_k": sum(case_classes.get(case["episode_id"]) == "contradictory"
                                       for case in ranked[:k]),
        "healthy_negative_in_top_k": sum(case_classes.get(case["episode_id"]) == "healthy-negative"
                                          for case in ranked[:k]),
        "stale_revision_in_results": sum(case_classes.get(case["episode_id"]) == "stale-revision"
                                          for case in ranked),
    }


def evaluate(fixture: dict[str, Any]) -> dict[str, Any]:
    repository = FixtureSearchRepository(fixture["cases"])
    class_by_episode: dict[str, str] = {}
    for item in fixture["cases"]:
        payload = item["payload"]
        class_by_episode[payload["episode_id"]] = item.get("class", "unclassified")

    k = int(fixture.get("k", 3))
    query_results = []
    for definition in fixture["queries"]:
        result = repository.search(
            scope=definition.get("scope"),
            query=definition.get("query"),
            fingerprint=definition.get("fingerprint"),
            instance_id=definition.get("instance_id"),
            observed_after=_time(definition.get("observed_after")),
            before=_time(definition.get("observed_before")),
            limit=max(k, int(definition.get("limit", k))),
        )
        scored_definition = {**definition, "case_classes": class_by_episode}
        query_results.append(_query_result(scored_definition, result["cases"], k))

    judged = [item for item in query_results if item["recall_at_k"] is not None]
    expected_empty = [item for item in query_results if item["expected_empty"]]
    return {
        "fixture": fixture["name"],
        "engine": "PostgresEstimaRepository.search",
        "mode": "offline fixture rows; production repository scoring/order",
        "k": k,
        "case_revisions": len(fixture["cases"]),
        "queries": len(query_results),
        "metrics": {
            "precision_at_k": (sum(item["precision_at_k"] for item in query_results) / len(query_results)) if query_results else 0.0,
            "recall_at_k": (sum(item["recall_at_k"] for item in judged) / len(judged)) if judged else 0.0,
            "mrr": (sum(item["reciprocal_rank"] for item in judged) / len(judged)) if judged else 0.0,
            "mean_ndcg_at_k": (sum(item["ndcg_at_k"] for item in judged if item["ndcg_at_k"] is not None) /
                               sum(item["ndcg_at_k"] is not None for item in judged)) if judged else 0.0,
            "expected_empty_queries": len(expected_empty),
            "expected_empty_false_positive_rate": (sum(not item["empty_result"] for item in expected_empty) / len(expected_empty)) if expected_empty else 0.0,
            "contradictory_results_at_k": sum(item["contradictory_in_top_k"] for item in query_results),
            "healthy_negative_results_at_k": sum(item["healthy_negative_in_top_k"] for item in query_results),
            "stale_revision_leaks": sum(item["stale_revision_in_results"] for item in query_results),
        },
        "per_query": query_results,
        "limitations": [
            "The production repository search method supplies scoring and final ordering.",
            "A fixture row source simulates PostgreSQL scope/time/text filtering and latest-revision selection.",
            "This does not test PostgreSQL SQL execution, migrations, HTTP/auth/cursor behavior, or production relevance.",
            "Synthetic relevance labels are authored expectations, not operator-reviewed production ground truth.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Score Estima search against synthetic offline relevance labels.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = evaluate(fixture)
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
