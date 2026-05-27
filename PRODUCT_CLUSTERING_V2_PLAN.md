# Product Clustering V2 Plan

## Goal

Build a catalog-grade clustering flow for unknown jewelry photos.

The tool must support three identities:

```text
image cluster   = duplicate/crop/export/edit of the same image
product cluster = same exact sellable product/version
design cluster  = same design family or variant family
```

False merges and false splits are both sensitive. The tool should classify when
evidence is strong and ask for human review only when the boundary is genuinely
unclear.

## Important Business Rule

`same_design_variant` is a valid final classification. It is not automatically a
human-review reason.

Human review is required for:

- identity conflict
- variant boundary unclear
- insufficient views
- stone count unclear
- metal/source conflict
- contradictory AI votes
- cluster graph contradiction

Not for:

- confidently same design but different product
- confidently different design
- confidently same physical product

## Prior Evidence

Local embedding baseline:

```text
SigLIP at threshold 0.94
precision: 1.000
recall: 0.345
```

SigLIP candidate queue at threshold 0.92:

```text
58 candidate pairs
55 benchmark same-product
3 benchmark different-product
```

GPT-4.1 mini adjudication over those 58 pairs:

```text
candidate-queue precision: 1.000
candidate-queue recall: 0.964
global recall across all benchmark same-product pairs: 0.445
```

Conclusion:

```text
AI pair adjudication works when the right pair is shown.
The current bottleneck is candidate generation.
```

## Source Of Truth From HAL

HAL has a strong single-image observation prompt:

```text
stav/prompts/vision_v1_4.txt
```

It is image-local, not product-level. Use it as the observation layer, not as the
final product fingerprint.

The useful fields are:

```text
dominant_subject_category
dominant_form
metal_tone_family_visible
metal_finish_visible
stones[].species
stones[].shape
stones[].setting
stones[].quantity
stones[].position
stones[].color_observed
stones[].transparency
stones[].surface_features
closure_type_visible
structural_features_visible
hollow_construction_visible
adjustable_visible
symmetry_visible
camera_angle
framing_type
image_role_candidate
full_silhouette_visible
stone_detail_visible
structure_detail_visible
diagnostic_notes
```

## Identity Semantics

Same physical/sellable product:

- same exact catalog item version
- same product type/form
- same stone layout/count
- same metal/version/material class
- same size/model when visible or source-confirmed
- same finish/texture/charm configuration
- different views/crops/model shots of same exact item belong together

Same design variant:

- same design family
- different stone color/species in same layout
- yellow/rose/white gold versions of same form
- explicit S/L/size variants when proportions are same family
- same motif across variants

Different design:

- different product type
- different dominant form
- changed focal geometry
- motif/texture/finish difference that defines the design
- stone count/layout mismatch unless explicitly size-related
- with/without stones when it changes visual structure

## V2 Pipeline

```text
manifest.csv
  -> embeddings and top-K retrieval
  -> optional AI image observations
  -> deterministic product/design fingerprint
  -> broad candidate generation
  -> AI product/design adjudication
  -> signed graph clustering
  -> cluster consistency checks
  -> review queue
  -> OpenClaw export
```

## Candidate Generation

Use multiple candidate sources:

- SigLIP score above candidate threshold
- top-K nearest neighbors per asset
- near-neighbor union across providers when available
- fingerprint similarity once observations exist
- source/folder/name evidence when available

Candidate generation should optimize recall. It is acceptable to send more pairs
to AI, as long as the count stays affordable and the AI can reject false matches.

Current next implementation:

```text
candidate-v2 = threshold pairs + top-K pairs
```

## AI Pair Adjudication Labels

Use these labels:

```text
same_physical_product
same_sellable_product
same_design_variant
different_design
unsure
```

For benchmark compatibility:

```text
same_physical_product and same_sellable_product => product-same positive
same_design_variant, different_design, unsure => product-same negative/missed
```

## AI Prompt Requirements

The AI receives:

- image A
- image B
- available observation/fingerprint data when present
- product vs design definitions
- instruction that same design is not same product
- instruction that confident same_design_variant is a valid final answer

It must return JSON:

```json
{
  "decision": "same_physical_product",
  "confidence": 0.92,
  "product_evidence": [],
  "design_evidence": [],
  "difference_evidence": [],
  "variant_fields": {
    "product_type": null,
    "metal_tone_family": null,
    "stone_layout": null,
    "stone_count_total": null,
    "finish_texture": [],
    "charms": []
  },
  "review_required": false,
  "review_flags": []
}
```

## Graph Clustering

Build a signed graph:

- positive product edge: `same_physical_product`, `same_sellable_product`
- design edge: `same_design_variant`
- negative edge: `different_design`
- uncertain edge: `unsure`

Product clusters are connected components of positive product edges, unless a
negative/uncertain contradiction exists inside the component.

Design clusters can connect product clusters by design edges.

## Review Queue

Review is for unresolved risk only:

- likely missed product sibling
- product cluster contains negative edge
- product cluster has weak evidence only
- design/product boundary unclear
- AI contradiction
- singleton with strong top-K candidates but no accepted edge

## V2 Implementation Order

1. Add top-K candidate generation to the existing cluster command. DONE.
2. Upgrade AI pair labels from same/different/unsure to product/design labels. DONE.
3. Add benchmark compatibility for product-same labels. DONE.
4. Add review sheets grouped by decision label. DONE.
5. Run AI proof on candidate-v2 and compare. DONE:
   - candidate count
   - candidate same-product coverage
   - AI precision/recall
   - global recall
6. Build product/design cluster export from AI decisions. DONE.
7. Add AI observation/fingerprint extraction as the next layer if top-K still
   does not surface enough true pairs.

## Implemented Slice

Implemented in `tools/jewelry_cluster_benchmark.py`:

- `--candidate-top-k`
- candidate reasons per pair
- candidate coverage report
- product/design AI labels
- product-same benchmark compatibility
- decision-specific AI review sheets

First candidate-v2 run:

```text
provider: SigLIP google/siglip-base-patch16-224
candidate threshold: 0.92
top-K neighbors per asset: 5
candidate pairs: 313
candidate same-reference pairs: 115
candidate different-reference pairs: 198
total benchmark same-reference pairs: 119
candidate recall ceiling: 0.966
```

Interpretation:

```text
The old 58-pair queue was too narrow.
The new top-K queue is much broader and surfaces almost all benchmark same-product
pairs, but it needs AI/product-design adjudication to filter false candidates.
```

Next paid proof:

```text
Run completed:
  candidates: 313
  product-same true positives: 113
  product-same false positives: 0
  product-same false negatives inside candidate queue: 2
  product-same candidate precision: 1.000
  product-same candidate recall: 0.983
  product-same global recall: 0.950
  same_design_variant: 20
  human-review-required pair decisions: 0
```

The cluster export now writes:

```text
results/ai_cluster_export_v2_topk/
  product_clusters.json
  design_clusters.json
  cluster_edges.json
  cluster_review_queue.json
  cluster_summary.json
  asset_cluster_assignments.csv
  cluster_export_summary.md
  review_sheets/
    20_product_clusters.html
    21_design_clusters.html
    22_cluster_review_queue.html
```

Current export:

```text
assets: 101
product clusters: 34
product singletons: 1
design clusters: 29
product-same edges: 113
same-design edges: 20
different-design/product edges: 180
unsure edges: 0
review queue items: 2
```

The two review items are transitive contradictions: the AI links each cluster
through several product-same edges but marks one internal pair as
`different_design`. Those are the right cases to surface for visual review
instead of silently trusting the connected component.

## Dataset Correction: Photoshop Edits Are Not Variants

The first V2 prompt overused `same_design_variant` for metal-tone/color
differences. The deeper correction is that these are not separate product
photos at all: fixed/unfixed are the same source images with small Photoshop
adjustments.

Therefore this must be handled before product clustering:

```text
fixed/unfixed edit dedup -> visual assets -> product clustering
```

not:

```text
visual assets -> AI decides whether fixed/unfixed are same product
```

The prompt now says:

```text
Color or metal-tone difference alone must not be classified as
same_design_variant when structure, shape, texture, stone layout, and
proportions otherwise match.
```

After rejudging the 20 prior `same_design_variant` pairs:

```text
same_physical_product: 112
same_sellable_product: 7
same_design_variant: 7
different_design: 187
```

