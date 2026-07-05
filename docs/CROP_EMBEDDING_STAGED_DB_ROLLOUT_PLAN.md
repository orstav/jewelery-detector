# Crop Embedding Staged DB Rollout Plan

Approval package for inserting crop/profile evidence into the detector DB safely. This document is a plan only; it does not authorize or perform DB writes.

## Status

- Current branch: `detector-from-production-scratch`
- Evidence commit: `05aa836 Evaluate additive crop read simulation`
- No production DB writes have been made.
- Hidden holdout remains untouched.
- The full studio background run was intentionally killed after it proved too slow; the completed studio evidence is the bounded 160-image non-regression run.

## Why this is worth staging

Read-only additive simulation from dry-run crop profiles:

| Split | Products | Images | Probes | Baseline Top-1 | Baseline Top-5 | Additive Top-1 | Additive Top-5 | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| live/lifestyle | 97 | 235 | 209 | 45.45% | 66.99% | 54.55% | 82.30% | pass: +9.09pp / +15.31pp |
| studio/product bounded | 68 | 160 | 130 | 48.46% | 91.54% | 49.23% | 96.92% | pass: no regression |

The crop layer should be staged as additive retrieval evidence, not as an auto-match policy by itself.

## Existing schema supports safe staging

Read-only schema inspection confirmed:

### `image_profiles`

| Column | Type | Nullable | Default |
|---|---|---:|---|
| `id` | bigint | no | sequence |
| `image_id` | text | no | |
| `source_sha256` | text | no | |
| `model` | text | no | |
| `prompt_version` | text | no | |
| `max_image_size` | integer | no | |
| `cache_key` | text | no | |
| `profile_json` | jsonb | no | |
| `raw_response_json` | jsonb | yes | |
| `status` | text | no | `ready` |
| `created_at` | timestamptz | no | `now()` |

### `image_embeddings`

| Column | Type | Nullable | Default |
|---|---|---:|---|
| `product_id` | text | yes | |
| `image_id` | text | no | |
| `crop_id` | text | no | |
| `view_type` | text | no | |
| `crop_box` | jsonb | no | |
| `crop_source` | text | no | |
| `risk_flags` | jsonb | no | |
| `embedding` | vector | no | |
| `embedding_model` | text | no | |
| `preprocess_version` | text | no | |
| `embedding_dim` | integer | no | |
| `source_sha256` | text | no | |
| `active` | boolean | no | `true` |

Important: `active=false` exists and should be used for first insertion.

## Staging constants

Use explicit version names so rollback is exact:

```text
profile_model: deterministic-jewelry-cropper
profile_version: crop-profile-v1
embedding_model: google/siglip-base-patch16-224
preprocess_version: jewelry-crop-v1
crop_source prefix: crop-profile-v1:<crop_id_suffix>
```

The existing full-frame rows must stay active and unchanged.

## Proposed write phases

### Phase 0 — preflight only

Run before any write:

1. Verify DB target host/port/db/user, but never print password or full connection string.
2. Count existing rows:
   - `image_profiles` by `(model, prompt_version, status)`
   - `image_embeddings` by `(preprocess_version, view_type, active)`
3. Verify no rows already exist for `preprocess_version='jewelry-crop-v1'` unless resuming an approved staged run.
4. Verify dry-run JSONL hashes match current source files.
5. Verify tests still pass.

### Phase 1 — insert profiles only

Insert one `image_profiles` row per source image/profile version.

Constraints:

- Use `ON CONFLICT`/resume logic keyed by `image_id + source_sha256 + model + prompt_version + max_image_size + cache_key` if a unique index exists; otherwise pre-read and skip exact duplicates.
- `status='ready'` only for profiles that produced preview crops and passed local JSON validation.
- Do not insert binary preview images into DB.
- Store compact crop metadata in `profile_json`; store no secrets and no large image data.

Validation after Phase 1:

```sql
SELECT model, prompt_version, status, COUNT(*)
FROM image_profiles
WHERE prompt_version = 'crop-profile-v1'
GROUP BY model, prompt_version, status;
```

