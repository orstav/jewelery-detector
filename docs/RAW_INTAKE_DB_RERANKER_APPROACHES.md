# DB reranker approach comparison

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Read-only comparison of 10 production-realistic reranking formulas over the existing detector DB candidate generator:

```text
stored image embeddings -> pgvector Top-K -> product candidate aggregation -> deterministic rerank formulas
```

No filename tokens, catalog filenames, or probe catalog IDs are used as matching inputs. Product IDs are used only for evaluation truth/split labels.

## Tool

Added:

```text
tools/evaluate_db_reranker_approaches.py
```

The tool tries exactly 10 deterministic reranking approaches:

1. current engine score
2. best similarity only
3. top-3 mean only
4. best + evidence bonus
5. mean + query coverage
6. balanced consensus
7. singleton-spike penalty
8. asset consensus
9. dense-family penalty window=2
10. consistency + density

## Split

```text
total_products: 154
dev_products: 139
hidden_products: 15
hidden_evaluated: false
evaluated_probes: 995
```

Hidden set hash:

```text
3d2651f436f999121ec9496fb557cdaacfbb9ee303f03579fe264ad332f61f06
```

## Important DB finding

The detector DB currently contains only full-image embeddings:

```text
view_type: full_image = 1139
crop_source: cached_full_image = 1139
risk_flags: none
```

So these 10 reranking attempts are limited to full-frame SigLIP evidence. They do not test real jewelry-localized crops yet.

## Results

No approach passed the "very good" gate.

Gate used by the tool:

```text
Top-1 >= 75%
auto precision >= 95%
correct auto recall >= 40%
auto wrong <= 0
```

Top approaches from the dev run:

| Approach | Top-1 | Top-3 | Top-5 | Auto precision | Correct auto recall | Auto wrong |
|---|---:|---:|---:|---:|---:|---:|
| 10_consistency_and_density | 42.91% | 83.52% | 88.04% | 87.14% | 6.13% | 9 |
| 05_mean_plus_query_coverage | 43.22% | 85.73% | 89.25% | 73.15% | 26.83% | 98 |
| 03_top3_mean_only | 43.22% | 85.73% | 89.25% | 70.45% | 21.81% | 91 |
| 01_current_engine_score | 44.82% | 85.53% | 89.15% | 67.91% | 29.35% | 138 |
| 04_best_plus_evidence_bonus | 42.71% | 83.92% | 87.54% | 68.31% | 36.18% | 167 |

## Interpretation

This result is negative but useful:

- deterministic reranking formulas over current full-frame embeddings do not solve the problem;
- Top-5 stays high enough that the retriever remains useful as a shortlist generator;
- Top-1 does not improve meaningfully because the stored evidence is full-image only;
- threshold/policy tuning can trade precision and recall, but cannot create a "very good" tool from this evidence.

The next concrete improvement should be adding jewelry-localized crop embeddings, then rerunning this same evaluator against full-image vs crop vs multi-crop evidence.

## Verification command shape

```bash
python3 -m py_compile tools/evaluate_db_reranker_approaches.py tools/evaluate_db_embedding_retrieval.py
PYTHONPATH=. uv run --with 'psycopg[binary]' \
  python tools/evaluate_db_reranker_approaches.py \
  --database-url "$DATABASE_URL" \
  --output workbench/raw-intake-embedding-consensus/db_reranker_approaches.json \
  --top-k 80
```
