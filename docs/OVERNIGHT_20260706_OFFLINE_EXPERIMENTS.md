# Overnight Offline Experiments — 2026-07-06

Branch: `raw-intake-embedding-consensus`

This pass was read-only/offline. The cron environment did not have `DATABASE_URL`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`, so no DB-backed active-policy rebuild and no external VLM profile generation were attempted. Existing candidate/result caches under `workbench/` were reused. No WhatsApp, Shopify, Airtable, Drive, or detector DB writes were made.

## Active-policy reranker grid check

Existing completed outputs under `workbench/active-policy-reranker-grid/` were parsed.

| Split | Probes | Current Top-1 | Best Top-1 formula | Best Top-1 | Δ Top-1 | Best Top-5 delta | Deployable candidates |
|---|---:|---:|---|---:|---:|---:|---:|
| live | 325 | 31.08% | `grid_best0.50_mean0.50_bonus0.012_penalty0.00` | 31.38% | +0.31pp | +0.92pp | 0 |
| studio | 663 | 53.39% | `grid_best0.80_mean0.20_bonus0.000_penalty0.00` | 53.70% | +0.30pp | +0.15pp | 0 |
| all | 995 | 45.93% | `grid_best0.80_mean0.20_bonus0.000_penalty0.00` | 46.13% | +0.20pp | +0.30pp | 0 |

Conclusion: score-only reranking remains below deploy threshold; no runtime change.

## Pair-judge multi-gate replay on 300-probe cache

Command:

```bash
PYTHONPATH=. uv run python tools/evaluate_pair_judge_multi_gate_sweep.py \
  --candidate-cache workbench/pair-judge-reranker/candidate_cache.json \
  --output workbench/overnight-20260706/pair_judge_multi_gate_sweep_default.json \
  --limit 50
```

Output summary:

```text
probe_count: 300
safe_like_count: 0
zero_wrong_count: 3312
best_zero_wrong:
  approach: 02_best_similarity
  auto_correct: 10
  auto_wrong: 0
  auto_precision: 100.00%
  correct_auto_recall: 3.33%
  min_proxy_score: 0.90
  min_proxy_margin: 0.15
  split: live 6 correct / 0 wrong; studio 4 correct / 0 wrong
```

This is still far below the active deployed consensus-v2 policy's documented 88/995 correct autos, 0 wrong, 8.84% correct-auto recall.

## Pair-judge multi-gate replay on full raw Top-K cache

Existing output parsed: `workbench/overnight-20260706/raw_pair_judge_multi_gate_sweep.json`.

```text
probe_count: 1997
safe_like_count: 0
best_zero_wrong:
  approach: 01_current_score
  auto_correct: 10
  auto_wrong: 0
  auto_precision: 100.00%
  correct_auto_recall: 0.50%
  min_proxy_score: 0.99
  min_proxy_margin: 0.15
```

Conclusion: conservative deterministic pair-judge gates are not deployable.

## Conditional live/studio blend cache analysis

Existing outputs parsed:

- `workbench/overnight-20260706/conditional_blend_raw_cache.json`
- `workbench/overnight-20260706/conditional_blend_active_sample.json`

Raw 1997-probe cache best result versus current constants:

```text
baseline current constants: top1 44.32%, top5 83.07%, missing 103
best conditional formula: conditional:live_b0.35_bon0.012_pen0.00|studio_b0.65_bon0.000_pen0.00
top1 44.82%, top5 83.43%
delta: +0.50pp top1, +0.35pp top5
split deltas: live +0.40pp top1 / +0.10pp top5; studio +0.60pp top1 / +0.60pp top5
```

Active 300-probe sample best result versus current constants:

```text
baseline current constants: top1 59.00%, top5 87.00%, missing 17
best conditional formula: conditional:live_b0.65_bon0.018_pen0.00|studio_b0.65_bon0.000_pen0.00
top1 59.33%, top5 87.33%
delta: +0.33pp top1, +0.33pp top5
```

Conclusion: conditional split-specific scoring shows only tiny offline gains and is not deployable without a DB-backed active-policy threshold/preflight run.

## Safety decision

No code or DB policy was deployed. The active baseline remains `jewelry-siglip-live-crop-v1` / consensus-v2 code path as previously deployed. Next useful work requires a DB-backed run or real VLM profile generation credentials; absent those, deterministic cached rerank tweaks are exhausted for now.
