from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


MAX_QUERY_TERMS = 20
MAX_RANKING_CANDIDATES = 500
TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_ALIASES = {
    "postgresql": "postgres",
    "waiter": "wait",
    "waiters": "wait",
    "waiting": "wait",
    "waited": "wait",
    "connections": "connection",
    "requests": "request",
    "events": "event",
    "failures": "failure",
    "errors": "error",
    "expired": "expire",
    "expiry": "expire",
    "certificates": "certificate",
}
_COUNT_UNITS = {
    "count", "counts", "request", "requests", "event", "events",
    "connection", "connections", "waiter", "waiters", "item", "items",
    "record", "records", "attempt", "attempts", "occurrence", "occurrences",
}
_SIGNAL_TERMS = {"wait", "error", "failure", "retry", "deadlock", "timeout", "drop"}
_NEGATIVE_QUERY_TERMS = {"no", "none", "without", "zero", "absent", "clear"}
_SCORE_WEIGHTS = {
    "summary": 1.0,
    "scope": 0.30,
    "kind": 0.55,
    "key": 1.15,
    "unit": 0.45,
    "value": 0.35,
}
_BASE_WEIGHT = 0.78
_KEY_WEIGHT = 0.07
_FINGERPRINT_BONUS = 0.04
_STATE_MATCH_BONUS = 0.18
_STATE_MISMATCH_PENALTY = 0.20


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return [_ALIASES.get(token, token) for token in TOKEN_RE.findall(text)]


def _unit_is_count(unit: Any) -> bool:
    if not isinstance(unit, str):
        return False
    normalized = unicodedata.normalize("NFKC", unit).strip().casefold()
    return normalized in _COUNT_UNITS


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def _number_token(value: Any) -> str | None:
    numeric = _numeric_value(value)
    if numeric is None:
        return None
    if numeric.is_integer():
        return str(int(numeric))
    return format(numeric, ".15g")


def _case_fields(case: Mapping[str, Any]) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
    fields: dict[str, set[str]] = {
        "summary": set(_tokens(case.get("summary"))),
        "scope": set(),
        "kind": set(),
        "key": set(),
        "unit": set(),
        "value": set(),
    }
    scope = case.get("scope")
    if isinstance(scope, Mapping):
        for value in scope.values():
            fields["scope"].update(_tokens(value))

    observations: list[dict[str, Any]] = []
    raw_observations = case.get("observations")
    if isinstance(raw_observations, Sequence) and not isinstance(raw_observations, (str, bytes)):
        for raw in raw_observations:
            if not isinstance(raw, Mapping):
                continue
            kind_tokens = set(_tokens(raw.get("kind")))
            key_tokens = set(_tokens(raw.get("key")))
            unit = raw.get("unit")
            unit_tokens = set(_tokens(unit))
            value = raw.get("value")
            value_token = _number_token(value)
            if value_token is None and isinstance(value, str):
                value_tokens = set(_tokens(value))
            else:
                value_tokens = {value_token} if value_token is not None else set()
            fields["kind"].update(kind_tokens)
            fields["key"].update(key_tokens)
            fields["unit"].update(unit_tokens)
            fields["value"].update(value_tokens)
            observations.append({
                "kind_tokens": kind_tokens,
                "key_tokens": key_tokens,
                "value": value,
                "unit": unit,
            })

    return fields, observations