The remaining 7 same-design labels are stone/no-stone or stone-setting
differences. Because this dataset has no intended design-family grouping, V1
exports should disable design variants:

```bash
python tools/jewelry_cluster_benchmark.py build-clusters \
  --manifest data/normalized/manifest.csv \
  --decisions results/ai_adjudication_v2_topk/ai_decisions.json \
  --out results/ai_cluster_export_v2_topk_no_design \
  --no-design-variants
```

Current V1 product-only export:

```text
assets: 101
product clusters: 32
product singletons: 1
design clusters: 32
product-same edges: 119
same-design edges: 0
different-design/product edges: 194
review queue items: 3
```

Note: benchmark precision now reports lower because the reference folders split
some Photoshop-edited same-ring examples into different labels. User feedback
overrides those benchmark labels for this case.

## V3 Normalization: Edit Dedup First

Added normalization option:

```bash
python tools/jewelry_cluster_benchmark.py normalize \
  --fixed data/extracted/fixed/fix \
  --unfixed data/extracted/unfixed/2025-03-19 \
  --reference data/extracted/reference/לקטלג \
  --out data/normalized_edit_dedup \
  --edit-dedup
```

The edit-dedup pass matches only `fixed` to `unfixed` by conservative
mutual-nearest perceptual hash, same image kind, and same dimensions. It does
not use this path for product-level clustering; it is only for tiny
Photoshop/fix adjustments of the same source photo.

Result:

```text
old visual assets: 101
new visual assets: 67
fixed/unfixed edit matches: 66
assets containing fixed+unfixed: 34
```

Running SigLIP candidate generation on the corrected normalization:

```text
assets: 67
candidate pairs: 215
candidate same-reference pairs: 35
candidate different-reference pairs: 180
total same-reference pairs: 36
candidate recall ceiling: 0.972
```

This is the right shape for V1: fewer duplicated visual assets, still broad
candidate coverage, and the fixed/unfixed Photoshop problem removed from the AI
product decision layer.

## V3 AI Product-Only Result

Ran AI adjudication on the corrected 215-pair V3 queue:

```text
candidate pairs: 215
true positives: 34
false positives: 1
false negatives: 1
true negatives: 179
same-design variants: 3
precision: 0.971
recall inside candidate queue: 0.971
global recall across benchmark same-product pairs: 0.944
```

Then built a product-only export:

```bash
python tools/jewelry_cluster_benchmark.py build-clusters \
  --manifest data/normalized_edit_dedup/manifest.csv \
  --decisions results/ai_adjudication_v3_edit_dedup/ai_decisions.json \
  --out results/ai_cluster_export_v3_edit_dedup_no_design \
  --no-design-variants
```

Export result:

```text
assets: 67
product clusters: 34
product singletons: 4
product-same edges: 35
same-design edges: 0
review queue items: 1
```

The remaining review item is exactly the type of issue the tool should surface:
one product cluster has a negative internal edge due to transitive pair
disagreement.

## Hardening Pass

The export contract is now safer for OpenClaw:

```text
product_clusters.json         = approved clusters only
blocked_product_clusters.json = clusters with contradictions/review flags
product_clusters_all.json     = full diagnostic set
cluster_review_queue.json     = review queue for humans/OpenClaw handoff
```

Current hardened export:

```text
results/ai_cluster_export_v3_hardened_no_design
approved product clusters: 33
blocked product clusters: 1
total product clusters: 34
```

The AI cache is now versioned by:

```text
pair id
model
prompt version
max image size
source image hash
target image hash
```

This prevents prompt/model/image changes from silently reusing old pair
judgments.

Normalization also writes a dedicated edit-dedup review sheet:

```text
review_sheets/07_edit_duplicate_matches.html
```

AI adjudication now has:

```text
--from-cache
--retries
flush=True progress logging
retry/backoff for transient network/API failures
```

## Success Bar

Near-term:

```text
AI precision >= 0.98
global recall materially above 0.445
candidate count still affordable
```

Catalog-grade:

```text
product clusters complete enough for OpenClaw to collect product info once
review queue contains uncertain cases instead of silent false splits
design variants are separated from exact products without human review when clear
```
