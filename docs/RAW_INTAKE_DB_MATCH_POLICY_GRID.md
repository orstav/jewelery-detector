# DB match-policy grid evaluation

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

This is a read-only policy-grid evaluation over the existing detector engine:

```text
stored image/crop embeddings -> pgvector retrieval -> product candidate aggregation -> offline decision policies
```

It does not use filename tokens, catalog filenames, or probe catalog ID/prefix as matching features. Product IDs are used only as benchmark truth/split labels.

## Tool

Added:

```text
tools/evaluate_db_match_policy_grid.py
```

The tool builds product candidates once, then applies many decision policies offline. Hidden product holdout remains untouched.

Example command shape:

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/evaluate_db_match_policy_grid.py \
  --database-url "$DATABASE_URL" \
  --output workbench/raw-intake-embedding-consensus/db_match_policy_grid.json \
  --top-k 80
```

## Important implementation correction

The dense-family guard is now opt-in via policy:

```json
{"dense_family_guard_enabled": true}
```

Without that flag, existing policy behavior is preserved. This prevents the earlier conservative prototype from silently reducing auto-match recall in production-like policy runs.

## Split

```text
total_products: 154
dev_products: 139
hidden_products: 15
hidden_evaluated: false
probes: 995
```

Hidden set hash:

```text
3d2651f436f999121ec9496fb557cdaacfbb9ee303f03579fe264ad332f61f06
```

## Baseline, no dense-family guard

| Metric | Count / Rate |
|---|---:|
| Auto total | 430 |
| Auto correct | 292 |
| Auto wrong | 138 |
| Review | 550 |
| No match | 15 |
| Auto precision | 67.91% |
| Correct auto recall | 29.35% |

## Best tradeoff from current grid

Policy:

```text
guard_margin=0.07_delta=0.08_window=2
```

| Metric | Baseline | Candidate | Delta |
|---|---:|---:|---:|
| Auto correct | 292 | 246 | -46 |
| Auto wrong | 138 | 56 | -82 |
| Review | 550 | 678 | +128 |
| Auto precision | 67.91% | 81.46% | +13.55 pp |
| Correct auto recall | 29.35% | 24.72% | -4.62 pp |
| Wrong auto rate | 13.87% | 5.63% | -8.24 pp |

## Other useful candidates

| Policy | Auto correct | Auto wrong | Review | Precision | Correct auto recall |
|---|---:|---:|---:|---:|---:|
| margin=0.08 delta=0.08 window=2 | 237 | 51 | 692 | 82.29% | 23.82% |
| margin=0.05 delta=0.08 window=10 | 266 | 73 | 641 | 78.47% | 26.73% |
| margin=0.06 delta=0.08 window=2 | 255 | 65 | 660 | 79.69% | 25.63% |

## Interpretation

The earlier all-prefix dense-family guard was too conservative. This grid confirms a better shape:

- restrict the dense-family guard by numeric neighborhood (`window=2` is currently strongest tradeoff);
- keep score delta broad enough (`0.08`) to catch sibling risks;
- use the guard only when explicitly enabled by policy.

This still sacrifices some recall, but much less than the first prototype while removing most dangerous wrong auto-matches.

Recommended dev candidate for the next review cycle:

```json
{
  "dense_family_guard_enabled": true,
  "same_design_review_margin": 0.07,
  "dense_family_score_delta": 0.08,
  "same_family_numeric_window": 2
}
```

Do not evaluate hidden holdout until the dev policy/reranking approach is accepted.

## Verification

Focused detector DB tests:

```text
10 passed, 66 deselected
```
