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

## Safety conclusion

No runtime detector code or DB policy change should be deployed from these two experiments yet.
