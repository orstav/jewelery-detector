# Mobile Clustering App Import/Export Contract

Status: Phase 0 draft. Offline/staging only.

## Import package

Detector/raw-intake exports a versioned folder or zip:

```text
stav-clustering-import-YYYYMMDD-HHMM/
  manifest.json
  photos.jsonl
  catalog_product_cards.jsonl
  candidate_suggestions.jsonl
  image_cluster_suggestions.jsonl
```

### manifest.json

```json
{
  "schema_version": "stav-mobile-clustering-import-v1",
  "created_at": "2026-07-06T12:00:00Z",
  "source": "jewelery-detector/offline-export",
  "production_writes": false,
  "hidden_evaluated": false,
  "detector_policy": "jewelry-siglip-live-crop-safe-v1",
  "notes": "Top candidates are distinct product/cluster suggestions, not duplicate image slots."
}
```

### photos.jsonl

One line per photo available to the app.

```json
{"id":"photo_raw_001","source_kind":"raw","source_ref":"drive/raw/2026-07/foo.jpg","thumbnail_url":"https://.../thumb.jpg","full_image_url":"https://.../full.jpg","shot_role":"live","status":"unreviewed"}
```

### catalog_product_cards.jsonl

Cards for existing catalog products that may appear as candidate options.

```json
{"catalog_product_id":"R123","display_name_he":"עגילי ...","design_id":"D045","thumbnail_urls":["https://.../1.jpg","https://.../2.jpg"],"product_type_he":"עגילים","status":"existing_catalog_product"}
```

### candidate_suggestions.jsonl

Distinct product/cluster candidates, already grouped by product.

```json
{"photo_id":"photo_raw_001","candidate_type":"existing_product","candidate_ref_id":"R123","rank":1,"score":0.972,"margin":0.041,"supporting_photo_ids":["cat_img_1","cat_img_2"],"explanation_he":"נמצאו 2 תמונות דומות של אותו מוצר","generated_by":"active-policy-top10-v1"}
```

### image_cluster_suggestions.jsonl

Near-duplicate image groups proposed by detector.

```json
{"image_cluster_id":"img_cluster_001","representative_photo_id":"photo_raw_001","photo_ids":["photo_raw_001","photo_raw_002","photo_raw_003"],"confidence":0.94,"reason_he":"תמונות מאוד דומות מאותה זווית"}
```

## Export package

The mobile app exports decisions and cluster state. These become labels/evidence for offline raw-intake and detector improvement, not direct production writes.

```text
stav-clustering-export-YYYYMMDD-HHMM/
  manifest.json
  decisions.jsonl
  product_clusters.jsonl
  product_cluster_photos.jsonl
  unresolved_review_queue.jsonl
```

### export manifest.json

```json
{
  "schema_version": "stav-mobile-clustering-export-v1",
  "created_at": "2026-07-06T13:20:00Z",
  "app_environment": "vercel-staging",
  "production_writes": false,
  "reviewed_photo_count": 42,
  "new_product_cluster_count": 7,
  "existing_product_links": 12,
  "not_sure_count": 3
}
```

### decisions.jsonl

Append-only log.

```json
{"id":"decision_001","actor":"dalia","photo_id":"photo_raw_001","decision_type":"same_product","target_product_cluster_id":"cluster_007","target_catalog_product_id":null,"created_at":"2026-07-06T13:01:00Z","payload":{"source":"mobile_review","button_he":"אותו מוצר"}}
```

### product_clusters.jsonl

Current reconstructed cluster state.

```json
{"id":"cluster_007","cluster_type":"new_product","catalog_product_id":null,"design_id":null,"human_label":"עגילים חדשים - זמני","status":"active","linked_photo_count":3,"created_by":"dalia"}
```

Existing product link example:

```json
{"id":"cluster_existing_R123","cluster_type":"existing_catalog_product","catalog_product_id":"R123","design_id":"D045","human_label":"R123","status":"active","linked_photo_count":2,"created_by":"system"}
```

Same-design sibling example:

```json
{"id":"cluster_009","cluster_type":"same_design_sibling","catalog_product_id":null,"design_id":"D045","human_label":"מוצר חדש באותו עיצוב כמו R123","status":"active","linked_photo_count":4,"created_by":"eyal"}
```

### product_cluster_photos.jsonl

```json
{"product_cluster_id":"cluster_007","photo_id":"photo_raw_001","role":"primary","decision_id":"decision_001","confirmed_by":"dalia","created_at":"2026-07-06T13:01:00Z"}
```

### unresolved_review_queue.jsonl

```json
{"photo_id":"photo_raw_044","reason":"not_sure","last_actor":"eyal","candidate_refs":["R123","R124","cluster_007"],"notes":"נראה דומה אבל לא בטוח אם אותו מוצר"}
```

## Contract rules

1. Suggestions must be distinct product/cluster options, not duplicate image options.
2. Every user action creates a decision row.
3. Exported decisions do not directly mutate Shopify/Airtable/Drive.
4. `same_design_different_product` is separate from `same_product`.
5. `new_product` is always available because not all products exist yet.
6. Hidden/unseen detector labels must not be included unless explicitly approved.
7. Image URLs should be signed or public staging URLs with no local/LAN dependency.
