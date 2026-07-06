# Stav Mobile Jewelry Clustering App PRD

Status: offline planning artifact; no production detector, Shopify, Airtable, Drive, or messaging behavior changed.
Date: 2026-07-06

## 1. Goal

Build a Hebrew-first mobile web app, deployed remotely on Vercel, where Dalia and Eyal can cluster raw jewelry photos into product-level groups.

The detector should assist by suggesting candidate products/clusters, but the app's source of truth is the human decision log.

Primary workflow:

```text
detector proposes review groups -> Dalia/Eyal approve/fix groups -> product cluster assignment -> durable decision log -> export labels back into detector/raw-intake workflow
```

Important correction from Or: **photo-by-photo review is not the main UX**. It is too slow. The app should be group-first and use single-photo review only as a fallback/repair screen. See `MOBILE_CLUSTERING_MINIMAL_INTENT_FLOW.md`.

The app must handle both:

1. existing catalog products; and
2. new products that do not exist yet.

## 2. Non-goals and safety boundaries

- No production detector behavior changes from this PRD.
- No Shopify writes.
- No Airtable writes.
- No Drive mutations.
- No WhatsApp/Telegram sends to Dalia/Eyal from tests.
- No hidden/unseen detector evaluation unless explicitly approved.
- Do not use filenames, catalog IDs, or product IDs as model features. They may be used only as evaluation labels, links, and business identifiers in the UI.

## 3. Core UX thesis

Dalia/Eyal should mostly go over **proposed groups**, not individual photos. The system pre-clusters similar photos and candidate product links, then asks for the smallest possible human intention.

Bad mental model:

```text
Photo -> choose matching image
```

Correct mental model:

```text
Proposed photo group -> approve/link/split/new/unsure -> product cluster(s)
```

The UI must collapse duplicate image hits into distinct product cards. If five retrieved images belong to the same product, they consume one product option, not five options.

## 4. Entities and state model

### 4.1 Photo

A raw or catalog image available for review.

Fields:

```text
id
source_kind: raw | catalog | shopify | drive | imported
source_ref
thumbnail_url
full_image_url
shot_role: studio | live | unknown
status: unreviewed | assigned | skipped | blocked | archived
created_at
```

### 4.2 Image cluster

Near-duplicate or very-similar photos. Used to reduce redundant work.

```text
id
representative_photo_id
status: proposed | human_confirmed | split | rejected
created_by: detector | human | system
```

Join table:

```text
image_cluster_photos
- image_cluster_id
- photo_id
- confidence
- human_confirmed
- decision_id nullable
```

### 4.3 Product cluster

The unit Dalia/Eyal are actually creating/confirming.

```text
id
cluster_type: existing_catalog_product | new_product | same_design_sibling | unknown
catalog_product_id nullable
design_id nullable
human_label nullable
status: active | needs_more_photos | needs_or_review | ready_for_raw_intake | merged | archived
created_by
created_at
updated_at
```

Join table:

```text
product_cluster_photos
- product_cluster_id
- photo_id
- role: primary | supporting | duplicate | rejected
- confirmed_by
- decision_id
- created_at
```

### 4.4 Candidate suggestions

Precomputed detector output for each photo.

```text
candidate_suggestions
- id
- photo_id
- candidate_type: existing_product | working_cluster | same_design | image_cluster
- candidate_ref_id
- rank
- score
- margin
- supporting_photo_ids[]
- explanation_he: short Hebrew reason
- generated_by: detector policy/version
- created_at
```

### 4.5 Decision log

Append-only. This is the most important table.

```text
decisions
- id
- actor_id
- photo_id nullable
- source_cluster_id nullable
- target_product_cluster_id nullable
- target_catalog_product_id nullable
- decision_type
- decision_payload_json
- created_at
```

Decision types:

```text
same_product
new_product
same_design_different_product
not_same_product
not_sure
skip
merge_clusters
split_photo_from_cluster
undo_decision
needs_more_images
send_to_or_review
```

Every current state must be reconstructable from decisions.

## 5. Main user flow

### Screen 1: Group queue

Hebrew title:

```text
קבוצות לבדיקה
```

Visible parts:

1. progress: `נבדקו 42 מתוך 310`;
2. cards for proposed groups, ordered by easiest/highest-confidence first;
3. each card shows 3-8 thumbnails, evidence, and one recommended action;
4. one-tap approval for high-confidence groups.

Example group actions:

```text
אישור מהיר
לפתוח
לחבר למוצר קיים
לפצל
לא בטוח
```

### Screen 1b: Group review

Used when they open a group card. Shows all photos in the proposed group plus product/cluster candidates.

Primary buttons:

```text
כן, זו קבוצה אחת
לחבר למוצר קיים
זה מוצר חדש
לפצל
לא בטוח
```

Single-photo review is only a fallback after split/singleton/ambiguous cases.

### Screen 2: Product candidate card

Each candidate is a product/cluster, not one image.

```text
[main image]
שם/מזהה: R123 or Cluster 7
כבר קושרו: 3 תמונות
סיבה: 2 תמונות דומות + התאמה גבוהה

[לקשר לכאן]
[השוואה]
[דומה אבל לא אותו מוצר]
```

Scores stay hidden by default. They can appear under `פרטים`.

### Screen 3: Compare

Side-by-side:

```text
Current photo | candidate product/cluster images
```

Actions:

```text
אותו מוצר
אותו עיצוב, מוצר אחר
לא אותו מוצר
לא בטוח
```

### Screen 4: Existing work prompt

When a similar photo appears after a cluster already exists:

