# Plan: Product-Level Jewelry Clustering Benchmark

Generated on 2026-05-26
Status: Draft
GStack review mode: engineering plan

## Goal

Build stage C on top of the validated normalization layer.

Stage B produced:

```text
464 image occurrences -> 101 visual assets
0 reference conflicts
```

Stage C should answer:

> Can we cluster the 101 normalized visual assets into same-physical-product
> groups, compare them against the reference clusters, and surface only the
> important disagreements for review?

## Step 0: Scope Challenge

### What Already Exists

- `tools/jewelry_cluster_benchmark.py normalize` already builds:
  - `data/normalized/manifest.csv`
  - `data/normalized/visual_assets.json`
  - `data/normalized/normalization_report.md`
  - HTML review sheets
- `data/normalized/manifest.csv` already provides:
  - `asset_id`
  - `preferred_path`
  - `quality_path`
  - `reference_cluster_ids`
  - source/kind/flag metadata
- The reference clusters are useful as benchmark labels, but not perfect ground truth.

### Minimum Useful Version

The minimum useful version for the current dataset is not "full AI product
understanding." The current images are product photos from different angles, not
model-worn photos, so full-image embeddings are enough for the first real
benchmark.

It is:

1. Embed one preferred full image per visual asset.
2. Build a similarity graph between assets.
3. Form conservative predicted clusters.
4. Benchmark against reference cluster labels.
5. Generate review sheets for merges, splits, and nearest-neighbor ambiguity.

### Scope Decision

Do not build catalog matching, OpenClaw integration, Airtable integration, or Shopify
logic in this stage.

Do not call a large vision model for every pair. With 101 assets, pairwise review
would be 5,050 comparisons before crops. That is expensive, slow, and hard to audit.

Use crop generation or a large vision model only after the full-image baseline
shows a real failure mode.

Use the large vision model for:

- jewelry crop detection when cheap crop heuristics are weak
- ambiguous pair review near the clustering threshold
- structured explanations for disagreement cases

## Architecture

```text
data/normalized/
  manifest.csv
  visual_assets.json
        |
        v
  Image Selector
    - preferred full image
        |
        v
  Embedding Engine
    - one vector per visual asset in the baseline
    - optional crop vectors in later phases
    - cache by image hash + crop spec + model id
        |
        v
  Similarity Graph
    - node = visual_asset
    - edge = strong same-product evidence
    - edge evidence = full-image similarity in baseline
    - optional best crop-pair similarity in later phases
        |
        v
  Clusterer
    - connected components at conservative thresholds
    - threshold sweep for benchmark comparison
        |
        v
  Benchmark + Review Output
    - predicted_clusters.json
    - threshold_report.md
    - merge/split/disagreement sheets
```

## Component Boundaries

### Manifest Loader

Reads `manifest.csv` and `visual_assets.json`.

Responsibilities:

- validate required fields
- ignore missing files with a named error
- expose one `VisualAsset` object per row

### Image/Crop Generator

Selects the image view to embed.

Initial baseline:

- `full`: full preferred image

Deferred crop candidates:

- `foreground`: white-background/non-background bounding crop
- `center_square`: centered square crop
- `quality_full`: full `quality_path` image when different from preferred
- `ai_jewelry_box`: object box from a vision model
- `ai_jewelry_mask`: segmentation crop if detector confidence is high

### Embedding Engine

Converts crop images into vectors.

Must be adapter-based:

```text
EmbeddingProvider
  embed(image_path, crop_spec) -> vector
```

The first implementation can support one provider, but the interface must allow
swapping providers without rewriting clustering.

Provider priority:

1. Local SigLIP full-image embeddings for the current v1 benchmark winner.
2. DINOv2 and OpenCLIP full-image embeddings as comparison providers.
3. Remote multimodal embedding API if local inference is too slow on production hardware
   or if it gives a clearly better benchmark score at acceptable cost.
4. Hash-only fallback for plumbing tests only, explicitly marked as `not_quality_valid`.

Recommended first provider:

- `google/siglip-base-patch16-224` as the local v1 model.
- Comparison trial: `facebook/dinov2-small`, `facebook/dinov2-base`, and
  `openai/clip-vit-base-patch32` for regression checks.
- Upgrade trial: larger SigLIP/SigLIP2 or DINOv2 variants only if local v1 cannot
  produce a good candidate queue.

Reasoning:

- The current problem is visual instance retrieval: "is this the same physical
  jewelry piece from another angle?"
