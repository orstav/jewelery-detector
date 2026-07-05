# Active Policy Retrieval Evaluation

Read-only evaluation of the active detector DB policy on catalog dev products.

## Inputs

- shot role: `live`
- top_k: `50`
- writes detector DB: `false`
- uses filename tokens for matching: `false`

## Split

- total products: 154
- dev products: 139
- hidden products: 15
- evaluated probes: 325
- shot roles: `{"live": 325}`

## Metrics

| Approach | Probes | Top-1 | Top-3 | Top-5 | Δ Top-1 vs full/studio | Δ Top-5 vs full/studio | Missing correct |
|---|---:|---:|---:|---:|---:|---:|---:|
| `runtime_live_gate` | 325 | 31.08% | 80.92% | 85.23% | 3.38% | 8.31% | 11 |
| `force_live` | 325 | 31.08% | 80.92% | 85.23% | 3.38% | 8.31% | 11 |
| `force_studio` | 325 | 27.69% | 72.62% | 76.92% | 0.00% | 0.00% | 16 |

## Policy modes

```json
{
  "force_live:live_additive_crop": 325,
  "force_studio:studio_full_only": 325,
  "runtime_live_gate:live_additive_crop": 325
}
```
