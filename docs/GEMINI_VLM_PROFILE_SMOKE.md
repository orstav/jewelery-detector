# Gemini VLM Profile Smoke

Small external VLM smoke after Or clarified that Gemini is available through Google API. This run used Google Generative Language API with the Stav service-account key and OAuth scope `https://www.googleapis.com/auth/generative-language`; no Gemini/API key value was printed or persisted.

## Scope

- Input: same 12 catalog lifestyle/model images used by the OpenAI VLM smoke.
- Model: `gemini-2.5-flash-lite`.
- Tool added: `tools/profile_images_gemini.py`.
- Output artifacts:
  - `workbench/vlm-profile-gemini-smoke/gemini/image_profiles.json`
  - `workbench/vlm-profile-gemini-smoke/gemini/summary.json`
  - `workbench/vlm-profile-gemini-smoke/evidence/evidence_views.json`
  - `workbench/vlm-profile-gemini-smoke/gemini_crop_contact_sheet.jpg`
- No detector DB writes.
- No Shopify/Drive/Airtable writes.

## Result

Gemini profile generation succeeded using service-account OAuth:

```text
profiles: 12
auth_mode: service_account_oauth
scene_counts:
  model_lifestyle: 3
  macro_detail: 5
  clean_product: 4
recommended_evidence_policy:
  full_plus_crop: 11
  full_only: 1
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

The Gemini crop contact sheet is more stable than the first OpenAI smoke for these lifestyle images:

- Many earring crops include the ear plus visible jewelry and are usable as contextual retrieval evidence.
- Several rows still include a lot of face/neck/background context; this may be useful for routing/live-scene detection but is not guaranteed identity evidence.
- Some full-body/side-profile rows are too broad and would dilute exact-product embeddings.

## Decision

Gemini through Google API is confirmed usable for this detector workflow, but VLM crops still require the staged gate:

1. Generate profiles for a capped dev subset.
2. Build crop evidence.
3. Embed into a separate inactive version, e.g. `jewelry-gemini-vlm-crop-v1`.
4. Read back/evaluate against active-policy gates.
5. Activate only if recall improves without breaking the current zero-wrong auto-match safety gate.

Do **not** bulk-activate VLM crop rows directly from profile boxes.
