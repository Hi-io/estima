from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estima.ranking import rank_search_candidates


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "search_relevance_v1.json"
TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _search_document(case: dict[str, Any]) -> str:
    parts = [case["summary"]]
    parts.extend(value for value in case["scope"].values() if value)
    for item in case["observations"]:
        parts.extend((item["kind"], item["key"], str(item["value"]), item["unit"] or ""))
    return " ".join(parts).casefold()


def _baseline_rank(candidates: list[dict[str, Any]], query: str | None, fingerprint: str | None) -> list[dict[str, Any]]:
    terms = list(dict.fromkeys(TOKEN_RE.findall((query or "").casefold())))[:20]
    scored = []
    for case in candidates:
        text = _search_document(case)
        exact_fingerprint = bool(fingerprint and case.get("fingerprint") == fingerprint.casefold())
        matched = sum(1 for term in terms if term in text)
        coverage = matched / len(terms) if terms else 0.0
        phrase_bonus = 0.25 if query and " ".join(query.casefold().split()) in text else 0.0
        score = 1.0 if exact_fingerprint else min(1.0, coverage * 0.75 + phrase_bonus)
        scored.append({**case, "score": round(score, 4)})
    scored.sort(key=lambda item: (item["score"], str(item.get("observed_at") or ""), str(item.get("id", ""))), reverse=True)
    return scored


def _candidate_pool(fixture: dict[str, Any], definition: dict[str, Any]) -> list[dict[str, Any]]:
    cases = []
    for index, item in enumerate(fixture["cases"]):
        case = {**item["payload"], "id": f"fixture-{index:04d}"}
        cases.append(case)

    scope = definition.get("scope") or {}
    cases = [case for case in cases if all(case["scope"].get(key) == value for key, value in scope.items())]
    if definition.get("instance_id"):
        cases = [case for case in cases if case["instance_id"] == definition["instance_id"]]

    after = definition.get("observed_after")
    before = definition.get("observed_before")
    if after:
        cases = [case for case in cases if case["observed_at"] >= after]
    if before:
        cases = [case for case in cases if case["observed_at"] <= before]

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for case in cases:
        key = (case["instance_id"], case["episode_id"])
        previous = latest.get(key)
        if previous is None or (case["revision"], case["observed_at"]) > (previous["revision"], previous["observed_at"]):
            latest[key] = case
    cases = list(latest.values())

    terms = list(dict.fromkeys(TOKEN_RE.findall((definition.get("query") or "").casefold())))[:20]
    fingerprint = (definition.get("fingerprint") or "").casefold()
    if terms or fingerprint:
        cases = [case for case in cases if case.get("fingerprint") == fingerprint or
                 any(term in _search_document(case) for term in terms)]
    cases.sort(key=lambda case: (
        bool(fingerprint and case.get("fingerprint") == fingerprint), case["observed_at"],
    ), reverse=True)
    return cases[:500]


def _dcg(grades: list[int], k: int) -> float:
    return sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades[:k], start=1))


def _result(query: dict[str, Any], ranked: list[dict[str, Any]], k: int) -> dict[str, Any]:
    judgments = query.get("judgments", {})
    grades = [int(judgments.get(case["episode_id"], 0)) for case in ranked]
    relevant_count = sum(int(grade) >= 2 for grade in judgments.values())
    relevant_ranks = [rank for rank, grade in enumerate(grades, start=1) if grade >= 2]
    ideal = sorted((int(value) for value in judgments.values()), reverse=True)
    ideal_dcg = _dcg(ideal, k)
    returned_grades = grades[:k]
    classes = query.get("case_classes", {})
    return {
        "id": query["id"],
        "episode_ids": [case["episode_id"] for case in ranked],
        "top_k_grades": returned_grades,
        "precision_at_k": sum(grade >= 2 for grade in returned_grades) / k,
        "recall_at_k": sum(grade >= 2 for grade in returned_grades) / relevant_count if relevant_count else None,
        "reciprocal_rank": 1 / relevant_ranks[0] if relevant_ranks else 0.0,
        "ndcg_at_k": _dcg(returned_grades, k) / ideal_dcg if ideal_dcg else None,
        "expected_empty": bool(query.get("expected_empty", False)),
        "empty_result": not ranked,
        "nonrelevant_contradictory_at_k": sum(
            classes.get(case["episode_id"]) == "contradictory" and
            int(judgments.get(case["episode_id"], 0)) < 2
            for case in ranked[:k]
        ),
        "nonrelevant_contradictory_at_1": bool(
            ranked and classes.get(ranked[0]["episode_id"]) == "contradictory" and
            int(judgments.get(ranked[0]["episode_id"], 0)) < 2
        ),
        "healthy_negative_at_k": sum(
            classes.get(case["episode_id"]) == "healthy-negative"
            for case in ranked[:k]
        ),
        "stale_revision_leaks": sum(
            classes.get(case["episode_id"]) == "stale-revision"
            for case in ranked
        ),
    }


