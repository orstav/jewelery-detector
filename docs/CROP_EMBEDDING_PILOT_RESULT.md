# Crop Embedding Pilot Result

Approved pilot executed as an inactive DB staging run. No crop rows were activated.

## Scope

Inputs:

- `workbench/crop-profile-dryrun-live-full/crop_profiles.jsonl`
- `workbench/crop-profile-dryrun-studio-160/crop_profiles.jsonl`

Staged versions:

- profiles: `crop-profile-v1`
- embeddings: `jewelry-crop-v1`

## DB write result

| Item | Count |
|---|---:|
| input profiles | 395 |
| inserted/updated profile rows | 343 |
| staged crop embeddings | 998 |
| active crop embeddings | 0 |
| active full-image rows after pilot | 1197 |

Embedding rows by view:

| view_type | active | rows |
|---|---:|---:|
| `center_object` | false | 395 |
| `detail_object` | false | 395 |
| `foreground_object` | false | 208 |

`image_profiles` count is lower than input profiles because the table's unique key is source-hash/model/version/max-size; duplicate source files collapse safely at the profile layer. Embeddings remain keyed by crop ID and were staged for all input profiles.

## DB readback validation

Readback used inactive `jewelry-crop-v1` rows from DB plus existing active full-image rows. Hidden holdout was not evaluated.

### Live/lifestyle

| Method | Top-1 | Top-5 | Δ Top-1 | Δ Top-5 |
|---|---:|---:|---:|---:|
| DB full only | 45.08% | 70.47% | — | — |
| additive max all | 53.37% | 85.49% | +8.29pp | +15.03pp |

Live passes the gate.

### Studio/product bounded

| Method | Top-1 | Top-5 | Δ Top-1 | Δ Top-5 |
|---|---:|---:|---:|---:|
| DB full only | 55.00% | 91.67% | — | — |
| additive max all | 54.17% | 96.67% | -0.83pp | +5.00pp |

Studio is not a clean activation pass for global additive routing: Top-5 improves, but Top-1 is slightly below the non-regression gate. Use crop evidence for live/lifestyle retrieval first; keep studio full-image-first plus human review/sibling guard.

## Safety status

- No crop embeddings active.
- Full-image rows unchanged and active.
- No Shopify/Airtable/Drive/WhatsApp writes.
- No hidden holdout evaluation.
- Tests: `75 passed`.

## Rollback

Fast rollback/deactivation remains:

```sql
UPDATE image_embeddings
SET active = false
WHERE preprocess_version = 'jewelry-crop-v1';
```

Full cleanup, only if requested and after backup:

```sql
DELETE FROM image_embeddings
WHERE preprocess_version = 'jewelry-crop-v1';

DELETE FROM image_profiles
WHERE model = 'deterministic-jewelry-cropper'
  AND prompt_version = 'crop-profile-v1';
```

## Recommendation

Do not activate globally yet. Next safe step is a live-only/read-policy activation plan:

- production matcher uses crop evidence only when shot role is live/lifestyle or unknown-live-like;
- studio/product keeps full-image-first ranking and existing human review/sibling guard;
- activation still needs separate approval.
