# Design: Jewelry Cluster Benchmark V0

Generated on 2026-05-25
Status: Draft

## Problem

OpenClaw needs a visual capability that can look at a batch of unknown jewelry
photos and group images that show the same physical product. The wider business
workflow already exists elsewhere: Airtable is the catalog source of truth,
OpenClaw handles the operational flow, and Shopify upload is out of scope.

The first version should not try to solve catalog matching. It should test the
core visual question:

> Can an automatic clustering system turn a loose image batch into product-level
> groups that match or improve on the current reference clusters?

## Scope

V0 is a benchmark and review tool.

It takes:

- A reference clustering created by the current manual or semi-manual process,
  stored as one folder per product-like group.
- Optionally, a separate folder of raw/unclustered jewelry images if the raw
  image set is not exactly the same as the clustered folder contents.

It produces:

- Predicted product-level clusters.
- Duplicate and near-duplicate candidates.
- Possible useful detail crops.
- A benchmark report comparing predicted clusters to the reference clusters.
- Contact sheets for fast visual review.

## Explicitly Out Of Scope

- Airtable integration.
- Shopify integration.
- OpenClaw tool transport: MCP, HTTP, CLI, or otherwise.
- Jeweler question generation.
- Final product creation decisions.
- Matching against already-existing catalog products.
- Automatic deletion of duplicate images.

## Inputs

The real dataset may arrive as three imperfect folders:

1. `fixed_unclustered`: edited/fixed images, may not include every product.
2. `unfixed`: original images, may include more images.
3. `clustered_reference`: folder-per-cluster reference batch, may include all
   images, extra images, and may still miss some images.

V0 must not assume these folders contain the same files, the same filenames, or
the same exact pixels.

Observed in the current dataset:

- The fixed zip has 70 images: 35 `print` and 35 `web`.
- The unfixed zip has 126 images: 42 `print`, 42 `web`, and 42 `png`.
- The clustered reference zip has 268 images across 33 cluster folders.
- The reference includes both current/fixed files and `before fix` files.
- Some filenames are reused across fixed and unfixed folders for different image
  content, so filenames must not be treated as stable image identity.

### Raw Images

A directory containing all images in the batch. This is optional for V0 if the
reference cluster folders already contain every image that should be benchmarked.

```text
input/raw_images/
  IMG_001.jpg
  IMG_002.jpg
  IMG_003.jpg
```

### Reference Clusters

Primary supported format: folder-per-cluster.

Folder format:

```text
input/reference_clusters/
  product_001/
    IMG_001.jpg
    IMG_014.jpg
  product_002/
    IMG_002.jpg
    IMG_009.jpg
```

CSV format:

```csv
image_path,reference_cluster_id
IMG_001.jpg,product_001
IMG_014.jpg,product_001
IMG_002.jpg,product_002
```

The reference clusters are not assumed to be perfect. The report should call them
`reference_clusters`, not ground truth.

If `--images` is not provided, the tool should derive the candidate image set by
walking all files inside `--reference`.

## Dataset Reconciliation

Before clustering, the tool should build an inventory table across all provided
folders.

For every image occurrence, record:

- Source folder: `fixed_unclustered`, `unfixed`, or `clustered_reference`.
- Relative path.
- Filename.
- File size.
- Pixel dimensions.
- SHA-256 hash for exact file identity.
- Perceptual hash for near-identical image identity.
- Optional reference cluster ID when the file came from a reference subfolder.

Filenames are useful hints only. The reconciliation layer must prefer file hash,
perceptual hash, and later visual embeddings over filename matching.

## Version-Aware Deduplication

The fixed/unfixed distinction is not just a one-off cleanup detail. It represents
a general requirement: the system must understand that two files can be different
bytes, and even visibly edited, while still representing the same underlying
photo or product asset.

The core model should separate these concepts:

