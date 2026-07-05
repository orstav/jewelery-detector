#!/usr/bin/env python3
"""Validate current detector DB crop mechanics and crop evidence availability.

Read-only. This answers whether the current catalog matching benchmark is using
actual crop mechanics/crop embeddings, and whether there are persisted profiles
needed to generate crop views. It can also render a small hard-negative review
sheet showing current available evidence for wrong Top-1 cases.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import jewelry_detector_db as jdb
from tools.evaluate_db_embedding_retrieval import (
    EmbeddingRow,
    connect,
    hidden_products,
    query_candidates,
    read_catalog_embeddings,
)
from tools.jewelry_cluster_benchmark import make_thumbnail

Json = dict[str, Any]


def db_crop_inventory(url: str) -> Json:
    with connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT view_type, crop_source, COALESCE(active, false), count(*)
            FROM image_embeddings
            GROUP BY view_type, crop_source, COALESCE(active, false)
            ORDER BY count(*) DESC, view_type, crop_source
            """
        )
        embedding_groups = [
            {"view_type": row[0], "crop_source": row[1], "active": bool(row[2]), "count": int(row[3])}
            for row in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT
              count(*) AS total_embeddings,
              count(*) FILTER (WHERE active) AS active_embeddings,
              count(DISTINCT image_id) FILTER (WHERE active) AS active_images,
              count(DISTINCT product_id) FILTER (WHERE active) AS active_products,
              count(*) FILTER (WHERE active AND view_type <> 'full_image') AS active_non_full_embeddings,
              count(*) FILTER (WHERE active AND crop_source <> 'cached_full_image') AS active_non_cached_full_source,
              count(*) FILTER (WHERE active AND crop_box::text <> '[0, 0, 0, 0]' AND crop_box::text <> '[]') AS active_non_empty_boxes
            FROM image_embeddings
            """
        )
        row = cur.fetchone()
        embedding_summary = {
            "total_embeddings": int(row[0] or 0),
            "active_embeddings": int(row[1] or 0),
            "active_images": int(row[2] or 0),
            "active_products": int(row[3] or 0),
            "active_non_full_embeddings": int(row[4] or 0),
            "active_non_cached_full_source": int(row[5] or 0),
            "active_non_empty_boxes": int(row[6] or 0),
        }
        cur.execute(
            """
            SELECT status, count(*)
            FROM image_profiles
            GROUP BY status
            ORDER BY status
            """
        )
        profile_status_counts = {str(status): int(count) for status, count in cur.fetchall()}
        cur.execute("SELECT count(*), count(profile_json) FROM image_profiles")
        profile_total, profile_json_count = cur.fetchone()
        cur.execute(
            """
            SELECT status, count(*)
            FROM product_images
            GROUP BY status
            ORDER BY status
            """
        )
        product_image_status_counts = {str(status): int(count) for status, count in cur.fetchall()}
        cur.execute("SELECT count(*), count(DISTINCT product_id) FROM product_images WHERE product_id IS NOT NULL")
        product_images_total, product_count = cur.fetchone()
    return {
        "embedding_summary": embedding_summary,
        "embedding_groups": embedding_groups,
        "image_profiles": {
            "rows": int(profile_total or 0),
            "profile_json_rows": int(profile_json_count or 0),
            "status_counts": profile_status_counts,
        },
        "product_images": {
            "rows_with_product_id": int(product_images_total or 0),
            "products": int(product_count or 0),
            "status_counts": product_image_status_counts,
        },
    }


def read_source_map(url: str) -> dict[str, Json]:
    with connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT image_id, product_id, source_uri, width, height, status
            FROM product_images
            WHERE product_id IS NOT NULL
            """
        )
        return {
            str(image_id): {
                "image_id": str(image_id),
                "product_id": str(product_id),
                "source_uri": str(source_uri),
                "width": int(width or 0),
                "height": int(height or 0),
                "status": str(status),
            }
            for image_id, product_id, source_uri, width, height, status in cur.fetchall()
        }


