# Crop DB Readback Validation

Read-only validation against staged inactive `jewelry-crop-v1` DB rows.

## Inputs

- shot role: `studio_or_product`
- preprocess version: `jewelry-crop-v1`
- hidden evaluated: `false`
- writes detector DB: `false`

## Split

- products: 61
- images: 145
- probes: 120

## Metrics

| Approach | Top-1 | Top-5 | Δ Top-1 | Δ Top-5 | Missing correct |
|---|---:|---:|---:|---:|---:|
| `db_full_only` | 55.00% | 91.67% | 0.00% | 0.00% | 0 |
| `additive_max_all` | 54.17% | 96.67% | -0.83% | 5.00% | 0 |
| `hybrid_full_crop_max` | 54.17% | 95.83% | -0.83% | 4.17% | 0 |
| `crop_center_same_view` | 53.33% | 96.67% | -1.67% | 5.00% | 0 |
| `hybrid_full_center` | 53.33% | 95.83% | -1.67% | 4.17% | 0 |
| `additive_same_view_max` | 52.50% | 96.67% | -2.50% | 5.00% | 0 |
