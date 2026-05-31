# Project Guidance

## Jewelry Matching Strategy

Treat catalog labels as benchmark truth, not production inputs.

- Fields such as `final_product_ids`, `identity_eligible`, `media_role`, and
  `clustering_policy` may be used to measure precision/recall and build review
  sets.
- Do not design production matching logic that depends on those labels being
  present. Production should work from pixels plus any weak metadata that is
  actually available.
- When reporting experiments, separate the algorithm inputs from the evaluation
  labels.

Full-frame model/lifestyle images are poor product-identity candidates.

- The jewelry is often tiny relative to the frame.
- Full-image embeddings tend to match pose, hand/model, background, lighting,
  and composition before they match the jewelry.
- Current evidence from the catalog SigLIP run:
  - `model_or_lifestyle` assets: 160
  - model assets with a same-product identity anchor: 102
  - top-1 same-product identity retrieval: 3/102
  - top-5 same-product identity retrieval: 13/102
  - model-model pairs at similarity >= 0.92: 179 total, 31 same-product, 148
    different-product

Use localization before matching small jewelry in model images.

- Detect or segment the jewelry object first.
- Crop around the jewelry with enough padding to preserve context.
- Embed the crop, not the full lifestyle frame.
- For uncertain cases, use multiple crops/scales and take the best candidate
  match before AI adjudication.

Likely crop targets:

- Rings: hand/finger region or ring crop.
- Earrings: ear/side-face region or earring crop.
- Necklaces: neck/chest/pendant crop.
- Product shots: object mask or tight bounding crop around the jewelry.

Do not rely on all-pairs comparison as the production path.

- Pairwise scoring is useful for small benchmarks and diagnostics.
- Production should use retrieval:
  `image -> jewelry crop(s) -> crop embeddings -> ANN top-K retrieval -> rerank/adjudicate -> product match or no match`.
- Candidate storage can be FAISS, HNSW, pgvector, Qdrant, or another ANN index.
- Use the stronger vision adjudicator only on retrieved candidates, not every
  possible pair.

Preferred benchmark sequence:

1. Use the tagged catalog only as an evaluation harness.
2. Compare full-frame embeddings against cropped-jewelry embeddings.
3. Measure top-K same-product recall and false candidate rate.
4. Run AI adjudication on the candidate queue only after candidate generation is
   good enough.
5. Attach supporting/shared/model media after product identity is resolved,
   unless a crop-based method proves those images can produce reliable identity
   evidence.
