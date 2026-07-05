# Crop DB Readback Validation

Read-only validation against staged inactive `jewelry-crop-v1` DB rows.

## Inputs

- shot role: `live_or_lifestyle`
- preprocess version: `jewelry-crop-v1`
- hidden evaluated: `false`
- writes detector DB: `false`

## Split

- products: 87
- images: 216
- probes: 193

## Metrics

| Approach | Top-1 | Top-5 | Δ Top-1 | Δ Top-5 | Missing correct |
|---|---:|---:|---:|---:|---:|
| `additive_max_all` | 53.37% | 85.49% | 8.29% | 15.03% | 0 |
| `hybrid_full_crop_max` | 52.33% | 81.35% | 7.25% | 10.88% | 0 |
| `hybrid_full_center` | 51.81% | 78.76% | 6.74% | 8.29% | 0 |
| `crop_center_same_view` | 50.78% | 81.35% | 5.70% | 10.88% | 0 |
| `additive_same_view_max` | 47.67% | 78.24% | 2.59% | 7.77% | 0 |
| `db_full_only` | 45.08% | 70.47% | 0.00% | 0.00% | 0 |
