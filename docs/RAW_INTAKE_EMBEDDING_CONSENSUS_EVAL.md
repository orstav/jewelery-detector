# Raw-intake embedding consensus evaluation

Date: 2026-07-04
Branch: `raw-intake-embedding-consensus`

## Scope

This benchmark evaluates the existing jewelry detector engine:

```text
image/crop pixels -> SigLIP embedding -> pgvector retrieval -> product candidate aggregation -> margin/threshold decision
```

It does **not** use filename tokens, catalog filenames, or the probe catalog ID/prefix as matching features. Catalog `product_id` is used only as benchmark truth and for product-level train/dev/hidden splitting.

## Detector DB discovery

OpenClaw documentation identifies the detector Postgres service in:

```text
workspace/stav-p0-step3-identity-validation/docs/active/STATE_MANIFEST.md
```

Relevant non-secret connection facts:

```text
container: jewelery-detector-postgres
host: 127.0.0.1:55433
database: detector
user: detector
```

The live URL should be supplied via `DATABASE_URL` or a local secret source. Do not print full DB URLs in reports.

Port check during this work:

```text
127.0.0.1:55433 open
```

Current catalog index observed in DB:

```text
1139 active embeddings
154 product IDs
1139 image IDs
```

## Evaluation tool

Added:

```text
tools/evaluate_db_embedding_retrieval.py
```

The tool is read-only. It queries stored embeddings and compares:

1. `old_single_best_crop` — previous product aggregation by single best crop.
2. `new_product_consensus` — current aggregation using blended best similarity and top-3 same-product evidence.

Example command shape, with secrets supplied by environment:

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/evaluate_db_embedding_retrieval.py \
  --database-url "$DATABASE_URL" \
  --output workbench/raw-intake-embedding-consensus/db_embedding_retrieval_dev.json \
  --top-k 80
```

## Dev split

The default split holds out 10% of products and does not evaluate hidden products by default.

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

## Latest dev results

Evaluated probes: 995 catalog embeddings from dev products where the truth product has at least one other reference image.

| Approach | Top-1 | Top-3 | Top-5 | Missing correct candidate |
|---|---:|---:|---:|---:|
| old_single_best_crop | 43.82% | 84.22% | 88.14% | 16 |
| new_product_consensus | 44.82% | 85.53% | 89.15% | 16 |

Delta:

```text
Top-1: +1.01 percentage points
Top-3: +1.31 percentage points
Top-5: +1.01 percentage points
```

## Interpretation

The consensus change is a real but small improvement. It proves the existing engine can be improved through better product-level aggregation without filename leakage, but it is not close to the final target yet.

Next likely improvements:

1. Same-design/low-margin review guard.
2. Hard-negative mining for sibling product families.
3. Jewelry localization/crop quality improvements before embedding, especially for model/lifestyle images.
4. Policy calibration after candidate generation improves.

## Verification

Targeted DB aggregation tests:

```text
8 passed, 66 deselected
```

Full detector benchmark tests before the latest consensus weighting change:

```text
74 passed
```