- DINOv2 is image-only and optimized as a general visual feature backbone, so it
  was a sensible first baseline for shape, stone layout, setting, and texture
  similarity.
- CLIP/SigLIP-style models are valuable, but their image-text training can bias
  embeddings toward semantic similarity such as "gold ring" or "diamond pendant."
  That is useful for catalog labels, but can be dangerous when two different
  products share the same style.
- The implementation should benchmark at least two providers before treating one
  as final.

Current local benchmark result:

- `siglip-base-patch16-224` at threshold `0.94`: 101/101 assets embedded, 60
  predicted clusters, 19 singletons, precision 1.000, recall 0.345, 0 merge
  disagreements, 27 split disagreements.
- Candidate queue mode with SigLIP at threshold `0.92`: precision 0.935, recall
  0.487, 62 candidate positive pairs, 1 merge disagreement in the benchmark.
- GPT-4.1 mini adjudication over 58 SigLIP candidate pairs: candidate-queue
  precision 1.000, candidate-queue recall 0.964, 0 false positives, 2 false
  negatives. Global recall across all benchmark same-product pairs is 0.445
  because candidate generation only surfaced 55/119 same-product benchmark pairs.
- `dinov2_vits14` at threshold `0.97`: 101/101 assets embedded, 69 predicted
  clusters, 37 singletons, precision 1.000, recall 0.269, 0 merge disagreements,
  32 split disagreements.
- `dinov2_vitb14` at threshold `0.97`: 101/101 assets embedded, 70 predicted
  clusters, precision 1.000, recall 0.261, 0 merge disagreements, 32 split
  disagreements.
- `openai/clip-vit-base-patch32` at threshold `0.99`: 101/101 assets embedded,
  precision 1.000, recall 0.252, 0 merge disagreements, 32 split disagreements.
- Decision: use SigLIP as the local v1 default, but treat local embeddings as
  candidate generation rather than final product truth.

Deployment policy:

- Development laptop: local models are preferred for iteration and cost control.
- Production N150 server: run local embeddings only as an async/background job.
- Online embeddings are allowed when they are cost-effective, but only behind the
  same `EmbeddingProvider` interface and with cache keys that include provider
  name, model id, and image hash.
- Never block the WhatsApp/OpenClaw interaction on an uncached batch embedding run.
- Persist embeddings so each image is paid for or computed once unless the model
  changes.

### Similarity Graph

Builds asset-to-asset similarity from crop embeddings.

For each asset pair in the baseline:

- compare the full-image embedding from asset A with asset B
- create an edge only when the score exceeds the selected threshold

For later crop-enabled runs:

- compare all crop embeddings from asset A with all crop embeddings from asset B
- keep the best score and the crop pair that produced it

The graph must preserve evidence:

```json
{
  "source_asset_id": "A0025",
  "target_asset_id": "A0031",
  "score": 0.912,
  "source_view": "full",
  "target_view": "full",
  "provider": "embedding-provider-name",
  "threshold": 0.89
}
```

### Clusterer

Uses connected components over high-confidence graph edges.

Policy:

- prefer over-splitting to over-merging
- do not force every asset into a multi-asset cluster
- singletons are valid predicted clusters
- run a threshold sweep before recommending one threshold

### Benchmark Reporter

Compares predicted clusters against `reference_cluster_ids`.

Metrics:

- pairwise precision
- pairwise recall
- F1
- merge errors
- split errors
- singleton rate
- unscored assets

Important: report "reference disagreement," not "wrong," because reference clusters
are imperfect.

## AI Vision Model Usage

AI vision is useful, but should be used as leverage, not brute force.

Use AI vision for:

1. Detecting jewelry boxes in difficult images.
2. Describing product-visible attributes for ambiguous pairs.
3. Reviewing top disagreement cases after embedding clustering.

Do not use AI vision for:

- every pairwise comparison
- final business decisions
- overwriting benchmark labels automatically

Decision rule:

```text
cheap embeddings find candidate relationships
AI vision explains or adjudicates ambiguous cases
human reviews the remaining hard cases
```

## Data Flow

### Happy Path

```text
manifest.csv
  -> load 101 visual assets
  -> select preferred full image per asset
  -> embed each full image once and cache vector
  -> compare asset pairs by cosine similarity
  -> threshold sweep creates candidate clusters
  -> benchmark against reference labels
  -> write JSON, Markdown, and HTML review sheets
```

