# AI Adjudication Proof

Goal: prove whether a vision model can improve product-clustering recall without
hurting precision.

The local embedding baseline is now used as candidate generation, not final
truth:

```text
SigLIP candidate pairs at 0.92
  -> 58 candidate pairs
  -> benchmark says 55 are same-product and 3 are different-product
```

The proof asks an AI vision model to judge only those candidate pairs.

## Run A Dry Check

```bash
. .venv/bin/activate
python tools/jewelry_cluster_benchmark.py ai-adjudicate \
  --manifest data/normalized/manifest.csv \
  --candidates results/clustering_siglip_base/candidate_pairs.json \
  --out results/ai_adjudication_openai \
  --dry-run
```

## Run A Small Paid Sample

Set the API key first:

```bash
export OPENAI_API_KEY=...
```

Then run 10 pairs:

```bash
python tools/jewelry_cluster_benchmark.py ai-adjudicate \
  --manifest data/normalized/manifest.csv \
  --candidates results/clustering_siglip_base/candidate_pairs.json \
  --out results/ai_adjudication_openai \
  --model gpt-4.1-mini \
  --max-pairs 10
```

## Run The Full Candidate Queue

```bash
python tools/jewelry_cluster_benchmark.py ai-adjudicate \
  --manifest data/normalized/manifest.csv \
  --candidates results/clustering_siglip_base/candidate_pairs.json \
  --out results/ai_adjudication_openai \
  --model gpt-4.1-mini
```

Outputs:

```text
results/ai_adjudication_openai/
  ai_decisions.json
  ai_benchmark.json
  ai_benchmark.md
  review_sheets/
    09_ai_all_decisions.html
    10_ai_false_positives.html
    11_ai_false_negatives.html
```

Success target:

```text
precision >= 0.98
recall meaningfully above 0.345
```

If AI reaches around the candidate ceiling, it can get close to:

```text
precision 0.95-1.00
recall around 0.45-0.49
```

That would prove AI is useful as pair adjudication. If it does not, we need a
different candidate generator, crops/multi-view logic, or a labeled metric
learning model.

## Result: GPT-4.1 Mini Full Candidate Queue

Run completed on 58 SigLIP candidate pairs.

Within the candidate queue:

```text
candidate pairs: 58
true positives: 53
false positives: 0
false negatives: 2
true negatives: 3
precision: 1.000
recall: 0.964
F1: 0.981
```

Across all benchmark same-product pairs:

```text
total same-product benchmark pairs: 119
AI accepted same-product pairs: 53
global precision: 1.000
global recall: 0.445
```

Interpretation:

- AI pair adjudication works well once a likely pair is shown to it.
- Precision did not drop on this run.
- Recall improved over the conservative SigLIP auto-cluster baseline, but the
  overall recall is still limited by candidate generation.
- The next problem is not AI adjudication; it is finding more candidate pairs
  without flooding the AI with unrelated pairs.

## V2 Candidate Queue Result

Added top-K neighbor candidate generation:

```text
candidate threshold: 0.92
top-K neighbors per asset: 5
candidate pairs: 313
candidate same-reference pairs: 115
candidate different-reference pairs: 198
total benchmark same-reference pairs: 119
candidate recall ceiling: 0.966
```

This is the first queue that can plausibly solve false splits. It is larger, but
still small enough for a paid AI adjudication proof.

The next AI prompt uses product/design labels:

```text
same_physical_product
same_sellable_product
same_design_variant
different_design
unsure
```

`same_design_variant` is a valid final classification, not an automatic human
review reason.

## Result: GPT-4.1 Mini V2 Candidate Queue

Run completed on all 313 V2 candidate pairs.

Within the candidate queue:

```text
candidate pairs: 313
true positives: 113
false positives: 0
false negatives: 2
true negatives: 198
same-design variants: 20
unsure or missing: 0
precision: 1.000
recall: 0.983
F1: 0.991
```

Across all benchmark same-product pairs:

```text
total same-product benchmark pairs: 119
AI accepted same-product pairs: 113
global precision: 1.000
global recall: 0.950
```

Decision distribution:

```text
same_physical_product: 112
same_sellable_product: 1
same_design_variant: 20
different_design: 180
review_required: 0
```

Interpretation:

- V2 candidate generation fixed the recall bottleneck enough for the current
  benchmark.
- AI adjudication preserved benchmark precision while recovering almost all
  same-product pairs visible to the candidate queue.
- The remaining recall loss comes from 4 true pairs never entering the candidate
  queue plus 2 candidate pairs the AI rejected.
- Reference labels are useful benchmark labels, but they are not guaranteed
  truth, so the two false negatives and any transitive contradictions should be
  reviewed visually before changing the flow.

## Correction: Fixed/Unfixed Color Edits

User review found that some pairs initially labeled `same_design_variant` are
actually the same source image with small Photoshop/fix edits. That means this
case should be deduped before product clustering, not solved by the AI pair
judge.

Prompt was updated to avoid treating metal tone or color cast alone as a
variant. After targeted rejudging of the 20 prior `same_design_variant` pairs,
the full candidate cache now has:

```text
same_physical_product: 112
same_sellable_product: 7
same_design_variant: 7
different_design: 187
```

The remaining `same_design_variant` labels involve stone/no-stone differences.
For this dataset, V1 export should use `--no-design-variants`, which treats
those as different designs and produces product-only clusters.

The corrected V3 normalization with `--edit-dedup` collapses fixed/unfixed edit
copies first:

```text
old visual assets: 101
new visual assets: 67
fixed/unfixed edit matches: 66
candidate pairs after SigLIP top-K: 215
candidate recall ceiling: 0.972
```

Future AI adjudication should run on `data/normalized_edit_dedup`, not the older
`data/normalized` set.

## Result: V3 Edit-Dedup Candidate Queue

Run completed on the corrected edit-dedup normalized set:

```text
manifest: data/normalized_edit_dedup/manifest.csv
candidates: results/clustering_siglip_v3_edit_dedup/candidate_pairs.json
output: results/ai_adjudication_v3_edit_dedup
model: gpt-4.1-mini
```

Within the candidate queue:

```text
candidate pairs: 215
true positives: 34
false positives: 1
false negatives: 1
true negatives: 179
same-design variants: 3
unsure or missing: 0
precision: 0.971
recall: 0.971
F1: 0.971
```

Across all benchmark same-product pairs:

```text
total same-product benchmark pairs: 36
AI accepted same-product pairs: 35
global precision: 0.971
global recall: 0.944
```

Decision distribution:

```text
same_physical_product: 34
same_sellable_product: 1
same_design_variant: 3
different_design: 177
review_required: 0
```

The product-only export with `--no-design-variants` is:

```text
results/ai_cluster_export_v3_edit_dedup_no_design
assets: 67
product clusters: 34
product singletons: 4
product-same edges: 35
same-design edges: 0
different-design/product edges: 180
review queue items: 1
```

The single review item is a transitive contradiction where A0028-A0038 and
A0038-A0060 are product-same, but A0028-A0060 is different-design.

## Hardening Notes

The cluster export now separates safe product clusters from review-blocked
clusters:

```text
product_clusters.json         -> approved clusters only
blocked_product_clusters.json -> contradicted/review-required clusters
product_clusters_all.json     -> diagnostics
```

On the V3 result this means:

```text
approved clusters: 33
blocked clusters: 1
```

AI cache keys now include the model, prompt version, image size, and image
hashes, so changing any of those inputs creates a new cache entry instead of
silently reusing a stale decision.
