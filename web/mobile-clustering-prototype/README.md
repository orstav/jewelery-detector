# Stav Jewelry Identity Sorter

Mobile-first identity and photo-clustering tool for Dalia.

The tool answers only the identity question:

1. Are these photos the same jewelry product?
2. Is it an existing product, a new product, or uncertain?
3. If new, is it a new design or a visible variation of an existing design?

Names, commercial copy, prices, material facts, approvals, and publishing stay in the canonical Stav workflow. The sorter does not write Airtable, Drive, Shopify, or WhatsApp.

## Production workflow

- **Prepare all source photos at once:** `npm run fixture`
- **Review one dated batch at a time:** choose a batch in the mobile queue.
- **Persist across devices:** decisions are stored through `/api/identity-session`.
- **Collect status and validated packets:** `npm run workflow:status`
- **Apply downstream only after validation:** HAL maps packets to Airtable/Drive/task lanes under existing Stav safety gates.

## Coverage invariant

Every SQLite source asset must have exactly one lane:

- `dalia_identity_review`
- `terminal_closed`
- `downstream_existing_workflow`
- `support_linked_to_web`
- `support_mapping_pending`
- `non_web_source_routed`
- `system_review_pending`

`npm run check` fails if an asset is missing, duplicated, assigned to an unknown lane, or if a review photo is missing from the built artifact.

## Local development

```bash
npm ci
npm run check
npm run dev
```

Open `http://127.0.0.1:5173`. Choose a batch from the start screen or pass a batch explicitly:

```text
http://127.0.0.1:5173/?batch=dropbox-2026-06-29-web
```

Demo cards are disabled by default and appear only with `?demo=1`.

## Operational files

- `public/batches/index.json` — Dalia batch queue
- `public/batches/coverage.json` — complete source-to-lane evidence
- `public/batches/<batch-id>/manifest.json` — per-batch source/review/auto-routed coverage
- `qa/all-batch-build-summary.json` — fixture build summary
- `qa/batch-session-status.json` — live session collection report
- `tools/hal_collect_batch_sessions.mjs` — read-only batch collector
- `../../tools/build_mobile_identity_batches.py` — all-batch fixture builder

See [WORKFLOW.md](./WORKFLOW.md) for the batch order and handoff gates.
