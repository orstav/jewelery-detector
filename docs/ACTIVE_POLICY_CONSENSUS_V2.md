# Active Policy Consensus v2

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`
Code commit: this commit (`Tune active policy product consensus scoring`)
DB policy deployed: `jewelry-siglip-live-crop-consensus-v2`

## Scope

Production-realistic, read-only DB experiment over active-policy runtime candidates after live-only crop activation.

Algorithm inputs:

- stored image/crop embeddings
- runtime active-policy candidate retrieval
- candidate aggregate evidence fields only: `best_similarity`, `mean_top3_similarity`, query/candidate crop coverage, and spike gap

Evaluation labels:

- catalog `product_id`, used only to score precision/recall on the dev split

No WhatsApp/Shopify/Airtable/Drive writes. No external VLM calls.

## Runtime scoring change

Previous product aggregate score:

```text
0.60 * best_similarity + 0.40 * mean_top3_similarity
```

New conservative consensus score:

```text
0.80 * best_similarity
+ 0.20 * mean_top3_similarity
+ 0.018 * min(query_crop_count, 4) / 4
+ 0.018 * 0.6 * min(candidate_crop_count, 6) / 6
- 0.04 * max(best_similarity - mean_top3_similarity - 0.04, 0)
```

The formula uses no filenames, image IDs, product IDs, or truth labels.

## Offline gate result

Command:

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/evaluate_active_policy_threshold_grid.py \
  --database-url "$DATABASE_URL" \
  --output workbench/active-policy-threshold-grid/consensus_v2_grid.json \
  --shot-role any --top-k 50 \
  --candidate-min-scores 0.94 \
  --review-min-scores 0.94,0.96 \
  --auto-match-scores 0.94,0.96 \
  --margin-thresholds 0.12 \
  --safe-auto-precision 1.0 \
  --safe-auto-recall 0.05 \
  --safe-max-wrong 0 \
  --safe-max-wrong-per-split 0
```

Output summary:

```text
current active safe-v1 thresholds with consensus-v2 scoring:
  auto_total: 82
  auto_correct: 82
  auto_wrong: 0
  auto_precision: 100.00%
  correct_auto_recall: 8.24%

best safe v2 thresholds:
  candidate_min_score: 0.94
  review_min_score: 0.94
  auto_match_score: 0.94
  margin_threshold: 0.12
  auto_total: 88
  auto_correct: 88
  auto_wrong: 0
  auto_precision: 100.00%
  correct_auto_recall: 8.84%
```

Split metrics for the v2 threshold candidate:

```text
live:   11 auto correct, 0 wrong, 3.38% correct-auto recall
studio: 77 auto correct, 0 wrong, 11.61% correct-auto recall
unknown: 0 auto correct, 0 wrong
```

Active deployed safe-v1 baseline before this change was 53 auto-correct / 0 wrong / 5.33% correct-auto recall on the same 995-probe dev split.

## Verification

```text
python3 -m py_compile tools/jewelry_detector_db.py tools/evaluate_active_policy_threshold_grid.py tools/evaluate_active_policy_reranker_grid.py
DATABASE_URL= PYTHONPATH=. uv run --with pytest --with pillow pytest tests/test_cluster_benchmark.py -q
# 90 passed in 0.48s

PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/preflight_live_crop_policy.py \
  --database-url "$DATABASE_URL" --require-live-crop-candidates
# status: pass
# active full rows: 1197
# active crop rows: 998
# active policy before deploy: jewelry-siglip-live-crop-safe-v1
```

## Rollback

Code rollback:

```bash
git revert <this-commit>
```

DB policy rollback:

```sql
BEGIN;
UPDATE matching_policies SET active = false WHERE active = true;
UPDATE matching_policies SET active = true WHERE name = 'jewelry-siglip-live-crop-safe-v1';
COMMIT;
```