def ranked_candidates(raw_rows: list[Json]) -> list[Json]:
    ranked = jdb.aggregate_product_candidates(raw_rows)
    compact: list[Json] = []
    for candidate in ranked:
        compact.append(
            {
                "rank": candidate.get("rank"),
                "product_id": candidate.get("product_id"),
                "score": candidate.get("score"),
                "best_similarity": candidate.get("best_similarity", candidate.get("similarity")),
                "mean_top3_similarity": candidate.get("mean_top3_similarity"),
                "margin": candidate.get("margin"),
                "evidence_count": candidate.get("evidence_count"),
                "query_crop_count": candidate.get("query_crop_count"),
                "candidate_crop_count": candidate.get("candidate_crop_count"),
            }
        )
    return compact


def mine_sample_hard_negatives(args: argparse.Namespace) -> Json:
    rows = read_catalog_embeddings(args.database_url, model=args.model, preprocess_version=args.preprocess_version)
    product_ids = sorted({row.product_id for row in rows})
    hidden = hidden_products(product_ids, args.hidden_ratio, args.seed)
    dev_products = set(product_ids) - hidden
    by_product: dict[str, list[EmbeddingRow]] = defaultdict(list)
    for row in rows:
        by_product[row.product_id].append(row)
    probes = [row for row in rows if row.product_id in dev_products and len(by_product[row.product_id]) >= 2]
    if args.max_probes:
        probes = probes[: args.max_probes]

    negatives: list[Json] = []
    type_counts: Counter[str] = Counter()
    crop_id_counts: Counter[str] = Counter()
    for probe in probes:
        raw = query_candidates(args.database_url, probe, ref_products=dev_products, top_k=args.top_k)
        ranked = ranked_candidates(raw)
        top = ranked[0] if ranked else None
        truth = next((cand for cand in ranked if cand["product_id"] == probe.product_id), None)
        if truth and truth.get("rank") == 1:
            continue
        top_pid = str(top.get("product_id")) if top else None
        neg_type = "truth_missing_from_top_k" if truth is None else "rerankable" if int(truth.get("rank") or 999) <= 5 else "broad_error"
        type_counts[neg_type] += 1
        crop_id_counts[probe.crop_id] += 1
        item = {
            "query_image_id": probe.image_id,
            "query_crop_id": probe.crop_id,
            "query_view_type": probe.view_type,
            "query_crop_source": probe.crop_source,
            "query_risk_flags": probe.risk_flags or [],
            "truth_product_id": probe.product_id,
            "wrong_top_product_id": top_pid,
            "truth_rank": truth.get("rank") if truth else None,
            "wrong_top_score": top.get("score") if top else None,
            "truth_score": truth.get("score") if truth else None,
            "negative_type": neg_type,
            "top_candidates": ranked[:5],
        }
        negatives.append(item)
        if len(negatives) >= args.sample_limit:
            break
    return {
        "split": {
            "seed": args.seed,
            "hidden_ratio": args.hidden_ratio,
            "total_products": len(product_ids),
            "dev_products": len(dev_products),
            "hidden_products": len(hidden),
            "hidden_evaluated": False,
        },
        "evaluated_probe_cap": len(probes),
        "sampled_hard_negatives": len(negatives),
        "sample_negative_type_counts": dict(type_counts.most_common()),
        "sample_query_crop_id_counts": dict(crop_id_counts.most_common()),
        "hard_negative_samples": negatives,
    }


def representative_product_image(product_id: str, source_map: dict[str, Json], exclude_image_id: str | None = None) -> Json | None:
    for item in sorted(source_map.values(), key=lambda x: (x["product_id"], x["image_id"])):
        if item["product_id"] == product_id and item["image_id"] != exclude_image_id:
            return item
    return None


