# OpenAI VLM Profile Smoke

Small external VLM smoke after Or confirmed OpenAI/Gemini keys are available. This run used OpenAI only; Gemini key was not found in the expected environment/secret files during this pass.

## Scope

- Input: 12 catalog lifestyle/model images from the existing local catalog cache.
- Model: `gpt-4.1-mini` via OpenAI vision API.
- Output artifacts:
  - `workbench/vlm-profile-openai-smoke/manifest.json`
  - `workbench/vlm-profile-openai-smoke/openai/image_profiles.json`
  - `workbench/vlm-profile-openai-smoke/evidence/evidence_views.json`
  - `workbench/vlm-profile-openai-smoke/vlm_crop_contact_sheet.jpg`
- No detector DB writes.
- No Shopify/Drive/Airtable writes.

## Result

OpenAI profile generation succeeded:

```text
profiles: 12
scene_counts: model_lifestyle=12
recommended_evidence_policy: full_plus_crop=12
has_person: 12
has_hand: 0
```

Evidence generation from profiles succeeded:

```text
views: 48
full_image: 12
vlm_context: 12
owlv2_padded: 12
owlv2_context: 12
```

## Visual QA

The contact sheet shows mixed quality:

- `vlm_context` / `owlv2_context` often includes the jewelry plus enough face/ear context, especially for earrings.
- `owlv2_padded` is frequently too tight or shifted onto skin/face and sometimes misses the jewelry or only includes a tiny edge of it.
- Some lifestyle images are usable for earring crops, but several VLM boxes are not reliable enough to bulk-embed blindly.

## Decision

Do not activate VLM-generated crop rows directly from this smoke. Next useful step is a bounded VLM-crop embedding eval:

1. Generate profiles for a larger but capped dev subset.
2. Build crop views from VLM boxes.
3. Embed as a new inactive version, e.g. `jewelry-vlm-crop-v1`.
4. Read back/evaluate against active policy gates.
5. Only consider activation if live recall improves and zero-wrong safe threshold remains intact.

