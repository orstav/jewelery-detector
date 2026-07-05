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
selected_products: 50
selected_images: 135
```

## Results

| Approach | Views | Probes | Top-1 | Top-3 | Top-5 | Missing |
|---|---|---:|---:|---:|---:|---:|
| full_image_only | full_image | 135 | 42.22% | 68.89% | 71.11% | 0 |
| vlm_context_only | vlm_context | 135 | 42.22% | 68.15% | 70.37% | 0 |
| owlv2_padded_only | owlv2_padded | 135 | 42.22% | 68.15% | 70.37% | 0 |
| owlv2_context_only | owlv2_context | 135 | 42.22% | 68.15% | 70.37% | 0 |
| profile_crop_views_only | owlv2_context, owlv2_padded, vlm_context | 135 | 43.70% | 69.63% | 71.85% | 25 |
| all_profile_views | full_image, owlv2_context, owlv2_padded, vlm_context | 135 | 43.70% | 70.37% | 71.85% | 34 |
| center_views_only | center50, center70 | 135 | 45.93% | 74.81% | 82.22% | 5 |
| all_views_with_centers | center50, center70, full_image, owlv2_context, owlv2_padded, vlm_context | 135 | 48.15% | 75.56% | 81.48% | 19 |

## Conclusion

This bounded dev run proves the missing crop path is now executable and measurable offline. It is not a full production gate: only a subset of dev products was evaluated, and the generated profiles are heuristic, not persisted VLM profiles.

Best Top-1 in this run: `all_views_with_centers` at 48.15%.

The result is promising enough to scale the experiment and improve crop/profile generation before touching live detector DB rows.

## View counts

```text
center50: 135
center70: 135
full_image: 135
owlv2_context: 135
owlv2_padded: 135
vlm_context: 135
```

## Contact sheet

```text
/home/server/.hermes/profiles/hermes-hal-9000/workspace/openclaw-hal-import/workspace/apps/jewelery-detector/workbench/raw-intake-embedding-consensus/crop-siglip-dev/crop_siglip_contact_sheet.html
```
