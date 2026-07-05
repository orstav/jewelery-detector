# Active Policy Reranker Grid

Read-only grid search over active-policy candidate aggregates after live-only crop deployment.

Tool:

```text
tools/evaluate_active_policy_reranker_grid.py
```

The tool rebuilds runtime-style active-policy candidate lists, then reranks candidates with deterministic formulas over candidate evidence fields only:

- `best_similarity`
- `mean_top3_similarity`
- query/candidate crop coverage
- spike penalty for one-off high-similarity evidence

It does not use filenames, image IDs, product IDs, or truth labels as scoring features. Product IDs are evaluation labels only.

## Latest run

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/evaluate_active_policy_reranker_grid.py \
  --database-url "$DATABASE_URL" --shot-role live \
  --output workbench/active-policy-reranker-grid/live.json --top-k 50

PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/evaluate_active_policy_reranker_grid.py \
  --database-url "$DATABASE_URL" --shot-role studio \
  --output workbench/active-policy-reranker-grid/studio.json --top-k 50

PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/evaluate_active_policy_reranker_grid.py \
  --database-url "$DATABASE_URL" --shot-role any \
  --output workbench/active-policy-reranker-grid/all.json --top-k 50
```

## Results

| Split | Current Top-1 | Current Top-5 | Best formula | Best Top-1 | Best Top-5 | Δ Top-1 | Δ Top-5 | Deployable candidates |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| live | 31.08% | 85.23% | `grid_best0.50_mean0.50_bonus0.012_penalty0.00` | 31.38% | 86.15% | +0.31pp | +0.92pp | 0 |
| studio | 53.39% | 94.27% | `grid_best0.80_mean0.20_bonus0.000_penalty0.00` | 53.70% | 94.42% | +0.30pp | +0.15pp | 0 |
| all | 45.93% | 91.36% | `grid_best0.80_mean0.20_bonus0.000_penalty0.00` | 46.13% | 91.66% | +0.20pp | +0.30pp | 0 |

## Conclusion

No rerank formula cleared the deploy gate. The current runtime aggregate remains close to the best grid candidates. Keep these outputs as evidence that small scoring tweaks are not enough; the next improvement should come from better evidence (real VLM/text profiles, stronger localization/pair judging), not just score-weight tuning.
