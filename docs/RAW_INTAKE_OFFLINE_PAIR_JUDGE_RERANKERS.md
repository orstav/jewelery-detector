# Offline Top-K pair-judge/reranker evaluation

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Implemented and ran a read-only offline harness over already-retrieved Top-K candidates:

```text
stored embeddings -> existing Top-K candidate retrieval -> deterministic pair-judge proxies -> product rerank/auto gate
```

No external API calls were made. The harness does not mutate detector DB rows. It does not use filenames, query truth product IDs, or candidate product IDs as scoring features. Product IDs are used only as evaluation truth/output labels and for same-design sibling diagnostics.

## Tool

```text
tools/evaluate_offline_pair_judge_rerankers.py
```

The tool can either:

- replay an existing candidate JSON cache with `--candidate-cache`, or
- build a read-only offline candidate cache from DB retrieval with `--database-url` and optional `--write-candidate-cache`.

Deterministic proxies tested:

1. current aggregate score + score-floor pair judge
2. best similarity + score-floor pair judge
3. consensus pair judge from best/mean/evidence features
4. ambiguity-aware pair judge that routes dense close-score candidate sets to review

## Run

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/evaluate_offline_pair_judge_rerankers.py \
  --database-url "$DATABASE_URL" \
  --output workbench/raw-intake-embedding-consensus/offline_pair_judge_rerankers.json \
  --write-candidate-cache workbench/raw-intake-embedding-consensus/offline_pair_judge_candidates.json \
  --top-k 80
```

Replay check, with no DB read:

```bash
PYTHONPATH=. python3 tools/evaluate_offline_pair_judge_rerankers.py \
  --candidate-cache workbench/raw-intake-embedding-consensus/offline_pair_judge_candidates.json \
  --output /tmp/offline_pair_judge_candidates_replay.json \
  --top-k 80
```

## Data summary

```text
products: 154
dev_products: 139
hidden_products: 15
hidden_evaluated: false
embeddings: 2137
images: 1139
probes: 1997
candidate product rows evaluated: 48336
```

View/crop split in the DB candidate source:

```text
full_image: 1139
center_object: 395
detail_object: 395
foreground_object: 208
```

Harness split inference produced 998 live/crop-like probes and 999 studio/full-image probes.

## Results

No candidate is safe to deploy.

Safe gate:

```text
min_eval_probes: 100
auto_precision >= 97%
correct_auto_recall >= 20%
auto_wrong <= 0
auto_wrong_per_split <= 0
same_design_sibling_wrong <= 0
```

| Approach | Top-1 | Top-3 | Top-5 | Auto precision | Correct auto recall | Auto wrong | Sibling wrong | Safe |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 04_ambiguity_aware_pair_judge | 43.16% | 76.66% | 80.92% | 60.89% | 27.99% | 359 | 274 | no |
| 03_consensus_pair_judge | 41.36% | 75.71% | 80.62% | 60.99% | 27.79% | 355 | 258 | no |
| 01_current_score | 44.57% | 79.07% | 83.58% | 56.20% | 25.64% | 399 | 315 | no |
| 02_best_similarity | 44.22% | 78.67% | 82.62% | 55.47% | 25.89% | 415 | 327 | no |

Best proxy split metrics (`04_ambiguity_aware_pair_judge`):

| Split | Probes | Top-1 | Top-5 | Auto precision | Auto wrong | Same-design sibling wrong |
|---|---:|---:|---:|---:|---:|---:|
| live/crop-like | 998 | 43.99% | 74.75% | 55.27% | 225 | 168 |
| studio/full-image | 999 | 42.34% | 87.09% | 67.71% | 134 | 106 |

## Findings

- Cheap deterministic pair-judge proxies do not resolve same-design sibling confusion. The best proxy still produced 274 sibling-near-truth wrong auto selections.
- Live/crop-like probes were worse than studio on Top-5 and auto precision in this DB-backed candidate run, despite slightly higher Top-1.
- The ambiguity-aware proxy reduced auto attempts and slightly improved ranking score tradeoff, but precision remained far below a deployable threshold.
- Existing Top-K retrieval remains useful as a review shortlist, not as an autonomous same-product selector.

## Verification

```text
python3 -m py_compile tools/evaluate_offline_pair_judge_rerankers.py
PYTHONPATH=. uv run --with pytest --with pillow pytest -q tests/test_cluster_benchmark.py -k 'offline_pair_judge'
# 2 passed, 85 deselected
```

Full `tests/test_cluster_benchmark.py` with the ambient `DATABASE_URL` set failed one pre-existing environment-sensitive test (`test_jewelry_detector_db_requires_database_url`) because the test expects `DATABASE_URL` to be unset.
