# Dataset Inventory Notes

Generated on 2026-05-25

## Source Files

- `fix-20260525T160111Z-3-001.zip`
- `2025-03-19-20260525T155001Z-3-001.zip`
- `לקטלג-20260525T155336Z-3-001.zip`

Extracted under:

```text
data/extracted/
  fixed/
  unfixed/
  reference/
```

## Identified Roles

- `fix-20260525T160111Z-3-001.zip`: fixed images.
- `2025-03-19-20260525T155001Z-3-001.zip`: unfixed/original images.
- `לקטלג-20260525T155336Z-3-001.zip`: clustered reference.

## Counts

| Source | Image count | Breakdown |
| --- | ---: | --- |
| Fixed | 70 | 35 `print`, 35 `web` |
| Unfixed | 126 | 42 `print`, 42 `web`, 42 `png` |
| Reference | 268 | 101 `print`, 101 `web`, 66 `png` |

The reference set contains 33 cluster folders under `לקטלג`.

## Important Findings

The reference zip is not a simple folder-per-product set with one copy of each
image. It contains renditions and historical versions:

- 198 files are directly under cluster `print`, `web`, or `png` folders.
- 70 files are under nested `before fix` / `befoer fix` folders.
- The reference contains exact copies of all 70 fixed images.
- The reference contains exact copies of all 126 unfixed images.
- There are 72 additional reference-only image contents.

Filenames are not stable image identities:

- 70 basenames appear across fixed, unfixed, and reference.
- For those same basenames, fixed and unfixed often have different hashes.
- 35 parsed shot names appear in multiple reference clusters because the same
  filename can refer to different fixed/unfixed content.

Conclusion: the benchmark must reconcile by content and visual similarity, not by
filename alone.

The fixed/unfixed distinction should be modeled as version-aware deduplication:
when fixed and unfixed files represent the same visual asset, the fixed
occurrence should usually become the preferred file even though the hashes differ.
This rule belongs in the normalization layer, not in the product clustering layer.

## Reference Cluster Shape

The largest reference clusters contain 10 image files, usually because they
include multiple renditions and/or before-fix files. Smaller clusters contain 3,
6, 8, or 9 files.

This means benchmark metrics should distinguish:

- image occurrences: every file on disk
- visual assets: fixed/unfixed/png/web renditions that may represent the same shot
- product clusters: same physical jewelry product

For early implementation, image occurrences can be inventoried exactly, but
clustering quality should be interpreted carefully until visual asset
canonicalization is added.

The intended hierarchy is:

```text
image_occurrence -> visual_asset -> product_cluster
```

Where:

- `image_occurrence` is one file on disk.
- `visual_asset` groups fixed/unfixed/web/print/png versions of the same logical
  image where appropriate.
- `product_cluster` groups visual assets that show the same physical jewelry
  product.

## Implementation Implication

The first implementation step should be an inventory/reconciliation command before
any model-based clustering:

```bash
jewelry-cluster-benchmark inventory \
  --fixed data/extracted/fixed/fix \
  --unfixed data/extracted/unfixed/2025-03-19 \
  --reference data/extracted/reference/לקטלג \
  --out results
```

The inventory should produce `image_inventory.csv` and a report that identifies
same-name/different-content and before-fix cases explicitly.