```text
נראה שזה שייך לקבוצה שכבר יצרת:
[cluster card]
כבר קושרו 2 תמונות
```

Actions:

```text
לקשר לקבוצה הזאת
לפתוח השוואה
לא, זה מוצר אחר
```

### Screen 5: Product cluster page

```text
קבוצת מוצר חדשה #7
תמונות: 4
סטטוס: צריך פרטים / מוכן לבדיקה / לא בטוח
```

Actions:

```text
הוסף תמונות
פצל תמונה
מזג עם מוצר אחר
סמן כמוכן לשלב הבא
```

## 6. Expansion rules

When Dalia/Eyal cannot find the correct option:

- `להראות עוד מוצרים` expands distinct product candidates: Top 5, then Top 10.
- `להראות עוד תמונות דומות` expands image-level neighbors, grouped by likely duplicate/image cluster.
- `מוצר חדש` creates a new working product cluster.
- `לא בטוח` sends to Or/HAL review queue.

The app must distinguish:

```text
more products != more images
same design != same product
new product != unknown product
```

## 7. Redundant-work prevention

### 7.1 Product accumulation

After a decision links a photo to a product cluster, all future candidate cards for that cluster must show updated count immediately:

```text
כבר קושרו: 3 תמונות
```

### 7.2 Near-duplicate batch prompt

If the detector thinks several photos are near-duplicates:

```text
מצאנו 4 תמונות מאוד דומות. לקשר את כולן לאותה קבוצה?
```

Buttons:

```text
כן, לקשר את כולן
לא, רק את התמונה הזאת
לפתוח ולבדוק
```

### 7.3 Undo and repair

Mistakes are expected. Required actions:

```text
בטל פעולה אחרונה
העבר תמונה לקבוצה אחרת
פצל תמונה החוצה
מזג קבוצות
```

No destructive delete in MVP. Archive only.

## 8. Architecture

Recommended stack:

```text
Vercel Next.js app
Supabase or Neon Postgres
Supabase Storage / signed image URLs / Cloudflare R2 for thumbnails and full images
server-side API routes for detector candidate import/export
Hebrew RTL UI
simple login/auth for Dalia and Eyal
```

Why not LAN/local-only:

- Dalia/Eyal need remote access.
- Vercel cannot depend on the local detector machine at runtime.
- Detector outputs should be exported/imported as jobs, not queried live from LAN.

## 9. Data sync boundary

### Input into the web app

Read-only export package from detector/raw-intake environment:

```text
photos.jsonl
candidate_suggestions.jsonl
image_cluster_suggestions.jsonl
catalog_product_cards.jsonl
thumbnail/full image URLs or upload bundle
```

### Output from the web app

Append-only decisions export:

```text
decisions.jsonl
product_clusters.jsonl
product_cluster_photos.jsonl
review_queue.jsonl
```

These outputs later feed raw-intake/canonicalization as labels and work items. They do not directly write production systems without a separate approved gate.

## 10. Detector integration

Current detector metrics, dev-evaluated set, hidden excluded:

```text
Top-3 product candidate recall: about 88%
Top-5 product candidate recall: about 92%
Top-10 product candidate recall: about 95%
Safe auto-match coverage: about 5%, with 0 wrong autos in dev gate
```

UX implication:

- default show Top-3;
- allow Top-5/Top-10 expansion;
- never force auto-match for most cases;
- use decisions as future training/evaluation labels.

## 11. MVP acceptance criteria

MVP is useful if:

1. Dalia/Eyal can review on mobile without LAN access.
2. UI is Hebrew-first and simple.
3. Most photos are resolved by group-level actions, not picture-by-picture review.
4. Every photo can be linked to an existing product cluster or a new product cluster.
5. Candidate suggestions are distinct products/clusters, not duplicate images.
6. Product cluster counts update immediately after each decision.
7. They can expand to more products/images when needed.
8. They can undo/split/merge.
9. All decisions are append-only logged.
10. Export can be consumed by the offline detector/raw-intake workflow.
11. No production systems are changed by the app without a later approval gate.
12. Pilot UX target: at least 80% of photos resolved through group-level actions, and average taps per resolved photo <= 2.

## 12. Implementation phases

### Phase 0: Offline spec and schema

- This document.
- SQL schema/migration draft.
- JSON import/export contract.
- Static Hebrew wireframe.

### Phase 1: Local clickable prototype

- mocked data;
- Hebrew mobile screens;
- no auth;
- no production integration.

### Phase 2: Remote staging on Vercel

- managed Postgres;
- auth;
- image storage/signed URLs;
- import a small safe sample;
- test with Or first.

### Phase 3: Dalia/Eyal pilot

- small batch only;
- measure completion time, confusion rate, undo/split frequency;
- collect corrections.

### Phase 4: Label feedback loop

- export human decisions;
- build eval set from decisions;
- improve detector Top-3/Top-10 and safe auto coverage offline.

## 13. Open design questions for later

Do not block MVP on these, but resolve before pilot:

1. Supabase vs Neon + separate object storage.
2. Whether Dalia and Eyal share one account or separate accounts.
3. Whether product clusters should have provisional Hebrew names during review.
4. Whether Or gets a separate admin review queue before labels enter raw-intake.
5. Image URL strategy: signed URLs vs copied thumbnails.
6. Batch size for first pilot.

## 14. Next concrete build step

Create Phase 0 artifacts:

1. SQL schema draft for the tables above.
2. JSON import/export contract examples.
3. One static Hebrew mobile wireframe page using mocked data.

After that, implement a local prototype only. No production detector or store changes.