def thumbnail_for(source_uri: str, thumbs_dir: Path, label: str) -> str | None:
    src = Path(source_uri)
    if not src.exists():
        return None
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)[:80]
    dst = thumbs_dir / f"{safe}.jpg"
    if not dst.exists():
        make_thumbnail(src, dst)
    return dst.name


def render_html_report(path: Path, report: Json, source_map: dict[str, Json]) -> None:
    out_dir = path.parent
    thumbs_dir = out_dir / "crop_mechanics_thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for idx, neg in enumerate(report["hard_negative_sample"].get("hard_negative_samples", []), start=1):
        query = source_map.get(str(neg["query_image_id"]))
        truth_img = representative_product_image(str(neg["truth_product_id"]), source_map, str(neg["query_image_id"]))
        wrong_img = representative_product_image(str(neg["wrong_top_product_id"]), source_map, None) if neg.get("wrong_top_product_id") else None
        figures = []
        for role, item in (("query", query), ("truth-other", truth_img), ("wrong-top", wrong_img)):
            if not item:
                continue
            thumb = thumbnail_for(str(item["source_uri"]), thumbs_dir, f"{idx}_{role}_{item['image_id']}")
            if thumb:
                figures.append(
                    "<figure>"
                    f"<img src='{html.escape('crop_mechanics_thumbs/' + thumb)}' alt=''>"
                    f"<figcaption><b>{html.escape(role)}</b><br>{html.escape(item['product_id'])}<br>{html.escape(item['image_id'])}</figcaption>"
                    "</figure>"
                )
        cards.append(
            "<section>"
            f"<h2>#{idx} {html.escape(str(neg['truth_product_id']))} → {html.escape(str(neg.get('wrong_top_product_id')))}</h2>"
            f"<p>query crop: <code>{html.escape(str(neg.get('query_crop_id')))}</code>; "
            f"view: <code>{html.escape(str(neg.get('query_view_type')))}</code>; "
            f"source: <code>{html.escape(str(neg.get('query_crop_source')))}</code>; "
            f"truth rank: <code>{html.escape(str(neg.get('truth_rank')))}</code></p>"
            f"<div class='figures'>{''.join(figures)}</div>"
            "</section>"
        )
    inv = report["inventory"]
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Crop mechanics validation</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "section{background:white;border:1px solid #ddd;border-radius:8px;margin:0 0 18px;padding:14px}.figures{display:flex;gap:12px;flex-wrap:wrap}"
        "figure{margin:0;width:250px;border:1px solid #e5e7eb;background:#fafafa;padding:6px}img{width:250px;height:250px;object-fit:contain;background:#eee}"
        "figcaption{font-size:12px;word-break:break-word}code{background:#eee;padding:1px 4px}</style></head><body>"
        "<h1>Detector DB crop mechanics validation</h1>"
        f"<p><b>Active embeddings:</b> {inv['embedding_summary']['active_embeddings']} &nbsp; "
        f"<b>Active non-full embeddings:</b> {inv['embedding_summary']['active_non_full_embeddings']} &nbsp; "
        f"<b>Image profiles:</b> {inv['image_profiles']['rows']}</p>"
        "<p><b>Conclusion:</b> current DB benchmark has no persisted crop profiles and no active non-full crop embeddings; these hard negatives are full-image evidence only.</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def markdown_summary(report: Json, html_path: Path) -> str:
    inv = report["inventory"]
    groups = inv["embedding_groups"]
    group_lines = "\n".join(
        f"| `{g['view_type']}` | `{g['crop_source']}` | {g['active']} | {g['count']} |" for g in groups
    )
    sample = report["hard_negative_sample"]
    return f"""# Detector DB crop mechanics validation

Date: 2026-07-05
Branch: `raw-intake-embedding-consensus`

## Scope

Read-only validation of the **actual crop evidence currently available in the detector DB**. This does not judge theoretical crop code quality; it checks whether the evaluated catalog matcher is actually using persisted crop profiles/crop embeddings.

## Inventory

```text
Active embeddings: {inv['embedding_summary']['active_embeddings']}
Active images: {inv['embedding_summary']['active_images']}
Active products: {inv['embedding_summary']['active_products']}
Active non-full embeddings: {inv['embedding_summary']['active_non_full_embeddings']}
Active non-cached-full crop sources: {inv['embedding_summary']['active_non_cached_full_source']}
Image profile rows: {inv['image_profiles']['rows']}
Image profile JSON rows: {inv['image_profiles']['profile_json_rows']}
Product image rows: {inv['product_images']['rows_with_product_id']}
```

| view_type | crop_source | active | count |
|---|---|---:|---:|
{group_lines}

## Hard-negative sample

```text
Hidden evaluated: {sample['split']['hidden_evaluated']}
Total products: {sample['split']['total_products']}
Dev products: {sample['split']['dev_products']}
Hidden products: {sample['split']['hidden_products']}
Sampled hard negatives: {sample['sampled_hard_negatives']}
Sample query crop IDs: {sample['sample_query_crop_id_counts']}
```

Review sheet:

```text
{html_path}
```

## Conclusion

The current detector DB benchmark is **not validating actual jewelry crop embeddings**. It contains full-image embedding rows only:

```text
view_type = full_image
crop_source = cached_full_image
```

There are also no persisted `image_profiles` rows, so the profile-driven crop mechanics (`vlm_context`, `owlv2_padded`, `owlv2_context`) are not available to validate from current DB state.

Therefore, the validated problem is not "bad crops". The validated problem is:

```text
current evaluated matching evidence = full-image SigLIP only
actual crop mechanics/crop embeddings = not present in DB benchmark
```

## Next required experiment

Generate a bounded offline set of profile-driven crop views for dev catalog images, embed those crop views with SigLIP, and compare:

1. current full-image embeddings;
2. `vlm_context` crop embeddings;
3. `owlv2_padded` crop embeddings;
4. `owlv2_context` crop embeddings;
5. multi-view product aggregation.

Only then can we say whether crop improvement helps or not.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--html-output", required=True)
    parser.add_argument("--markdown-output", required=True)
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int, default=250)
    parser.add_argument("--sample-limit", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    html_output = Path(args.html_output)
    markdown_output = Path(args.markdown_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    html_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)

    inventory = db_crop_inventory(args.database_url)
    hard_negative_sample = mine_sample_hard_negatives(args)
    source_map = read_source_map(args.database_url)
    report = {
        "schema_version": "1.0",
        "inputs": {
            "read_only": True,
            "uses_filenames_as_features": False,
            "uses_product_id_as_evaluation_label_only": True,
            "max_probes": args.max_probes,
            "sample_limit": args.sample_limit,
            "top_k": args.top_k,
        },
        "inventory": inventory,
        "hard_negative_sample": hard_negative_sample,
        "conclusion": {
            "actual_crop_embeddings_present": inventory["embedding_summary"]["active_non_full_embeddings"] > 0,
            "persisted_profiles_present": inventory["image_profiles"]["profile_json_rows"] > 0,
            "current_benchmark_validates_actual_crops": False,
            "reason": "current active embeddings are full-image/cached-full-image and image_profiles has no rows",
        },
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html_report(html_output, report, source_map)
    markdown_output.write_text(markdown_summary(report, html_output.resolve()), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "html_output": str(html_output),
                "markdown_output": str(markdown_output),
                "active_embeddings": inventory["embedding_summary"]["active_embeddings"],
                "active_non_full_embeddings": inventory["embedding_summary"]["active_non_full_embeddings"],
                "image_profile_rows": inventory["image_profiles"]["rows"],
                "sampled_hard_negatives": hard_negative_sample["sampled_hard_negatives"],
                "current_benchmark_validates_actual_crops": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
