# DB embedding hard-negative mining

Date: 2026-07-04
Branch: `raw-intake-embedding-consensus`

## Scope

This is a read-only diagnostic over the existing detector engine:

```text
stored crop/image embeddings -> pgvector retrieval -> product candidate aggregation
```

It does not use filename tokens, catalog filenames, or probe catalog ID/prefix as matching features. Catalog product IDs are used only as truth labels and split keys.

## Tool

Added:

```text
tools/mine_db_embedding_hard_negatives.py
```

Example command shape:

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/mine_db_embedding_hard_negatives.py \
  --database-url "$DATABASE_URL" \
  --output workbench/raw-intake-embedding-consensus/db_embedding_hard_negatives.json \
  --csv-output workbench/raw-intake-embedding-consensus/db_embedding_hard_negatives.csv \
  --top-k 80 \
  --limit 300
```

## Split

```text
total_products: 154
dev_products: 139
hidden_products: 15
hidden_evaluated: false
```

Hidden set hash:

```text
3d2651f436f999121ec9496fb557cdaacfbb9ee303f03579fe264ad332f61f06
```

## Latest result

Evaluated probes: 995
Hard negatives found: 549
Truth missing from Top-K: 16

### Negative types

| Type | Count |
|---|---:|
| same_prefix_family_risk | 278 |
| rerankable_top5_error | 113 |
| close_margin_sibling_risk | 94 |
| broad_retrieval_error | 53 |
| truth_missing_from_top_k | 11 |

### Wrong Top-1 score bands

| Band | Count |
|---|---:|
| >=0.95 | 450 |
| 0.90-0.95 | 60 |
| 0.85-0.90 | 30 |
| <0.85 | 9 |

## Most frequent hard-negative pairs

| Truth | Wrong Top-1 | Count |
|---|---|---:|
| R174 | R018 | 19 |
| R006 | R007 | 18 |
| R007 | R006 | 18 |
| R018 | R174 | 16 |
| R019 | R018 | 16 |
| E121 | E120 | 15 |
| E120 | E121 | 14 |
| R018 | R019 | 14 |
| R019 | R174 | 14 |
| R047 | R049 | 14 |

## Interpretation

The dominant issue is not a lack of broad retrieval. Most wrong Top-1 decisions are very high-scoring (`>=0.95`) sibling/family collisions.

This means the next production-safe improvement should not be another threshold-lowering change. It should be a safety/reranking policy for dense sibling neighborhoods:

1. If Top-1 and the truth-neighborhood alternatives are close, route to review instead of auto-match.
2. Require stronger margins when candidate family density is high.
3. Add focused regression cases for the frequent pairs above.
4. Use this hard-negative set as a fixed dev target before touching hidden holdout.