### Missing Input Path

```text
manifest missing / malformed
  -> raise ManifestLoadError
  -> print clear CLI error
  -> exit non-zero
  -> write no partial benchmark
```

### Empty Input Path

```text
manifest has zero assets
  -> raise EmptyManifestError
  -> write small report explaining no assets were clustered
  -> exit non-zero
```

### Embedding Failure Path

```text
embedding provider fails on one crop
  -> mark crop as failed
  -> continue if asset has another usable crop
  -> fail asset if all crops fail
  -> include failures in report
  -> clustering excludes failed assets from metrics
```

### AI Vision Failure Path

```text
AI detector unavailable / timeout
  -> skip AI crop candidate
  -> continue with deterministic crops
  -> report detector_unavailable
```

## State Machine

```text
RAW_ASSET
  -> CROPS_GENERATED
  -> EMBEDDED
  -> COMPARED
  -> CLUSTERED
  -> BENCHMARKED

Invalid transitions:
  RAW_ASSET -> EMBEDDED       blocked: no crop specs
  CROPS_GENERATED -> CLUSTERED blocked: no similarity graph
  COMPARED -> BENCHMARKED     blocked: no predicted clusters
```

## Output Files

```text
results/clustering/
  crops/
    A0001_full.jpg
    A0001_foreground.jpg
  embeddings/
    embeddings.jsonl
    embedding_cache_index.json
  similarity_edges.json
  predicted_clusters.json
  threshold_sweep.csv
  benchmark_report.md
  benchmark_report.json
  review_sheets/
    01_predicted_clusters.html
    02_reference_clusters.html
    03_merge_errors.html
    04_split_errors.html
    05_ambiguous_neighbors.html
```

## CLI Shape

```bash
python3 tools/jewelry_cluster_benchmark.py cluster \
  --manifest data/normalized/manifest.csv \
  --assets data/normalized/visual_assets.json \
  --out results/clustering \
  --provider local-or-remote \
  --thresholds 0.80,0.83,0.86,0.89,0.92
```

Optional AI-assisted crop mode:

```bash
python3 tools/jewelry_cluster_benchmark.py cluster \
  --manifest data/normalized/manifest.csv \
  --assets data/normalized/visual_assets.json \
  --out results/clustering \
  --provider local-or-remote \
  --ai-crops ambiguous-only
```

## Test Plan

No test framework exists yet. Add Python `unittest` tests using stdlib only unless
the project later adopts `pytest`.

### Coverage Diagram

```text
cluster command
  ├── load_manifest()
  │   ├── valid manifest                       [unit]
  │   ├── missing required column              [unit]
  │   ├── missing image path                   [unit]
  │   └── empty manifest                       [unit]
  ├── select_images()
  │   ├── full crop always produced            [unit]
  │   ├── preferred path selected              [unit]
  │   ├── missing preferred path fallback       [unit]
  │   └── quality path fallback                [unit]
  ├── generate_crops()                         [deferred]
  │   ├── foreground crop on white background  [unit]
  │   ├── no foreground found fallback         [unit]
  │   └── crop bounds stay inside image        [unit]
  ├── embed_crops()
  │   ├── cache hit                            [unit]
  │   ├── cache miss                           [unit]
  │   ├── provider failure for one crop         [unit]
  │   └── all crops fail for asset              [unit]
  ├── build_similarity_graph()
  │   ├── keeps best crop-pair score            [unit]
  │   ├── creates edge above threshold          [unit]
  │   └── rejects edge below threshold          [unit]
  ├── cluster_components()
  │   ├── connected assets become cluster       [unit]
  │   ├── singleton remains singleton           [unit]
  │   └── threshold sweep produces variants     [unit]
  └── benchmark()
      ├── pairwise precision/recall             [unit]
      ├── merge error detection                 [unit]
      ├── split error detection                 [unit]
      └── unscored asset handling               [unit]
```

### Golden Dataset

Create a tiny synthetic fixture:

```text
tests/fixtures/cluster_small/
  manifest.csv
  visual_assets.json
  images/
```

Fixture shape:

- 2 assets from same product
- 1 visually similar but different product
- 1 singleton product
- 1 missing image path
- deterministic fake embeddings for tests

Do not make tests depend on a real AI model. Provider behavior should be mocked or
implemented with deterministic fixture vectors.

## Performance Plan

With 101 assets and one full-image embedding per asset:

```text
101 embeddings
5,050 asset pairs
5,050 embedding comparisons
```

