# Mobile Clustering Minimal-Intent Flow

Status: offline design/build note; no production writes.
Date: 2026-07-06

## Problem Or identified

A pure photo-by-photo flow is too much work. Even if the system remembers previous decisions, Dalia/Eyal would still need to repeatedly inspect similar photos and make small decisions. The app must let them open the phone and finish quickly.

## UX principle

Do not ask them to classify every photo.

Ask them to approve or fix **pre-built groups**:

```text
detector proposes a batch/group -> Dalia/Eyal give one minimal intention -> system applies it to many photos
```

The app should only fall back to single-photo review when the group is ambiguous.

## Exact intention needed from Dalia/Eyal

For each proposed group, the system should ask only one of these intentions:

1. **כן, זו קבוצה אחת** — all shown photos are the same sellable jewelry/product.
2. **לחבר למוצר קיים** — all/selected photos belong to an existing catalog product or already-created work cluster.
3. **זה מוצר חדש** — all/selected photos are one new sellable product.
4. **לפצל** — not all photos belong together; select which stay together.
5. **אותו עיצוב, מוצר אחר** — visually same family/design, but separate sellable product.
6. **לא בטוח** — send this group to Or/HAL review.

That is the whole main vocabulary. No variant/SKU/Shopify/product-model language in the parent UI.

## Design / model / variant resolution

Or identified that `אותו עיצוב, מוצר אחר` alone does not solve the hard modeling question. The app should use a hybrid of both options:

1. **short explanation** before the question;
2. **one simple difference question** only when the group is visually close but not exact.

Do not ask Dalia/Eyal:

```text
האם זה וריאנט / מוצר / Design?
```

Ask:

```text
מה ההבדל שרואים?
```

Buttons:

```text
אין הבדל — אותו תכשיט
צבע מתכת אחר
כסף / זהב
אבן / צבע אבן אחר
גודל אחר
צורה / פרטים שונים
לא בטוח
```

Backend mapping:

```text
אין הבדל                 -> same_product
צבע מתכת אחר            -> possible offering/variant inside same gold product, review by rules
כסף / זהב                -> usually separate Product under same Design
אבן / צבע אבן אחר        -> usually separate Product under same Design
גודל אחר                 -> same Design; may be size offering or separate Product depending evidence
צורה / פרטים שונים       -> likely different Design or Or review
לא בטוח                  -> review queue
```

This keeps the parent UI simple but gives the backend enough signal to avoid corrupting Product/Variant/Design structure.

## Primary flow: group-first, not photo-first

### Step 1 — Queue shows proposed groups

Mobile home screen:

```text
קבוצות לבדיקה

[Group A] 8 תמונות · נראה מוצר אחד · ביטחון גבוה
[אישור מהיר] [לפתוח]

[Group B] 4 תמונות · דומה למוצר R123
[לחבר] [לפתוח]

[Group C] 6 תמונות · ייתכן שיש פה 2 מוצרים
[לפתוח ולפצל]
```

This lets them finish many photos with one tap.

### Step 2 — Group review screen

Show all photos in the group as large enough thumbnails, plus the best product/cluster candidates.

Primary buttons:

```text
כן, זו קבוצה אחת
לחבר למוצר קיים
זה מוצר חדש
לפצל
לא בטוח
```

If detector confidence is high, the primary action is pre-selected, but still human-approved.

### Step 3 — Split mode only when needed

When they tap `לפצל`, show selectable thumbnails:

```text
בחרי את התמונות ששייכות יחד
[photo] [photo] [photo]
[צור קבוצה מהנבחרות]
```

The unselected photos return to the queue as a smaller unresolved group. This avoids asking about every image individually.

### Step 4 — Existing product / existing work linking

If the group looks like a known product or a cluster already created earlier, show:

```text
נראה שזה שייך ל:
[Product/Cluster card]
כבר קושרו: 3 תמונות

[לחבר את כל הקבוצה]
[לבחור רק חלק]
[זה מוצר אחר]
```

The important point: their previous work accumulates at group level.

## Queue ordering

The app should order work by effort saved:

1. high-confidence duplicate/image groups with one-tap approve;
2. high-confidence existing product/working-cluster links;
3. likely new product groups;
4. ambiguous split-needed groups;
5. singletons / hard cases last.

This makes the first minutes productive and quick.

## When single-photo review appears

Only for:

- groups of size 1;
- after a split leaves unresolved photos;
- when a photo conflicts with the selected group;
- when Dalia/Eyal manually opens a photo.

Single-photo review is a repair tool, not the main path.

## Detector/import requirement

The detector import must precompute group proposals, not only per-photo Top-K:

```text
review_groups.jsonl
- group_external_id
- representative_photo_id
- photo_external_ids[]
- proposal_type:
  same_new_product_group | link_existing_product | link_working_cluster | same_design_sibling | split_likely | singleton
- confidence_bucket: high | medium | low
- recommended_action_he
- candidate_product_refs[]
- evidence_summary_he
```

The app renders these groups as the main queue.

## Data model implication

A `review_group` is a temporary work item. It can resolve into:

- one product cluster;
- an existing product cluster;
- several split groups;
- Or/HAL review queue.

Do not treat the review group as the final product entity.

## Metrics for pilot

The MVP should measure:

- photos resolved per minute;
- groups resolved per minute;
- average taps per resolved photo;
- split rate;
- undo rate;
- not-sure rate;
- percent resolved without single-photo fallback.

Target for “works very well” UX:

```text
>= 80% of photos resolved through group-level actions
<= 2 taps per resolved photo on average
low not-sure/undo rate after first pilot corrections
```

## UI wording

Use simple Hebrew:

```text
קבוצה אחת
לחבר למוצר קיים
מוצר חדש
לפצל
לא בטוח
עוד מוצרים
עוד תמונות דומות
```

Avoid:

```text
variant
SKU
Shopify
catalog entity
metafield
embedding
threshold
```