- `image_occurrence`: one file found on disk.
- `visual_asset`: one logical photo/shot after grouping fixed, unfixed, web,
  print, png, and near-identical versions.
- `preferred_occurrence`: the best file to use for clustering/review/export.
- `product_cluster`: a group of visual assets that show the same physical product.

For the current dataset, fixed images should generally win over unfixed images
when both represent the same visual asset. But this should be expressed as source
priority metadata, not as hard-coded folder behavior inside the clustering logic.

Example source priority:

```text
fixed > unfixed > reference_copy
```

The dedup/version resolver should support:

- Exact duplicates by SHA-256 hash.
- Same basename with different hash.
- Same shot exported as `print`, `web`, or `png`.
- Fixed vs unfixed edits with different hashes.
- Minor crop/retouch variants that should collapse to one preferred asset.
- Strong zoom/detail crops that should remain linked assets, not disposable
  duplicates.

This means V0 should run in two layers:

1. Build a normalized manifest of visual assets and preferred files.
2. Cluster the preferred visual assets into product-level groups.

The benchmark should report both layers separately. A version/dedup mistake is
different from a product clustering mistake.

The tool should then create a canonical image set.

Recommended source priority for visual clustering:

1. Use `fixed_unclustered` when an image can be confidently matched there.
2. Fall back to `unfixed`.
3. Fall back to the copy inside `clustered_reference`.

The benchmark comparison should only score images that have a reference cluster
label. Images without a reference label should still be clustered, but reported
as `unlabeled_images` rather than counted as precision or recall errors.

The report should include an inventory summary:

- Count of images per source folder.
- Exact hash overlaps between folders.
- Near-duplicate/perceptual overlaps between folders.
- Files present in reference but missing from fixed/unfixed.
- Files present in fixed/unfixed but missing from reference.
- Files with the same filename but different image content.
- Files with the same content but different filenames.
- Reference files under `before fix` / `befoer fix` paths.
- Visual assets where the preferred occurrence came from the fixed source.
- Visual assets where only an unfixed/reference occurrence exists.

## Outputs

```text
results/
  image_inventory.csv
  image_inventory.json
  predicted_clusters.json
  benchmark_report.md
  benchmark_report.json
  review_sheets/
    predicted_clusters/
    reference_clusters/
    disagreements/
    duplicate_candidates/
```

### Predicted Clusters JSON

```json
{
  "batch_id": "batch_001",
  "clusters": [
    {
      "cluster_id": "P001",
      "visual_assets": ["A001", "A014"],
      "preferred_images": ["IMG_001.jpg", "IMG_014.jpg"],
      "confidence": 0.91
    }
  ],
  "duplicates": [
    {
      "image": "IMG_044.jpg",
      "duplicate_of": "IMG_045.jpg",
      "confidence": 0.98,
      "reason": "near-identical crop and embedding"
    }
  ],
  "detail_crops": [
    {
      "image": "IMG_112.jpg",
      "parent_cluster": "P006",
      "confidence": 0.82,
      "reason": "zoomed jewelry crop, useful detail"
    }
  ],
  "uncertain": []
}
```

### Benchmark Report

The markdown report should include:

- Number of images.
- Number of reference clusters.
- Number of predicted clusters.
- Pairwise precision.
- Pairwise recall.
- F1 score.
- Merge errors.
- Split errors.
- Duplicate candidates.
- Cases where the model disagrees with the reference and may be right.

## Metrics

### Pairwise Precision

Of all image pairs the tool grouped together, what fraction are together in the
reference clusters?

High precision means the tool rarely merges different products.

### Pairwise Recall

Of all image pairs grouped together in the reference clusters, what fraction did
the tool also group together?

High recall means the tool rarely splits one product into many clusters.

### Split Errors

A single reference cluster appears across multiple predicted clusters.

Example:

```text
reference product_001 -> predicted P003, P017, P021
```

### Merge Errors

Multiple reference clusters appear inside one predicted cluster.

