# OpenClaw Usage

OpenCLAW production should call the single-image `profile` and `embed` commands,
then use the DB-aware `init-db`, `store-profile`, and `store-embedding` commands
to persist detector outputs. OpenCLAW still owns vector search, policy
thresholds, match attempts, and review state. The benchmark wrapper remains
available for evaluation-only runs.

## OpenCLAW Setup Guide

Prerequisites:

```text
Python 3.9+
local Python packages from requirements-local.txt
OPENAI_API_KEY for profile calls
SigLIP model files in the Hugging Face cache for offline production embedding
Postgres with pgvector
DATABASE_URL for DB-aware commands
```

The production `tools/jewelry_detector.py` path uses Pillow for image
preprocessing and is not macOS- or `sips`-dependent. Some legacy benchmark
commands in `tools/jewelry_cluster_benchmark.py` still use `sips`.

Install the detector environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

Set the profile model credential where OpenCLAW launches the detector:

```bash
export OPENAI_API_KEY=...
```

Run a dependency-light smoke test first. This does not call OpenAI or load
SigLIP; it only proves the wrapper, image reading, JSON output, and OpenCLAW
parsing path:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id smoke_test_1 \
  --out /tmp/smoke_test_1.embedding.json \
  --provider fake
```

Warm the SigLIP model cache once on a trusted machine with network access:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id model_cache_warmup \
  --out /tmp/model_cache_warmup.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224
```

After that, production can run offline:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache
```

OpenCLAW setup responsibilities:

```text
1. Run jewelry_detector.py init-db to create product_images, image_profiles,
   image_embeddings, matching_policies, match_attempts, and match_candidates.
2. Run index-dir for catalog product folders.
3. Run match-image or match-dir for incoming images.
4. Use match_attempts and match_candidates for review/audit UI.
```

Initialize the DB:

```bash
export DATABASE_URL=postgresql://...

python3 tools/jewelry_detector.py init-db
```

Index catalog product folders:

```bash
python3 tools/jewelry_detector.py index-dir \
  --root /catalog \
  --work-dir /tmp/openclaw-detector/catalog \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache
```

`index-dir` expects one product-id folder per product:

```text
/catalog/
  R001/
    front.jpg
    model.jpg
  E014/
    front.jpg
```

Match one incoming image:

```bash
python3 tools/jewelry_detector.py match-image \
  --image /incoming/new.jpg \
  --image-id incoming_001 \
  --work-dir /tmp/openclaw-detector/incoming \
  --out /tmp/openclaw-detector/incoming/incoming_001.match.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache
```

Match a folder of incoming images:

```bash
python3 tools/jewelry_detector.py match-dir \
  --input /incoming \
  --work-dir /tmp/openclaw-detector/incoming \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache
```

## Production Profile Contract

Use `profile` when OpenCLAW does not already have a compatible cached
profile row for the image hash, model, prompt version, and max image size:

```bash
python3 tools/jewelry_detector.py profile \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.profile.json \
  --model gpt-4.1-mini \
  --max-image-size 1024
```

Output shape:

```json
{
  "schema_version": "1.0",
  "image_id": "img_123",
  "source_sha256": "...",
  "model": "gpt-4.1-mini",
  "prompt_version": "multi_view_profile_v1_2026_05_29",
  "max_image_size": 1024,
  "cache_key": "...",
  "profile": {
    "image_id": "img_123",
    "image_width": 1200,
    "image_height": 900,
    "scene_type": "model_lifestyle",
    "has_hand": true,
    "has_person": false,
    "background_type": "studio",
    "jewelry_items": [
      {
        "type": "ring",
        "dominance": "small",
        "object_completeness": "complete",
        "box": [400, 300, 120, 120],
        "confidence": 0.9,
        "identity_features": ["gold band"]
      }
    ],
    "quality_flags": [],
    "recommended_evidence_policy": "crop_heavy"
  },
  "raw_response": {}
}
```

OpenCLAW should store this payload in its DB and use that DB row as the
production cache of record. The detector repo's older `image-profile` command
still writes `<out>/image_profile_cache.json` for batch benchmark/dev workflows,
but production should not rely on that local file cache.

## Production Embedding Contract

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device auto \
  --image-size 224
```

