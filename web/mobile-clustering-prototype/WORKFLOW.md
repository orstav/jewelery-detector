# Dalia Batch Workflow

## Goal

Reach 100% source-photo accountability without asking Dalia to process derivative files as if they were separate products.

The all-batch builder currently accounts for **555/555** indexed source assets. The generated counts are authoritative; rerun `npm run fixture` after source SQLite changes.

## What gets ingested

### Dalia identity batches

Only unresolved `web` assets enter the mobile identity queue. Multi-photo product groups open with a clustering question; single-photo groups start with product identity.

Initial queue generated from the current SQLite:

| Priority | Batch | Review cards | Photos |
|---:|---|---:|---:|
| 1 | 2025-03-19/web | 41 | 41 |
| 2 | 2026-06-29/web | 8 | 8 |
| 3 | 2026-06-22/web | 8 | 8 |
| 4 | 2026-06-15/web | 8 | 8 |
| 5 | 2026-06-08/web | 11 | 11 |
| 6 | 2026-05-10/web | 4 | 4 |
| 7 | 2026-04-27/web | 2 | 2 |
| 8 | 2026-04-20/web | 11 | 11 |
| 9 | 2026-01-18/web | 12 | 12 |
| 10 | 2026-01-08/web | 22 | 22 |
| 11 | 2025-12-30/web | 2 | 2 |
| 12 | 2025-12-22/web | 5 | 5 |
| 13 | 2025-12-18/web | 15 | 15 |
| 14 | 2025-09-18/web | 2 | 2 |
| 15 | 2025-08-14/web | 2 | 2 |
| 16 | 2025-03-24/web | 2 | 2 |
| 17 | 2025-02-23/web | 3 | 3 |
| 18 | 2025-02-02/web | 4 | 4 |

Total: **162 photos in 18 review batches**.

### Automatically routed assets

- `published_active_validated` and `dead_or_merged`: terminal; do not ask Dalia again.
- existing downstream statuses such as `ready_for_package`, `draft_created`, parent-fact capture, and proven existing-product checks: keep in the existing downstream lane.
- `print`, `png`, and `fix`: support/version assets. Link to the same-date `web` source where deterministic; otherwise keep in `support_mapping_pending`.

The support mapping queue is real work but **not** a product-identity question for Dalia. HAL should resolve it using source filename/date/hash/visual evidence and only ask a human when deterministic mapping fails.

## Operating loop

### 1. Prepare everything at once

```bash
cd web/mobile-clustering-prototype
npm run fixture
npm run check
```

This regenerates all batches, source coverage evidence, catalog thumbnails, and the deployed static data artifact. It performs no live catalog writes.

### 2. Dalia reviews one batch

1. Open the production link.
2. Use the highlighted batch first.
3. Work one card at a time.
4. Choose only what is visually known.
5. Use `לא בטוחה` instead of guessing.
6. Save; work persists on the shared backend.
7. At completion, move to the next batch link.

### 3. HAL collects all sessions

```bash
npm run workflow:status
```

The collector reads every expected batch session and writes `qa/batch-session-status.json`. It classifies each batch as:

- `not_started`
- `in_progress`
- `complete_validated`
- `invalid_session`
- backend error/unreachable

A complete batch is exported only if packet structure and source-photo coverage validate.

### 4. HAL routes validated packets

For each `complete_validated` batch:

- existing product images → verify exact Airtable product, link/create Drive folder as needed, then prepare image operation;
- new product identity → create/lock the temporary product identity, then use the existing Dalia/Eyal WhatsApp missing-data flow;
- same design/different visible facts → same Design, different Product, preserving Stav material/stone rules;
- uncertain/manual target → existing human follow-up lane with source image attached;
- duplicate/not relevant → preserve evidence and route through the existing non-destructive review gate.

No Shopify publish, destructive merge, deletion, bulk mutation, or ambiguous duplicate action occurs without the existing approval gates.

### 5. Support/version mapping loop

Run after each identity batch or as a separate HAL batch:

1. Match `print/png/fix` to canonical `web` source by date + normalized filename.
2. Strengthen with exact hash/dimensions/visual evidence when names are insufficient.
3. Record the canonical product/photo relationship.
4. Keep unmatched assets in `support_mapping_pending` with evidence; do not drop them and do not create new status families.
5. Coverage is complete only when the pending support count reaches zero or every remaining item has an explicit human question in the existing funnel.

## Definition of done

- 555/555 source assets have a lane at all times.
- 162/162 Dalia-review photos have validated decisions.
- every completed Dalia batch has a validated packet bundle.
- support/version mapping reaches zero unresolved, or every unresolved item is attached to a specific existing human-action queue.
- downstream writes pass Airtable/Drive/Shopify readback and existing safety gates.
