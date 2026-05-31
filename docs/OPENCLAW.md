# OpenClaw Usage

OpenClaw should call the beta wrapper script rather than directly stitching
together low-level benchmark commands.

## Agent Runbook

Goal: run a folder-labeled jewelry benchmark and report whether the current
conservative matcher is beta-safe.

Stop condition: produce a concise summary containing asset count, precision,
recall, F1, wrong merges, missed same-product pairs, split folders, and links or
paths to the Markdown report plus HTML review page.

Do not stop after only running the command. Read `benchmark/benchmark_report.json`
and report the metrics listed below.

## Benchmark A Folder-Labeled Dataset

```bash
scripts/openclaw_beta_benchmark.sh \
  --input "dataset untested" \
  --out results/openclaw_beta \
  --exclude-folder 8
```

Input contract:

- The input directory contains one subdirectory per product cluster.
- Every image inside a product directory is treated as the same product.
- Product directory names are labels for evaluation only, not production inputs.

Output contract:

- `normalized/manifest.csv` - normalized visual assets.
- `benchmark/benchmark_report.json` - machine-readable benchmark metrics.
- `benchmark/benchmark_report.md` - human-readable benchmark summary.
- `benchmark/review_sheets/00_truth_mistakes_overview.html` - consolidated
  review page showing correct matches, missed matches, wrong merges, and close
  correct non-matches.
- `benchmark/review_sheets/` - detailed HTML review pages and thumbnails.

## Fields To Parse

Read `benchmark/benchmark_report.json` and extract:

```text
asset_count
cluster_count
singleton_count
threshold
precision
recall
f1
predicted_positive
false_positive
false_negative
merge_error_count
split_error_count
split_errors
```

For `split_errors`, report each `reference_cluster_id` and its
`predicted_cluster_ids`. These are the product folders the system split.

## Required Agent Summary

Use this shape:

```text
Benchmark complete.

Assets: <asset_count>
Threshold: <threshold>
Precision / recall / F1: <precision> / <recall> / <f1>
Wrong merges: <merge_error_count>
Missed same-product pairs: <false_negative>
Split folders: <reference_cluster_ids from split_errors, or none>

Report: <path to benchmark_report.md>
Review HTML: <path to 00_truth_mistakes_overview.html>
Verdict: <beta-safe | needs review | failed>
```

Verdict rules:

- `beta-safe`: `precision >= 0.98` and `merge_error_count == 0`.
- `needs review`: benchmark ran but precision is lower, wrong merges exist, or
  recall is materially below the expected beta baseline.
- `failed`: command failed, no images were found, or required outputs are
  missing.

## Beta Decision Policy

Use the benchmark score bands conservatively:

```text
score >= 0.89  -> safe same-product candidate
0.86-0.89      -> send to review or AI adjudication
< 0.86         -> no automatic match
```

Do not use folder names or benchmark labels in production matching. Production
matching should use pixels plus available weak metadata, then review/adjudicate
uncertain candidates.

## Dependencies

The wrapper expects:

- Python 3
- macOS `sips`
- local Python dependencies from `requirements-local.txt`
- SigLIP model files already available in the local Hugging Face cache when
  `--offline-model-cache` is used

If model files are not cached, run a non-offline model setup once from a trusted
developer machine, then keep OpenClaw runs offline.

## Common Failures

- `sips is required`: run on macOS or install/provide an equivalent image
  metadata path before using this wrapper.
- model cache error with `--offline-model-cache`: run once with
  `--allow-network-model` from a trusted developer machine to populate the local
  cache, then rerun offline.
- `no image files found`: verify that the input directory has product
  subfolders containing `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.tif`, or
  `.tiff` files.
- very low precision or nonzero merge errors: do not auto-accept the run; report
  `needs review` and inspect the HTML review page.

## Production Boundary

This wrapper benchmarks folder-labeled datasets. It is not the production
single-image matching API. A production OpenClaw integration should call a
separate retrieval endpoint or command that accepts one incoming image and
returns candidate product IDs plus scores. Until that exists, use this wrapper
only for dataset evaluation.
