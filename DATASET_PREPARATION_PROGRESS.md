# Dataset Preparation Progress

Updated on 2026-05-28.

## Current Dataset

The current DS-ready catalog dataset is:

```text
data/catalog_normalized_clean_v1/final_labeled_manifest.csv
data/catalog_normalized_clean_v1/final_labeled_manifest.json
```

Use `final_labeled_manifest.*` for downstream work, not the raw
`manifest.csv`. The final manifest applies human and auto attribution labels on
top of the normalized catalog manifest.

## What We Built

- Normalized the catalog into visual assets under
  `data/catalog_normalized_clean_v1`.
- Removed loose category-root files from catalog product assets, such as copied
  source images sitting directly under `טבעות/`.
- Added manual attribution review for all high-risk catalog assets.
- Added a second-pass review for ambiguous role/product cases.
- Materialized the final labeled manifest with:
  - `final_product_ids`
  - `media_role`
  - `identity_eligible`
  - `supports_multiple_products`
  - `catalog_media_eligible`
  - `clustering_policy`
  - attribution source and notes

## Final Counts

From `final_labeled_manifest_summary.json`:

| Metric | Count |
| --- | ---: |
| Materialized assets | 683 |
| Identity eligible assets | 364 |
| Supporting assets | 292 |
| Shared supporting assets | 27 |
| Catalog media eligible assets | 683 |
| Manual labels | 25 |
| Auto labels | 16 |
| Inferred labels | 642 |
| Warnings | 0 |

## Key Corrections

- `R008`: image belongs only to `R008`; `R009` is a similar ring with a larger
  diamond and currently has no photo. Final role: `supporting`, product `R008`.
- `R164/R165`: `R164` was a mistaken/deleted product. The two lifestyle images
  are assigned to `R165` as `supporting`.
- `R012-R014`: stack images include `R013`. Final shared product IDs:
  `R012,R013,R014`.
- `R015-R017`: stack images include `R016`. Final shared product IDs:
  `R015,R016,R017`.
- `CA00546`: shared image for the silver ancient shekel and white-gold
  variation. Final shared product IDs: `E140,E141`.

## Accepted Limitations

This dataset is good enough to try as the working catalog media dataset, but it
is not a complete product-recognition benchmark yet.

- Some raw products are intentionally absent from the final usable dataset:
  `E119`, `R007`, `R019`, `R020`, `R049`, `R050`.
- Some products have only supporting or shared media and no identity anchor.
  Examples include `R008`, `R165`, `E141`, `R015`, and `R017`.
- Most non-high-risk labels are inferred from folder/name/role heuristics.
- The dataset should be used as a bootstrap dataset and improved as more clean
  product identity images are added.

## Commands

Rebuild clean catalog normalization:

```bash
python3 tools/jewelry_cluster_benchmark.py catalog-normalize \
  --root data/catalog_raw \
  --out data/catalog_normalized_clean_v1 \
  --exclude-ambiguous-assets
```

Rebuild attribution review data:

```bash
python3 tools/build_attribution_review.py
```

Materialize the final labeled dataset:

```bash
python3 tools/materialize_catalog_labels.py
```

Run tests:

```bash
python3 -m unittest tests/test_cluster_benchmark.py
```

## Next Step

Use `data/catalog_normalized_clean_v1/final_labeled_manifest.csv` as the input
for the first dataset-backed product matching / clustering experiment. Treat
`identity_eligible=true` assets as identity evidence, and attach
`supporting` / `shared_supporting` assets only after identity grouping.