With later crop-enabled runs at 3-5 crops per asset:

```text
~300-500 embeddings
~5,050 asset pairs
~45,000-125,000 crop-pair comparisons
```

Both are fine locally if embeddings are cached. The baseline should be fast enough
to rerun while tuning thresholds.

Required performance behaviors:

- cache embeddings by image hash, crop spec, and provider id
- never recompute embeddings when cache is valid
- write progress logs every N assets
- threshold sweep must reuse the same similarity matrix

## Failure Modes

| Failure | Impact | Handling |
| --- | --- | --- |
| Embedding provider unavailable | no clustering | fail clearly unless hash fallback explicitly requested |
| AI crop model unavailable | weaker crops | continue with deterministic crops and report skipped AI |
| Full-image embedding misses same product from angle change | false negative | split-error sheets and threshold sweep catch it |
| Hand/model dominates future images | false negative | deferred AI crop mode handles it |
| Similar designs merge | bad product cluster | conservative threshold and merge-error sheets |
| Same product split | extra manual work | split-error sheets and threshold sweep |
| Reference label wrong | false benchmark issue | report as disagreement, not truth |
| Cache stale after provider change | wrong scores | provider id included in cache key |

## NOT In Scope

- Airtable/catalog matching.
- Shopify upload.
- OpenClaw transport: CLI/HTTP/MCP decision.
- Training a custom jewelry detector.
- Full pairwise large-model comparison across all assets.
- Automatic correction of reference clusters.
- Human review UI beyond generated HTML sheets.

## Worktree Parallelization Strategy

Sequential implementation is acceptable because the core work touches one tool and
one output format.

Possible lanes if splitting:

| Step | Modules touched | Depends on |
| --- | --- | --- |
| Crop generation | `tools/`, `tests/fixtures/` | manifest loader |
| Embedding provider/cache | `tools/`, `tests/` | crop generation |
| Graph/cluster/benchmark | `tools/`, `tests/` | embedding vectors |
| HTML review sheets | `tools/` | benchmark output |

Execution order:

```text
manifest loader -> crop generator -> embedding cache -> graph/cluster -> benchmark -> review sheets
```

Parallelism is low-value until the embedding provider interface is defined.

## Implementation Phases

### Phase 1: Plumbing With Fake Embeddings

- Add `cluster` command.
- Load manifest/assets.
- Select preferred full images but do not require real model inference.
- Use deterministic fake embeddings in tests.
- Implement graph, threshold sweep, benchmark, and output JSON/Markdown.

Purpose: prove benchmark math and reports before model variability enters.

### Phase 2: Real Embedding Provider

- Add first real provider.
- Cache embeddings.
- Run on 101 assets.
- Produce first real threshold sweep.
- Use full-image embeddings only.

Provider choice should be decided after checking what can run on the target server.

### Phase 3: AI Crop/Review Adapter

- Add optional deterministic and AI crop detection if full-image embedding errors
  show that cropping is needed.
- Add optional AI review for ambiguous edges near threshold.
- Keep all AI outputs cached and auditable.

## Success Criteria

Phase 1 success:

- deterministic tests pass
- benchmark output is correct on fixture data
- generated review sheets are readable

Phase 2 success:

- runs on 101 normalized assets
- threshold report shows precision/recall tradeoff
- merge errors are explicitly listed
- predicted clusters can be reviewed visually

Phase 3 success:

- model/hand/background photos improve without brute-force pairwise AI calls
- ambiguous cases shrink rather than explode

## Open Decisions

1. Which embedding provider should run first on the server? Answer: DINOv2 base,
   with OpenCLIP/SigLIP as benchmark comparison providers.
2. Should foreground crops be deterministic-only in Phase 2, or should AI crop
   detection be included immediately? Answer: neither is required immediately;
   start with full-image embeddings.
3. What threshold posture should be default: very conservative or balanced?
   Answer: very conservative.

Recommendation:

- Phase 1: implement model-free plumbing and tests.
- Phase 2: choose provider based on server capabilities and use full-image embeddings.
- Phase 3: add AI crop/review once baseline errors are visible.

Decision on question 2: defer crop detection. The current dataset has product-only
photos, so cropping is not required for the first benchmark.

Decision on question 3: use a conservative default threshold. A bad merge creates
the wrong product grouping and can pollute the downstream product-info collection
flow. A split mostly creates extra manual work, which is cheaper and easier to
review.
