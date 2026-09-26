# Search Relevance Evaluation

Run the deterministic, offline benchmark from the repository root:

```sh
python3 scripts/evaluate_search_relevance.py
python3 -m unittest tests.test_search_relevance_eval
```

The fixture contains positive, lexical negative, healthy-but-overlapping,
contradictory-fingerprint, and superseded-revision examples. Judgments use
graded relevance (`0` unrelated or contradicting, `2` useful partial match,
`3` strong match). The report includes precision/recall at `k`, MRR, nDCG,
empty-query false positives, healthy negatives, contradiction hits, and stale
revision leaks.

The evaluator calls `PostgresEstimaRepository.search` directly. It substitutes
only an in-memory fixture row source for PostgreSQL; the production repository
method still computes the returned relevance score and final ordering. The
fixture row source simulates the SQL candidate filters and latest-revision
selection, so this check does not validate PostgreSQL syntax, schema migrations,
the HTTP/auth layer, cursor pagination, or production relevance. The expected
labels are synthetic regression examples, not operator-reviewed ground truth.

The exact-fingerprint contradiction case is deliberate: a matching retrieval
fingerprint and repeated words do not prove that the underlying observation
supports the current mechanism. The benchmark records that as a false positive
instead of changing the ranker to hide it.

## Baseline Snapshot

The checked-in search implementation scored `0.25` precision@3, `0.50`
recall@3, `0.50` MRR, and `0.499` mean nDCG@3 over these four synthetic
queries. The expected-empty stale-revision query returned no result.

- `pool-fingerprint`: the contradictory same-fingerprint case ranked first,
  the supporting exact case second, and a healthy pool case third. Recall@3 was
  `0.50`.
- `pool-lexical-only`: the top three were the contradictory case, the healthy
  pool case, and the OAuth case with a different cause. Neither relevant pool
  case reached the top three.
- `oauth-cause`: both OAuth/certificate cases ranked first and second.
- `superseded-stale-terms`: the old revision's MySQL/deadlock terms did not
  leak through its newer revision; the query returned no cases.

Across top-three slots, the ranker returned three contradictory cases and three
healthy negatives. These are slot counts across queries, not distinct cases.
This suggests the exact fingerprint can overrule conflicting structured
observations, while unweighted lexical coverage and recency can favor common
healthy terms over a useful older case.

A follow-up candidate, not implemented or verified here, is to keep exact
fingerprints as a retrieval feature while capping their ranking bonus, then
combine field-weighted or inverse-document-frequency lexical matches with
typed observation overlap. A contradiction penalty should require explicit
comparable evidence, not an inferred interpretation of free text. Re-evaluate
that approach with independently reviewed labels before changing production
weights.