def _metrics(query_results: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [item for item in query_results if item["recall_at_k"] is not None]
    expected_empty = [item for item in query_results if item["expected_empty"]]
    return {
        "precision_at_k": sum(item["precision_at_k"] for item in query_results) / len(query_results) if query_results else 0.0,
        "recall_at_k": sum(item["recall_at_k"] for item in judged) / len(judged) if judged else 0.0,
        "mrr": sum(item["reciprocal_rank"] for item in judged) / len(judged) if judged else 0.0,
        "mean_ndcg_at_k": sum(item["ndcg_at_k"] for item in judged if item["ndcg_at_k"] is not None) /
        sum(item["ndcg_at_k"] is not None for item in judged) if judged else 0.0,
        "expected_empty_false_positive_rate": sum(not item["empty_result"] for item in expected_empty) / len(expected_empty) if expected_empty else 0.0,
        "nonrelevant_contradictory_results_at_k": sum(item["nonrelevant_contradictory_at_k"] for item in query_results),
        "nonrelevant_contradictory_results_at_1": sum(item["nonrelevant_contradictory_at_1"] for item in query_results),
        "healthy_negative_results_at_k": sum(item["healthy_negative_at_k"] for item in query_results),
        "stale_revision_leaks": sum(item["stale_revision_leaks"] for item in query_results),
    }


def _evaluate_ranker(fixture: dict[str, Any], ranker: str) -> dict[str, Any]:
    k = int(fixture.get("k", 3))
    class_by_episode = {
        item["payload"]["episode_id"]: item.get("class", "unclassified")
        for item in fixture["cases"]
    }
    results = []
    for definition in fixture["queries"]:
        candidates = _candidate_pool(fixture, definition)
        if ranker == "baseline":
            ranked = _baseline_rank(candidates, definition.get("query"), definition.get("fingerprint"))
        else:
            ranked = rank_search_candidates(
                candidates,
                query=definition.get("query"),
                fingerprint=definition.get("fingerprint"),
            )
        ranked = ranked[:max(k, int(definition.get("limit", k)))]
        scored_definition = {**definition, "case_classes": class_by_episode}
        results.append(_result(scored_definition, ranked, k))
    return {"metrics": _metrics(results), "per_query": results}


def evaluate(fixture: dict[str, Any]) -> dict[str, Any]:
    k = int(fixture.get("k", 3))
    return {
        "fixture": fixture["name"],
        "mode": "offline synthetic corpus; identical latest-revision/candidate filters for both rankers",
        "evaluation_scope": "synthetic n=4 queries; ranker-only comparison, not a production retrieval claim",
        "k": k,
        "case_revisions": len(fixture["cases"]),
        "queries": len(fixture["queries"]),
        "baseline": _evaluate_ranker(fixture, "baseline"),
        "candidate": _evaluate_ranker(fixture, "candidate"),
        "limitations": [
            "Relevance labels are synthetic expectations, not operator-reviewed production ground truth.",
            "Candidate retrieval, latest-revision selection, PostgreSQL execution, pagination, and API behavior are not evaluated.",
            "The candidate ranker is deterministic and does not use an LLM or infer causation.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare deterministic Estima search rankers on synthetic offline labels.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    report = evaluate(json.loads(args.fixture.read_text(encoding="utf-8")))
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
