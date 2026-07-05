# Live-Only Crop Activation Plan

Approved direction: use crop evidence for live/model/unknown-live-like queries only. Keep studio/product matching full-image-first with existing human review/sibling guard.

## Why not global activation

DB readback from staged inactive `jewelry-crop-v1` rows showed:

| Split | Full-only Top-1 | Crop Top-1 | Full-only Top-5 | Crop Top-5 | Decision |
|---|---:|---:|---:|---:|---|
| live/lifestyle | 45.08% | 53.37% | 70.47% | 85.49% | pass |
| studio/product bounded | 55.00% | 54.17% | 91.67% | 96.67% | Top-1 regression |

Conclusion: crop evidence is good for live recall, but global crop routing would risk studio sibling/detail mistakes.

## Runtime behavior

`tools/jewelry_detector_db.py` now applies an effective candidate policy per incoming embedding payload:

- policy does not include `jewelry-crop-v1` → `full_only`;
- policy includes `jewelry-crop-v1` and payload is live-like → `live_additive_crop`;
- policy includes `jewelry-crop-v1` and payload is studio/legacy/unknown → `studio_full_only`.

Live-like signals from the embedding payload:

- `profile_scene_type in {model_lifestyle, multi_item}`;
- `profile_has_hand=true` or `profile_has_person=true`;
- `profile_evidence_policy=crop_heavy`.

Legacy payloads without profile metadata stay full-image-only.

## Deployment order

Do not update the active DB policy before this code is deployed, because older production code would treat a comma-separated `preprocess_version` as one literal version and could return no candidates.

Safe order:

1. Deploy/merge code that supports `effective_candidate_policy`.
2. Verify current default policy still returns full-image candidates.
3. Activate crop embedding rows, or leave them inactive until the same deploy window.
4. Add/switch to a policy whose `preprocess_version` is:

```text
jewelry-evidence-v1,jewelry-crop-v1
```

5. Smoke test one live-like payload and one studio/legacy payload with `--no-persist`.

Expected policy modes:

- live-like payload → `live_additive_crop`;
- studio/legacy payload → `studio_full_only`.

## Rollback

Immediate policy rollback:

```sql
UPDATE matching_policies
SET active = true
WHERE name = 'jewelry-siglip-v1';

UPDATE matching_policies
SET active = false
WHERE name = 'jewelry-siglip-live-crop-v1';
```

Crop row rollback/deactivation:

```sql
UPDATE image_embeddings
SET active = false
WHERE preprocess_version = 'jewelry-crop-v1';
```

## Pre-deploy read-only preflight

Use this before any production policy switch:

```bash
PYTHONPATH=tools uv run --with 'psycopg[binary]' \
  python tools/preflight_live_crop_policy.py \
  --include-inactive-crops \
  --require-live-crop-candidates
```

Expected pass conditions:

- current active policy returns candidates using only `jewelry-evidence-v1` + `full_image`;
- simulated studio future policy stays `studio_full_only` and returns no `jewelry-crop-v1` candidates;
- simulated live future policy becomes `live_additive_crop` and can see crop candidates.

Latest read-only preflight result:

```text
status: pass
current: 20 candidates, full_only, jewelry-evidence-v1/full_image only
studio simulated: 20 candidates, studio_full_only, jewelry-evidence-v1/full_image only
live simulated: 20 candidates, live_additive_crop, jewelry-evidence-v1 + jewelry-crop-v1
row counts: 1197 active full-image evidence rows; 998 inactive crop rows
```

## Current status

- Code support implemented on branch.
- Pre-deploy preflight passes.
- Tests pass.
- Staged crop rows remain inactive.
- No active DB policy was switched in this step.