Example:

```text
predicted P008 -> reference product_004, product_012
```

Merge errors are more dangerous than split errors because they can cause different
physical products to be treated as one.

## Product-Level Rules

The target clustering level is same physical jewelry product.

Same product:

- Same piece, different angle.
- Same piece on model and off model.
- Same piece with different crop.
- Same piece with small color, exposure, or retouch edits.

Not necessarily same product:

- Same design with different stone.
- Same design with different metal.
- Same design in different size when size maps to a different sellable item.
- Visually similar earrings/rings from the same collection.

Separate relationship:

- A strong zoom/detail crop may be a useful asset for the same product, but should
  not be treated as a disposable duplicate.

## Matching Strategy

V0 should use multiple signals rather than one threshold.

Recommended signals:

- File hash for exact duplicates.
- Perceptual hash for near-identical images.
- Image embeddings for semantic visual similarity.
- Crop/detail heuristics based on image similarity plus composition difference.
- Conservative clustering thresholds to avoid bad merges.
- Source priority rules for selecting fixed images over unfixed images after
  visual-asset deduplication.

The implementation should prefer over-splitting to over-merging. It is easier for
a reviewer to merge two clusters than to notice two different products hidden
inside one cluster.

## Review Sheets

Contact sheets are part of the core product, not a nice-to-have.

They should show:

- Predicted clusters with numbered images.
- Reference clusters with numbered images.
- Top disagreement cases.
- Possible duplicate pairs.
- Possible detail-crop relationships.

Each sheet should be readable in a chat or browser without opening 300 files one
by one.

## Success Criteria

V0 is successful if:

- It inventories the three-folder dataset and explains what images exist where.
- It runs on the current roughly 300-image batch.
- It produces predicted clusters without manual intervention.
- It compares those clusters against the existing reference clusters.
- It surfaces disagreements in a way that can be reviewed quickly.
- It avoids aggressive same-design merges.
- It gives enough evidence to tune thresholds for the next run.

Quantitative target for the first useful version:

- Pairwise precision above 0.90 against the reference clusters.
- Pairwise recall above 0.75 against the reference clusters.
- All merge errors listed explicitly for review.

The precision target is intentionally higher than recall because wrong merges are
more damaging than extra splits.

## Future Versions

V1: Add review corrections and save an approved clustering.

V2: Compare approved clusters to existing Airtable catalog images.

V3: Package the visual resolver for OpenClaw through the transport that fits the
server architecture best.

## Open Questions

- Are raw original images and clustered images the same files, or were copies
  renamed during clustering?
- Are fixed images visually corrected versions of the same files, or can they be
  cropped/retouched enough that filename/hash matching may fail?
- Should detail crops remain inside the same product cluster, or be represented as
  linked assets outside the main cluster?
- Are product variants such as different stone colors always different products?
- Should earrings, necklaces, rings, and bracelets be clustered together in one
  run or separated by category first when category is available?

## Recommended Next Step

Implement a local evaluator with this command shape:

```bash
jewelry-cluster-benchmark \
  --fixed input/fixed_unclustered \
  --unfixed input/unfixed \
  --reference input/clustered_reference \
  --out results
```

The first stage may also be exposed separately:

```bash
jewelry-cluster-benchmark normalize \
  --fixed input/fixed_unclustered \
  --unfixed input/unfixed \
  --reference input/clustered_reference \
  --out data/normalized
```

The normalized output becomes the generic input to the clusterer:

```bash
jewelry-cluster-benchmark cluster \
  --manifest data/normalized/manifest.csv \
  --out results
```

Minimal reference-only form:

```bash
jewelry-cluster-benchmark \
  --reference input/reference_clusters \
  --out results
```

The implementation should start as local code with JSON and contact-sheet outputs.
Only after the benchmark result is useful should we decide whether OpenClaw calls
it through CLI, HTTP, MCP, or another interface.
