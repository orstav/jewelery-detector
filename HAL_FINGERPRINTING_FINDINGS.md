# HAL Fingerprinting Findings

Source: read-only HAL bridge and direct read-only server inspection on
`/home/server/.openclaw/workspace`.

## Main Finding

OpenClaw does not currently have a maintained product-level jewelry clustering
tool.

It has a strong single-image observation prompt:

```text
stav/prompts/vision_v1_4.txt
```

The prompt is intentionally image-local:

- analyze exactly one image
- direct visual observation only
- no cross-image merge assumptions
- strict JSON output
- conservative gemstone and metal detection
- no product-level inference from partial views

This is useful, but it is not itself a product fingerprint. It is the raw
observation layer we should build product fingerprints from.

## Existing Model / Storage

Stored backfill is mostly:

```text
provider: Gemini
model: gemini-2.5-pro
prompt: vision_v1_4
temperature: 0.1
```

Local DB:

```text
/home/server/.openclaw/workspace/vision_analysis.db
table: vision_data
rows: 1613
raw_vision_response rows: 1611
models: gemini-2.5-pro mostly, one gemini-2.5-flash row
```

Important fields:

```text
airtable_record_id
source_folder
filename
image_url
drive_link
raw_vision_response
image_id
confidence_score
image_role
category
model_used
analysis_status
metal_color
primary_stone_species
image_phash
```

`image_phash` exists but is currently empty in this DB. Use
`raw_vision_response` as the primary data source.

## Useful Observation Fields

The fields most relevant for clustering are:

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

## Existing Product-Matching Attempts

HAL found only historical/partial artifacts.

Design-candidate JSON artifacts:

```text
workbench/design-migration/full-vision-structural-candidates-v1.json
workbench/design-migration/full-vision-structural-candidates-v2-strict.json
workbench/design-migration/full-vision-structural-candidates-v3-strict.json
workbench/design-migration/full-vision-structural-candidates-v4-post-review.json
```

The generation code was not preserved.

Inferred scoring logic:

- summarize each product from full vision observations
- compare type/category
- compare dominant form
- compare center stone shape/setting
- compare accent stone shape/setting/count
- use meaningful name-token overlap
- reject generic token evidence like `gold`, `diamond`
- penalize accent quantity mismatch and structural contradictions

Known limitation: historical false positives remained; v4 post-review is empty,
which suggests review suppressed remaining candidates.

Older plan:

```text
workbench/design-migration/feature1-evidence-backed-clustering-plan-v1.md
workbench/design-migration/feature1-evidence-backed-clustering-plan-v1.json
```

Limitation: too name-driven and too dependent on flattened fields.

Duplicate detection exists, but it is image-level, not product-level:

```text
/home/server/.openclaw/skills/duplicate-detector/detect.py
stav/tools/vision_then_duplicates.py
stav/tools/stav_processor/rules/duplicates.py
```

Limitations:

- exact/pHash duplicate oriented
- filename or flattened metal oriented
- not wired to `vision_analysis.db` v1.4 raw JSON
- not designed for same product across different angles

## Design Implication

Our next tool should have three layers:

```text
1. Image observation layer
   - reuse/adapt vision_v1_4-style structured fields

2. Product fingerprint layer
   - deterministic summary built from one or many image observations
   - separates strong identity evidence from weak generic evidence

3. Candidate/adjudication layer
   - embeddings retrieve possible matches
   - fingerprints expand/score candidates
   - AI adjudicates candidate pairs/clusters with evidence
```

## Recommended Matching Rules

Hard filters:

- product category mismatch is usually a block
- multiple products / unclear dominant subject goes to review

Strong positive evidence:

- same non-generic dominant form
- same center stone shape/setting/position
- same accent layout and count
- same closure/structural features
- same distinctive motif or diagnostic note
- same surface texture or metal finish when distinctive
- same silhouette across different angles

Weak evidence only:

- same metal tone
- same broad category
- same common stone species
- same generic form such as simple band/drop/chain

Contradictions:

- different dominant form
- center geometry mismatch
- setting mismatch
- stone count mismatch
- motif/texture/finish mismatch
- product type mismatch
- one image is detail-only and cannot support full-product merge

## Next Build Recommendation

Build `fingerprint` and `candidate-v2` stages:

```text
manifest.csv
  -> AI image observations using vision_v1_4-compatible schema
  -> deterministic product-fingerprint summary per asset
  -> broad candidates from SigLIP top-K + fingerprint similarity
  -> AI pair adjudication with image + fingerprint evidence
  -> graph clustering with positive and negative evidence
  -> cluster-level review
```

This directly attacks the current failure: embeddings plus AI adjudication work
when the right pair is shown, but candidate generation misses too many true
same-product pairs.

## Product vs Design Semantics From HAL

Same physical/sellable product is stricter than same design.

Same product/photo-set means:

- same exact catalog item version
- same jewelry structure from multiple views
- same product type, form, stone layout/count, metal/version, size/model, finish,
  charm configuration, and sellable state
- edited/cropped/exported versions of the same photo are same image cluster

Same design but different product/variant can include:

- yellow/rose/white gold versions of the same form
- different gemstone colors/species in the same stone layout
- explicit size/model variants in the same visual family
- same motif across material/stone variants

Usually different design:

- different product type
- different dominant form
- changed focal geometry
- different motif structure
- visually defining finish/texture difference
- with/without stones when it changes visual structure

Feature-specific rules:

- Stone species/color: same design if layout/shape/setting are the same; different
  product if sold separately.
- Stone count: strong split signal unless count difference is explicitly
  size-related and supported.
- Metal tone: yellow/rose/white gold can be design variants; silver is a
  separate product/material class. Do not infer silver from image; use
  `white_family` unless source confirms silver.
- Finish/texture: brushed/smooth/hammered/polished can split design if visually
  defining.
- Engraving: personal engraving is variant/customization; integral motif
  engraving can define/split design.
- Earring pair/single: image-level only. A single visible earring can still
  belong to a pair product.
- Charms: detachable charm options can be variants; fixed charm arrangement can
  split design/product.

Known false-positive families HAL named:

- Yaara vs Oval Tulip
- Sophie vs Eden
- Tamari vs Calla Lily / Antalya
- Victoria vs Liv
- Arabella vs Sophie
- Noa vs Iris

Output should include:

```text
image_cluster_id
product_cluster_id
design_cluster_id
cluster_type
variant_fields
confidence / confidence_label
positive_evidence
negative_evidence
review_flags
best_evidence_images
```

Implementation rule:

```text
Do not silently merge. Strong auto-grouping needs at least two discriminative
structural matches, or one structural match plus strong source/name/Drive evidence
and no contradictions.
```
