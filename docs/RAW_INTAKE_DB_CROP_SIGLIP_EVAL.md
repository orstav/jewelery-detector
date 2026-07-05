# Offline crop SigLIP retrieval evaluation

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Read-only offline experiment. It does not write to detector Postgres or production data. Hidden product holdout remains excluded.

## Split

```text
seed: 704
hidden_ratio: 0.1
total_products: 154
dev_products: 139
hidden_products: 15
hidden_products_sha256: 3d2651f436f999121ec9496fb557cdaacfbb9ee303f03579fe264ad332f61f06
hidden_evaluated: False
selected_products: 15
selected_images: 42
```

## Results

| Approach | Views | Probes | Top-1 | Top-3 | Top-5 | Missing |
|---|---|---:|---:|---:|---:|---:|
| full_image_only | full_image | 42 | 61.90% | 76.19% | 83.33% | 0 |
| vlm_context_only | vlm_context | 42 | 61.90% | 78.57% | 83.33% | 0 |
| owlv2_padded_only | owlv2_padded | 42 | 61.90% | 78.57% | 83.33% | 0 |
| owlv2_context_only | owlv2_context | 42 | 61.90% | 78.57% | 83.33% | 0 |
| profile_crop_views_only | owlv2_context, owlv2_padded, vlm_context | 42 | 61.90% | 80.95% | 85.71% | 2 |
| all_profile_views | full_image, owlv2_context, owlv2_padded, vlm_context | 42 | 61.90% | 80.95% | 85.71% | 5 |
| center_views_only | center50, center70 | 42 | 66.67% | 92.86% | 92.86% | 0 |
| all_views_with_centers | center50, center70, full_image, owlv2_context, owlv2_padded, vlm_context | 42 | 69.05% | 90.48% | 90.48% | 4 |

## Conclusion

This bounded dev run proves the missing crop path is now executable and measurable offline. It is not a full production gate: only a subset of dev products was evaluated, and the generated profiles are heuristic, not persisted VLM profiles.

Best Top-1 in this run: `all_views_with_centers` at 69.05%.

The result is promising enough to scale the experiment and improve crop/profile generation before touching live detector DB rows.

## View counts

```text
center50: 42
center70: 42
full_image: 42
owlv2_context: 42
owlv2_padded: 42
vlm_context: 42
```

## Contact sheet

```text
/home/server/.hermes/profiles/hermes-hal-9000/workspace/openclaw-hal-import/workspace/apps/jewelery-detector/workbench/raw-intake-embedding-consensus/crop-siglip-dev/crop_siglip_contact_sheet.html
```
