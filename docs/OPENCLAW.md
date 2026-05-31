# OpenClaw Usage

OpenClaw should call the beta wrapper script rather than directly stitching
together low-level benchmark commands.

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
- `benchmark/review_sheets/` - HTML review pages and thumbnails.

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
