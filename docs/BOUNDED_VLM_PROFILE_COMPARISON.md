# Bounded VLM Profile Comparison — OpenAI vs Gemini

External VLM detector experiment on the same bounded 56-image catalog dev subset. This is offline/read-only: no detector DB writes, no Shopify/Drive/Airtable writes, no hidden holdout evaluation.

## Inputs

- Manifest: `workbench/vlm-profile-openai-bounded/manifest.json`
- Images: 56 catalog dev images
- Evidence views per provider: 224 each
  - 56 `full_image`
  - 56 `vlm_context`
  - 56 `owlv2_padded`
  - 56 `owlv2_context`

## Profile summaries

| Provider | Profiles | Scene summary | Policy summary | Person | Hand |
|---|---:|---|---|---:|---:|
| OpenAI `gpt-4.1-mini` | 56 | 30 model/lifestyle, 24 clean product, 2 multi-item | 29 full+crop, 27 full-only | 30 | 0 |
| Gemini `gemini-2.5-flash-lite` | 56 | 8 model/lifestyle, 10 macro detail, 37 clean product, 1 multi-item | 34 full+crop, 22 full-only | 30 | 0 |

Gemini ran through Google Generative Language API using Stav service-account OAuth, not a printed API key.

## Contact-sheet QA

Artifacts:

- `workbench/vlm-profile-openai-bounded/openai_bounded_crop_contact_sheet.jpg`
- `workbench/vlm-profile-gemini-bounded/gemini_bounded_crop_contact_sheet.jpg`

Findings:

- Both providers produce many visually plausible earring/necklace crops and are good enough for **offline inactive embedding evaluation**.
- OpenAI is more likely to produce tight ear/skin crops that sometimes clip or miss tiny jewelry detail.
- Gemini is a bit more stable on the bounded set, especially on lifestyle/context framing, but still includes broad face/neck/background context in multiple rows.
- Product/studio rows generally crop the product correctly for both providers.
- Neither provider is safe for direct bulk activation from profile boxes alone.

## SigLIP retrieval smoke on generated evidence

Command shape:

```bash
uv run --with torch --with transformers --with pillow --with numpy --with scikit-learn \
  python tools/jewelry_cluster_benchmark.py multi-view-retrieve \
  --evidence <provider>/evidence/evidence_views.json \
  --profiles <provider>/image_profiles.json \
  --out <provider>/retrieval \
  --provider siglip \
  --device cpu \
  --top-k 20 \
  --offline-model-cache
```

Provider comparison using catalog ID only as eval truth:

| Provider | Queries | Top-1 | Top-3 | Top-5 | Safe autos at 0.96/0.12 |
|---|---:|---:|---:|---:|---:|
| OpenAI VLM views | 56 | 14.29% | 17.86% | 25.00% | 0 |
| Gemini VLM views | 56 | 14.29% | 19.64% | 23.21% | 0 |

This retrieval harness is not the active DB readback path; it is a bounded smoke over VLM-generated evidence only. The result says VLM boxes alone are not a deployable recall improvement.

## Decision

Do not deploy VLM crop activation from this result.

Keep:

- Gemini service-account VLM path.
- OpenAI/Gemini profile artifacts.
- Contact-sheet QA artifacts.
- Retrieval smoke outputs for diagnostics.

Next acceptable path:

1. Use VLM profiles as routing/QA hints, not as exact identity truth.
2. If continuing, compare VLM crops against the existing deterministic crop rows inside the active-policy readback harness.
3. Require objective improvement over `jewelry-siglip-live-crop-safe-v1` while preserving zero wrong auto matches.
