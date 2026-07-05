# DB crop/hash retrieval experiment

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Read-only experiment over detector DB catalog/reference images. This checks whether cheap local image evidence — center crops, heuristic foreground crops, average hash, and difference hash — gives enough exact-product identity signal to justify using perceptual hashes or simple crop-localization as the next reranker layer.

This is **not** production logic and does not mutate detector DB, Shopify, Airtable, Drive, or Stav state.

## Leakage controls

The evaluator does not use:

- filenames as features;
- probe catalog IDs as features;
- product prefixes as features;
- product IDs as matching inputs.

Product IDs are used only for product-level dev/hidden split and correctness scoring.

Hidden holdout remains untouched.

## Command

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' --with pillow --with pillow-heif \
  python tools/evaluate_db_crop_hash_retrieval.py \
  --database-url '[REDACTED]' \
  --output workbench/raw-intake-embedding-consensus/db_crop_hash_retrieval.json \
  --top-k 20
```

## Data

```text
Images read: 1,139
Images OK: 1,139
Total products: 154
Dev products: 139
Hidden products: 15
Hidden evaluated: No
```

The tool builds these production-realistic views from source pixels:

- `full`
- `center70`
- `center50`
- `foreground_padded` from a simple background-difference heuristic, falling back to `center70`

It compares `ahash` and `dhash` variants across these views.

## Results

| Approach | Top-1 | Top-3 | Top-5 | Missing correct candidate |
|---|---:|---:|---:|---:|
| `09_best_view_dhash` | 43.02% | 68.54% | 71.06% | 215 |
| `05_center50_ahash` | 41.71% | 67.64% | 69.45% | 244 |
| `03_center70_ahash` | 40.20% | 65.83% | 69.25% | 225 |
| `04_center70_dhash` | 39.30% | 69.05% | 71.86% | 223 |
| `06_center50_dhash` | 38.69% | 69.45% | 70.25% | 239 |
| `02_full_dhash` | 38.39% | 67.54% | 70.95% | 218 |
| `08_foreground_dhash` | 37.49% | 67.04% | 70.25% | 215 |
| `01_full_ahash` | 36.98% | 59.70% | 64.02% | 244 |
| `07_foreground_ahash` | 36.78% | 63.52% | 66.03% | 246 |
| `10_weighted_multi_hash` | 35.68% | 69.75% | 71.66% | 206 |

## Interpretation

The best cheap crop/hash variant reaches Top-1 `43.02%`, which is roughly comparable to but slightly below the current SigLIP/pgvector product-consensus Top-1 `44.82%`. Its Top-5 is much worse (`71.06%` vs current `89.15%`).

Conclusion:

```text
Cheap perceptual hashes and naive center/foreground crops are not enough.
```

This is a useful negative result. The next real improvement should not be phash/ahash/dhash-only matching. It should be **actual crop embeddings**:

```text
source image pixels
→ jewelry-localized crop generation
→ SigLIP embedding per crop/view
→ product-level multi-view aggregation
→ reranker/policy evaluation
```

## Notes

This run produced Pillow decompression warnings for a few very large catalog images, but completed successfully. The evaluator downsizes loaded images before hashing, and writes only local workbench artifacts.