Output shape:

```json
{
  "schema_version": "1.0",
  "image_id": "img_123",
  "embedding_model": "siglip-google_siglip-base-patch16-224-cpu-s224",
  "preprocess_version": "jewelry-evidence-v1",
  "embedding_dim": 768,
  "source_sha256": "...",
  "crops": [
    {
      "crop_id": "img_123_full_image",
      "view_type": "full_image",
      "box": [0, 0, 1200, 900],
      "source": "full",
      "risk_flags": [],
      "usable_for_retrieval": true,
      "embedding": [0.01, -0.02]
    }
  ],
  "warnings": []
}
```

The detector commands do not connect to OpenCLAW storage and do not require
`manifest.csv`, catalog labels, product IDs, or benchmark folders. With no
profile input, it emits a full-image crop and includes the warning
`no_profile_supplied_full_image_only`. OpenCLAW can pass `--profile` with a
single image profile JSON object, or the full `profile` output payload,
when crop evidence has already been generated or loaded from the DB.

For integration plumbing, use the deterministic fake provider first. It avoids
model downloads and still exercises file handling, hashing, crop metadata,
embedding serialization, error handling, and OpenCLAW JSON parsing:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id smoke_test_1 \
  --out /tmp/smoke_test_1.embedding.json \
  --provider fake
```

Expected smoke-test fields:

```text
schema_version: 1.0
embedding_model: fake-hash-v1
preprocess_version: jewelry-evidence-v1
embedding_dim: 64
crops[0].crop_id: smoke_test_1_full_image
crops[0].usable_for_retrieval: true
```

For production, use SigLIP and keep the model cache warm before running
OpenCLAW offline:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache
```

When OpenCLAW has a DB-cached profile payload, pass it directly:

```bash
python3 tools/jewelry_detector.py embed \
  --image /path/image.jpg \
  --image-id img_123 \
  --out /tmp/img_123.embedding.json \
  --provider siglip \
  --model-id google/siglip-base-patch16-224 \
  --device cpu \
  --image-size 224 \
  --offline-model-cache \
  --profile /tmp/img_123.profile.json
```

Missing or invalid input returns nonzero and writes structured error JSON to the
requested `--out` path:

```json
{
  "schema_version": "1.0",
  "status": "error",
  "error": {
    "type": "FileNotFoundError",
    "message": "image does not exist: /path/missing.jpg"
  }
}
```

## Production Storage And Matching

OpenCLAW should persist one embedding row per returned crop. Store and filter on
`embedding_model`, `preprocess_version`, and `embedding_dim` so stale or
incompatible embeddings are excluded from retrieval.

Profile cache flow:

```text
1. Compute or look up source_sha256 for the image.
2. Query image_profiles by source_sha256 + model + prompt_version + max_image_size.
3. If found, write the stored profile payload to a temp file and pass it
   to embed --profile.
4. If missing, call profile, store profile_json and raw_response_json in
   image_profiles, then pass that payload to embed --profile.
5. Store returned crop embeddings in image_embeddings.
```

Logical tables:

```text
product_images
- id
- product_id
- source_uri
- sha256
- width
- height
- status
- created_at

image_profiles
- id
- image_id
- source_sha256
- model
- prompt_version
- max_image_size
- cache_key
- profile_json
- raw_response_json
- status
- created_at

image_embeddings
- id
- product_id nullable for new/unmatched uploads
- image_id
- crop_id
- view_type
- crop_box
- crop_source
- risk_flags
- embedding vector(768)
- embedding_model
- preprocess_version
- embedding_dim
- source_sha256
- active
- created_at

matching_policies
- id
- name
- embedding_model
- preprocess_version
- top_k
- candidate_min_score
- auto_match_score
- review_min_score
- margin_threshold
- active
- created_at

match_attempts
- id
- input_image_id
- policy_id
- status: matched | needs_review | no_match | failed
- selected_product_id nullable
- confidence
- reason
- created_at

match_candidates
- id
- match_attempt_id
- product_id
- embedding_id
- rank
- similarity
- score
- margin
- decision_reason
```

