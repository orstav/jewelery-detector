# Jewelry Detector

Beta tooling for jewelry product matching experiments.

The current beta posture is conservative: high-confidence embedding matches can
be auto-accepted, while lower-confidence candidates should go to review or AI
adjudication. Local datasets, benchmark outputs, model caches, and generated
review pages are intentionally kept out of git.

## Repository Layout

- `tools/` - benchmark and matching utilities.
- `scripts/` - operator-facing wrappers for common runs.
- `tests/` - regression tests for clustering and adjudication behavior.
- `devtools/` - local quality gates.
- `docs/` - usage notes and beta baseline records.
- `data/`, `results/`, `dataset*/` - local inputs and generated outputs; ignored
  by git.

## Current Beta Baseline

Latest representative run excludes folder `8` from `dataset untested` because it
was not representative of the product clusters.

```text
provider: siglip-google_siglip-base-patch16-224-cpu-s224
assets: 45
threshold: 0.89
precision: 1.000
recall: 0.750
F1: 0.857
wrong merges: 0
missed same-product pairs: 5
split folders: 12, 16, 24
```

See `docs/BETA_BASELINE.md` for the interpretation and output locations.

## Quick Start

Install local dependencies in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

Run the current beta benchmark shape:

```bash
scripts/openclaw_beta_benchmark.sh \
  --input "dataset untested" \
  --out results/openclaw_beta \
  --exclude-folder 8
```

The script writes normalized assets, benchmark JSON/Markdown, and review HTML
under the output directory.

OpenClaw agents should start with [docs/OPENCLAW.md](docs/OPENCLAW.md). It
defines the run command, output fields to parse, report format, failure modes,
and stop conditions.

## Git Hygiene

Keep only source, docs, tests, and lightweight config in git. Do not commit:

- raw jewelry datasets
- generated `results/`
- model caches
- local secrets or key files
- zip archives
