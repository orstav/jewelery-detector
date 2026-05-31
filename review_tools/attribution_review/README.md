# Catalog Attribution Review

Static local review tool for dataset attribution QA.

## Run

```bash
python3 -m http.server 8765
```

Open:

```text
http://localhost:8765/review_tools/attribution_review/
```

## Regenerate Queue

```bash
. .venv/bin/activate
python tools/build_attribution_review.py
```

The queue currently targets attribution-quality cases:

- multiple product IDs
- shared product folders
- multiple categories
- folder/filename ID mismatches
- missing product IDs

Decisions are saved in browser local storage. Use the export buttons to download
JSON or CSV labels.
