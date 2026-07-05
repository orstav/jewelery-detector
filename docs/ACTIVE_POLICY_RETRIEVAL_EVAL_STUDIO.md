# Active Policy Retrieval Evaluation

Read-only evaluation of the active detector DB policy on catalog dev products.

## Inputs

- shot role: `studio`
- top_k: `50`
- writes detector DB: `false`
- uses filename tokens for matching: `false`

## Split

- total products: 154
- dev products: 139
- hidden products: 15
- evaluated probes: 663
- shot roles: `{"studio": 663}`

## Metrics

| Approach | Probes | Top-1 | Top-3 | Top-5 | Δ Top-1 vs full/studio | Δ Top-5 vs full/studio | Missing correct |
|---|---:|---:|---:|---:|---:|---:|---:|
| `force_live` | 663 | 54.00% | 92.76% | 94.72% | 0.60% | 0.45% | 8 |
| `runtime_live_gate` | 663 | 53.39% | 92.01% | 94.27% | 0.00% | 0.00% | 7 |
| `force_studio` | 663 | 53.39% | 92.01% | 94.27% | 0.00% | 0.00% | 7 |

## Policy modes

```json
{
  "force_live:live_additive_crop": 663,
  "force_studio:studio_full_only": 663,
  "runtime_live_gate:studio_full_only": 663
}
```