def _timestamp_key(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _typed_state_adjustment(
    query_terms: set[str], observations: list[dict[str, Any]],
) -> float:
    requested_signals = query_terms & _SIGNAL_TERMS
    if not requested_signals:
        return 0.0

    # A count only has polarity here when its observation key names the queried
    # signal, its kind is metric, and its unit is an explicitly count-like unit.
    expected_present = not bool(query_terms & _NEGATIVE_QUERY_TERMS)
    adjustments: set[float] = set()
    for observation in observations:
        if "metric" not in observation["kind_tokens"]:
            continue
        if not requested_signals.intersection(observation["key_tokens"]):
            continue
        if not _unit_is_count(observation["unit"]):
            continue
        value = _numeric_value(observation["value"])
        if value is None:
            continue
        observed_present = value > 0
        adjustments.add(
            _STATE_MATCH_BONUS if observed_present == expected_present
            else -_STATE_MISMATCH_PENALTY
        )
    if len(adjustments) != 1:
        return 0.0
    return next(iter(adjustments))


def _contains_phrase(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(list(haystack[index:index + width]) == list(needle)
               for index in range(len(haystack) - width + 1))


def rank_search_candidates(
    cases: Sequence[Mapping[str, Any]],
    *,
    query: str | None = None,
    fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    """Rank already-filtered search candidates without mutating their records.

    Scoring uses summaries, scope, and typed observation fields; hypotheses are
    intentionally excluded. The caller supplies at most 500 already-filtered
    candidates; this function does not retrieve or discard them.
    """
    if len(cases) > MAX_RANKING_CANDIDATES:
        raise ValueError(f"at most {MAX_RANKING_CANDIDATES} candidates can be ranked")
    query_sequence = _tokens(query)[:MAX_QUERY_TERMS]
    query_terms = list(dict.fromkeys(query_sequence))
    query_set = set(query_terms)
    normalized_fingerprint = fingerprint.strip().casefold() if fingerprint else None

    prepared: list[tuple[Mapping[str, Any], dict[str, set[str]], list[dict[str, Any]]]] = []
    document_frequency = {term: 0 for term in query_terms}
    for case in cases:
        fields, observations = _case_fields(case)
        prepared.append((case, fields, observations))
        document_terms = set().union(*fields.values())
        for term in query_terms:
            if term in document_terms:
                document_frequency[term] += 1

    candidate_count = max(len(prepared), 1)
    idf = {
        term: math.log(1.0 + (candidate_count - document_frequency[term] + 0.5) /
                       (document_frequency[term] + 0.5))
        for term in query_terms
    }
    denominator = sum(idf.values())
    query_phrase = query_sequence
    ranked: list[dict[str, Any]] = []

    for case, fields, observations in prepared:
        weighted_coverage = 0.0
        key_coverage = 0.0
        if denominator:
            for term in query_terms:
                term_weight = max(
                    (_SCORE_WEIGHTS[field] for field, values in fields.items() if term in values),
                    default=0.0,
                )
                weighted_coverage += idf[term] * min(term_weight, 1.0)
                if term in fields["key"]:
                    key_coverage += idf[term]
            weighted_coverage /= denominator
            key_coverage /= denominator

        summary_sequence = _tokens(case.get("summary"))
        phrase_bonus = 0.03 if len(query_phrase) > 1 and _contains_phrase(summary_sequence, query_phrase) else 0.0
        exact_fingerprint = bool(
            normalized_fingerprint and
            isinstance(case.get("fingerprint"), str) and
            case["fingerprint"].casefold() == normalized_fingerprint
        )
        state_adjustment = _typed_state_adjustment(query_set, observations)
        score = (
            _BASE_WEIGHT * weighted_coverage
            + _KEY_WEIGHT * key_coverage
            + phrase_bonus
            + (_FINGERPRINT_BONUS if exact_fingerprint else 0.0)
            + state_adjustment
        )
        score = round(min(1.0, max(0.0, score)), 4)
        relation = (
            "fingerprint_match" if exact_fingerprint
            else "typed_observation_match" if state_adjustment > 0
            else "lexical_similarity" if weighted_coverage > 0
            else "recent_in_scope"
        )
        ranked.append({**case, "score": score, "relation": relation})

    ranked.sort(
        key=lambda item: (item["score"], _timestamp_key(item.get("observed_at")), str(item.get("id", ""))),
        reverse=True,
    )
    return ranked
