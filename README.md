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

Generate a production-shaped embedding payload for one image:

```bash
python3 tools/jewelry_detector.py profile \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.profile.json \
  --model gpt-4.1-mini \
  --max-image-size 1024
```

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device auto \
  --image-size 224 \
  --profile /tmp/img_123.profile.json
```

`profile` writes OpenCLAW-ready profile JSON that OpenCLAW should store
in its own DB. `embed` writes source hash, embedding model, preprocess
version, crop metadata, risk flags, and one embedding per usable crop. Neither
command reads benchmark manifests or catalog labels.

For dependency-light OpenCLAW plumbing tests, replace the provider flags with
`--provider fake`. The fake provider is deterministic and exercises the same JSON
contract without loading a model.

Run the current evaluation benchmark shape:

```bash
scripts/openclaw_beta_benchmark.sh \
  --input "dataset untested" \
  --out results/openclaw_beta \
  --exclude-folder 8
```

The script writes normalized assets, benchmark JSON/Markdown, and review HTML
under the output directory. It is evaluation-only and should not be used as the
production request path.

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