Recommended v1 policy:

```json
{
  "name": "jewelry-siglip-v1",
  "embedding_model": "siglip-google_siglip-base-patch16-224-cpu-s224",
  "preprocess_version": "jewelry-evidence-v1",
  "top_k": 20,
  "candidate_min_score": 0.82,
  "review_min_score": 0.86,
  "auto_match_score": 0.93,
  "margin_threshold": 0.03
}
```

Vector search must filter compatible active catalog embeddings:

```sql
WHERE active = true
  AND product_id IS NOT NULL
  AND embedding_model = :policy_embedding_model
  AND preprocess_version = :policy_preprocess_version
  AND embedding_dim = :query_embedding_dim
```

Decision rules:

```text
top score >= auto_match_score and margin >= margin_threshold -> matched
top score >= review_min_score or margin < margin_threshold or crop risk flags present -> needs_review
top score < candidate_min_score -> no_match
```

`match-image`, `match-dir`, and `match-embedding` apply these rules and persist
`match_attempts` plus `match_candidates`.

## DB Indexing Responsibility

The detector now includes narrow Postgres/pgvector commands for the OpenCLAW
jewelry schema: `init-db`, `store-profile`, `store-embedding`, and
`match-embedding`.
OpenCLAW still owns product IDs, tenants, transactions around larger workflows,
review state, and policy decisions.

Most users should call `index-dir`, `match-image`, or `match-dir`. The lower-level
`profile`, `embed`, `store-profile`, `store-embedding`, and `match-embedding`
commands are available for custom queues, retries, debugging, or integrating
with an existing OpenCLAW job runner.

Profile JSON to `image_profiles`:

```text
image_id          <- payload.image_id
source_sha256     <- payload.source_sha256
model             <- payload.model
prompt_version    <- payload.prompt_version
max_image_size    <- payload.max_image_size
cache_key         <- payload.cache_key
profile_json      <- payload.profile
raw_response_json <- payload.raw_response
status            <- "ready" unless payload.status == "error"
```

Embedding JSON to `image_embeddings`, one row per crop:

```text
product_id          <- known catalog product id for dir1, null for incoming dir2
image_id            <- payload.image_id
crop_id             <- crop.crop_id
view_type           <- crop.view_type
crop_box            <- crop.box
crop_source         <- crop.source
risk_flags          <- crop.risk_flags
embedding           <- crop.embedding
embedding_model     <- payload.embedding_model
preprocess_version  <- payload.preprocess_version
embedding_dim       <- payload.embedding_dim
source_sha256       <- payload.source_sha256
active              <- true
```

For catalog indexing, wrap `product_images`, `image_profiles`, and
`image_embeddings` inserts in one OpenCLAW transaction. For incoming matching,
store the incoming `product_images`/`image_profiles` row, run vector search using
the returned query embeddings, then write `match_attempts` and
`match_candidates` in the same transaction as the decision.

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

The benchmark wrapper expects:

- Python 3
- local Python dependencies from `requirements-local.txt`
- SigLIP model files already available in the local Hugging Face cache when
  `--offline-model-cache` is used

If model files are not cached, run a non-offline model setup once from a trusted
developer machine, then keep OpenClaw runs offline.

For production OpenCLAW integration, prefer the setup guide at the top of this
document and call `tools/jewelry_detector.py`, not the benchmark wrapper.

## Common Failures

- `OPENAI_API_KEY is not set`: set it before calling `jewelry_detector.py
  profile`, or use `--mock-response` only for plumbing tests.
- model cache error with `--offline-model-cache`: run once with
  `jewelry_detector.py embed` without `--offline-model-cache` from a trusted
  developer machine to populate the local cache, then rerun offline.
- `no image files found`: verify that the input directory has product
  subfolders containing `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.tif`, or
  `.tiff` files.
- very low precision or nonzero merge errors: do not auto-accept the run; report
  `needs review` and inspect the HTML review page.

## Production Boundary

The wrapper benchmarks folder-labeled datasets. It is not the production
single-image matching API. Production OpenCLAW should use `embed` for
crop embeddings, then run pgvector retrieval, policy decisions, match
persistence, and review handling inside OpenCLAW.