### Phase 2 — insert crop embeddings inactive

Insert crop rows into `image_embeddings` with:

```text
active = false
preprocess_version = jewelry-crop-v1
```

Allowed `view_type` values:

```text
center_object
detail_object
foreground_object
```

Do not insert new `full_image` rows for this rollout.

Constraints:

- `crop_id = {image_id}:crop-profile-v1:{crop_id_suffix}`
- `crop_box` from profile JSON
- `risk_flags` from profile JSON
- `source_sha256` must equal source hash in profile JSON
- `embedding_dim` must match existing SigLIP dimension
- Insert in small transactions with row-count print before commit.
- Abort on any duplicate `(image_id, crop_id, embedding_model, preprocess_version)` unless it is an exact resume match.

Validation after Phase 2:

```sql
SELECT preprocess_version, view_type, active, COUNT(*)
FROM image_embeddings
WHERE preprocess_version = 'jewelry-crop-v1'
GROUP BY preprocess_version, view_type, active
ORDER BY view_type, active;
```

Expected first validation state:

```text
active=false only
full_image rows unchanged
```

### Phase 3 — readback simulation from DB inactive rows

Before activation, run the matcher/evaluator against inactive rows by explicitly including `preprocess_version='jewelry-crop-v1'` in the query. This must reproduce the local additive simulation within tolerance.

The runtime/query layer must support the staged policy shape before any write:

```json
{
  "preprocess_versions": ["jewelry-evidence-v1", "jewelry-crop-v1"],
  "include_inactive_embeddings": true,
  "view_types": ["full_image", "center_object", "detail_object", "foreground_object"]
}
```

The first readback validation may include inactive rows, but the default production policy must keep querying active rows only until activation is approved.

Required pass criteria:

| Split | Required |
|---|---|
| live/lifestyle | at least +5pp Top-1 and +10pp Top-5 vs same-run DB full baseline |
| studio/product | no worse than -0.5pp Top-1 and -1.0pp Top-5 vs same-run DB full baseline |
| row safety | no missing source hashes, no duplicate crop IDs, no full-image row changes |
| QA | JPEG contact sheet reviewed, no blank/broken crop previews |
| tests | `73 passed` or successor full test suite passing |

### Phase 4 — activate by version only

Activation is a single narrow DB update after explicit approval:

```sql
UPDATE image_embeddings
SET active = true
WHERE preprocess_version = 'jewelry-crop-v1'
  AND active = false;
```

Do not activate if any existing full-frame row would be modified.

### Phase 5 — production policy gate

Even after rows are active, the matcher must use a named additive policy and remain conservative:

```text
policy: additive_full_plus_crop_v1
match decision: still requires existing review/margin/sibling guards
crop rows: candidate evidence only
human review: still required for mixed/low-margin/sibling-risk cases
```

No auto-match expansion is approved by this rollout.

## Rollback

Rollback must not touch full-image rows.

### Fast rollback — deactivate crop embeddings

```sql
UPDATE image_embeddings
SET active = false
WHERE preprocess_version = 'jewelry-crop-v1';
```

### Full cleanup — delete only exact staged version

Only after backup/snapshot and if cleanup is required:

```sql
DELETE FROM image_embeddings
WHERE preprocess_version = 'jewelry-crop-v1';

DELETE FROM image_profiles
WHERE model = 'deterministic-jewelry-cropper'
  AND prompt_version = 'crop-profile-v1';
```

## Approval wording needed before writes

The required approval should be explicit, for example:

> Approved: insert crop-profile-v1 profiles and jewelry-crop-v1 embeddings into detector DB inactive only. No activation until readback report.

Separate approval is required later for activation:

> Approved: activate jewelry-crop-v1 crop embeddings after readback validation.

## Not approved by this plan

- No Shopify/Airtable/Drive/WhatsApp writes.
- No production code deploy.
- No hidden holdout evaluation.
- No deleting/replacing full-image embeddings.
- No automatic product data changes.
- No expanded auto-match coverage without a separate precision review.
