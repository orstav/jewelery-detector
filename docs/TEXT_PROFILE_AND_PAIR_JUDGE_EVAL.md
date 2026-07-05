# Text Profile and Pair-Judge Reranker Evaluation

Read-only offline experiments after live-only crop activation. These tools do **not** call external APIs and do **not** mutate the detector DB.

## Tools

- `tools/evaluate_db_text_profile_reranker.py`
  - Uses stored `image_profiles.profile_json` when usable.
  - Falls back to deterministic embedding-metadata proxy tokens when stored profiles have no useful profile tokens.
  - Excludes filenames, product IDs, image IDs, crop IDs, and truth labels from scoring features.
- `tools/evaluate_offline_pair_judge_rerankers.py`
  - Tests deterministic pair-judge/reranker proxies on retrieved Top-K candidate features.
  - Candidate product IDs are used only for evaluation/sibling diagnostics, not scoring.
- `tools/evaluate_pair_judge_threshold_sweep.py`
  - Reuses an existing pair-judge candidate cache and sweeps auto-match score/margin thresholds without DB access.
  - Useful when `DATABASE_URL` is unavailable; it still uses product IDs only as evaluation labels.

## Latest bounded run

Commands used a 300-probe dev slice, hidden products untouched:

```bash
PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/evaluate_db_text_profile_reranker.py \
  --database-url "$DATABASE_URL" \
  --output workbench/text-profile-reranker/text_profile.json \
  --top-k 50 \
  --max-probes 300

PYTHONPATH=. uv run --with 'psycopg[binary]' python tools/evaluate_offline_pair_judge_rerankers.py \
  --database-url "$DATABASE_URL" \
  --output workbench/pair-judge-reranker/pair_judge.json \
  --write-candidate-cache workbench/pair-judge-reranker/candidate_cache.json \
  --top-k 50 \
  --max-probes 300
```

## Results

| Experiment | Best approach | Top-1 | Top-5 | Auto precision | Correct auto recall | Wrong autos | Deploy? |
|---|---|---:|---:|---:|---:|---:|---|
| Text/profile proxy rerank | `05_balanced_embedding_text` | 63.00% | 87.67% | 79.89% | 47.67% | 36 | No |
| Pair-judge proxy rerank | `04_ambiguity_aware_pair_judge` | 60.67% | 86.00% | 73.58% | 47.33% | 51 | No |

## Interpretation

- The current stored profile layer is weak but measurable: DB has 343 `image_profiles` rows with crop-profile tokens after parsing `crops[]`; the best bounded text/profile proxy rerank reached 63.00% Top-1, but auto-match safety still fails (`36` wrong autos).
- The deterministic pair-judge idea improves Top-1 on the bounded slice, but the auto-match safety gate fails badly (`auto_wrong > 0`). It is not deployable as runtime behavior.
- These tools are still useful as harnesses for the next real experiment: generate real VLM profile JSON/text embeddings on a bounded approved dev subset, then rerun the same gates.

## Overnight 2026-07-05 no-DB threshold sweep

`DATABASE_URL`, `OPENAI_API_KEY`, and `GEMINI_API_KEY` were absent in the cron environment, so this pass did not run DB-backed active-policy grids or fabricate VLM outputs. Instead it reused the stored pair-judge candidate cache:

```bash
PYTHONPATH=. uv run python tools/evaluate_pair_judge_threshold_sweep.py \
  --candidate-cache workbench/pair-judge-reranker/candidate_cache.json \
  --output workbench/overnight-20260705/pair_judge_threshold_sweep_tool.json \
  --auto-scores 0.80,0.81,0.82,0.83,0.84,0.85,0.86,0.87,0.88,0.89,0.90,0.91,0.92,0.93,0.94,0.95,0.96,0.97,0.98,0.99,1.00 \
  --auto-margins 0,0.01,0.02,0.03,0.04,0.05,0.06,0.08,0.10,0.12,0.15,0.20
```

Result on the 300-probe cache: the best zero-wrong candidate was `02_best_similarity` at score `0.80` and margin `0.15`, with `10/300` correct autos, `0` wrong autos, `100%` auto precision, and `3.33%` correct-auto recall (`6` live, `4` studio). This is far below the active deployed consensus-v2 policy's documented `88/995` correct autos / `0` wrong autos / `8.84%` correct-auto recall, so it is not deployable.

## Safety conclusion

No runtime detector code or DB policy change should be deployed from these experiments yet.
