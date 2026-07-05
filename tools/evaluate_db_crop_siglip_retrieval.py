#!/usr/bin/env python3
"""Generate offline crop SigLIP embeddings and compare against full-image retrieval.

Read-only with respect to detector Postgres: it reads catalog image rows, materializes
profile-style crop views under workbench, embeds those view JPEGs, and evaluates
product-level retrieval on a dev product split. Hidden products remain excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_db_embedding_retrieval import connect, hidden_products
from tools.jewelry_cluster_benchmark import (
    build_embedding_provider,
    crop_image,
    foreground_product_box,
    generate_evidence_views,
    image_size,
    make_thumbnail,
    sha256,
    write_json,
)

Json = dict[str, Any]


def vector_dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def norm_vector(vector: list[float]) -> list[float]:
    mag = math.sqrt(sum(v * v for v in vector))
    return [v / mag for v in vector] if mag else vector


def read_images(url: str) -> list[Json]:
    with connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT image_id, product_id, source_uri, sha256, width, height, status
            FROM product_images
            WHERE product_id IS NOT NULL AND status = 'ready'
            ORDER BY product_id, image_id
            """
        )
        rows = []
        for image_id, product_id, source_uri, source_hash, width, height, status in cur.fetchall():
            path = Path(str(source_uri))
            if not path.exists():
                continue
            rows.append(
                {
                    "image_id": str(image_id),
                    "product_id": str(product_id),
                    "source_path": str(path),
                    "filename": path.name,
                    "sha256": str(source_hash or sha256(path)),
                    "width": int(width or 0),
                    "height": int(height or 0),
                    "status": str(status),
                }
            )
        return rows


def select_dev_rows(rows: list[Json], *, hidden_ratio: float, seed: int, max_products: int, max_images_per_product: int) -> tuple[list[Json], Json]:
    product_ids = sorted({str(row["product_id"]) for row in rows})
    hidden = hidden_products(product_ids, hidden_ratio, seed)
    by_product: dict[str, list[Json]] = defaultdict(list)
    for row in rows:
        pid = str(row["product_id"])
        if pid not in hidden:
            by_product[pid].append(row)
    eligible_products = [pid for pid, items in sorted(by_product.items()) if len(items) >= 2]
    if max_products > 0:
        eligible_products = eligible_products[:max_products]
    selected = []
    for pid in eligible_products:
        selected.extend(by_product[pid][:max_images_per_product])
    return selected, {
        "seed": seed,
        "hidden_ratio": hidden_ratio,
        "total_products": len(product_ids),
        "dev_products": len(set(product_ids) - hidden),
        "hidden_products": len(hidden),
        "hidden_products_sha256": hashlib.sha256("\n".join(sorted(hidden)).encode()).hexdigest(),
        "hidden_evaluated": False,
        "selected_products": len(eligible_products),
        "selected_images": len(selected),
    }


def center_box(width: int, height: int, scale: float) -> tuple[int, int, int, int]:
    w = max(1, int(width * scale))
    h = max(1, int(height * scale))
    return ((width - w) // 2, (height - h) // 2, w, h)


def heuristic_profile(record: Json) -> Json:
    path = Path(str(record["source_path"]))
    width = int(record.get("width") or 0)
    height = int(record.get("height") or 0)
    if not width or not height:
        width, height = image_size(path)
        width = width or 1
        height = height or 1
    fg = foreground_product_box(path, width, height)
    flags: list[str] = ["offline_heuristic_profile"]
    if fg and fg.get("box"):
        raw_box = [int(v) for v in fg["box"]]
        box = tuple(raw_box)  # type: ignore[assignment]
        source = "foreground_product_box"
        area = float(fg.get("box_area_ratio", 0.0))
        if area > 0.72:
            flags.append("foreground_box_large")
        if area < 0.03:
            flags.append("foreground_box_tiny")
    else:
        box = center_box(width, height, 0.70)
        source = "center70_fallback"
        flags.append("missing_foreground_box")
    return {
        "image_id": str(record["image_id"]),
        "image_width": width,
        "image_height": height,
        "scene_type": "model_lifestyle",
        "has_hand": True,
        "has_person": False,
        "background_type": "offline_unknown",
        "jewelry_items": [
            {
                "type": "jewelry",
                "dominance": "medium",
                "object_completeness": "uncertain",
                "box": list(box),
                "confidence": 0.70,
                "identity_features": [source],
            }
        ],
        "quality_flags": flags,
        "recommended_evidence_policy": "crop_heavy",
    }


def materialize_views(rows: list[Json], out_dir: Path, include_center_views: bool) -> list[Json]:
    manifest = []
    profiles = {}
    for row in rows:
        path = Path(str(row["source_path"]))
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
        if not width or not height:
            width, height = image_size(path)
        manifest.append(
            {
                "image_id": row["image_id"],
                "source_path": str(path),
                "filename": row["filename"],
                "width": int(width or 1),
                "height": int(height or 1),
                "sha256": row["sha256"],
                "status": "ready",
                "product_id": row["product_id"],
            }
        )
        profiles[str(row["image_id"])] = heuristic_profile(manifest[-1])
    views = generate_evidence_views(manifest, profiles, out_dir / "evidence", detector="profile")
    product_by_image = {str(row["image_id"]): str(row["product_id"]) for row in rows}
    for view in views:
        view["product_id"] = product_by_image.get(str(view["image_id"]))
    if include_center_views:
        center_dir = out_dir / "evidence" / "views"
        for row in manifest:
            source = Path(str(row["source_path"]))
            width = int(row["width"])
            height = int(row["height"])
            for view_type, scale in (("center70", 0.70), ("center50", 0.50)):
                box = center_box(width, height, scale)
                path = center_dir / f"{row['image_id']}_{view_type}.jpg"
                if crop_image(source, path, box):
                    views.append(
                        {
                            "view_id": f"{row['image_id']}_{view_type}",
                            "image_id": row["image_id"],
                            "product_id": row["product_id"],
                            "view_type": view_type,
                            "source": "center",
                            "box": list(box),
                            "view_path": str(path),
                            "risk_flags": ["offline_center_crop"],
                            "usable_for_retrieval": True,
                        }
                    )
    write_json(out_dir / "manifest.json", manifest)
    write_json(out_dir / "heuristic_profiles.json", list(profiles.values()))
    write_json(out_dir / "evidence_views.json", views)
    return views


def load_cache(path: Path) -> dict[str, Json]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def embed_views(views: list[Json], out_dir: Path, args: argparse.Namespace) -> dict[str, list[float]]:
    provider_args = argparse.Namespace(
        provider=args.provider,
        model_id=args.model_id,
        device=args.device,
        image_size=args.image_size,
        offline_model_cache=args.offline_model_cache,
        dinov2_model="facebook/dinov2-base",
    )
    provider = build_embedding_provider(provider_args)
    cache_path = out_dir / "crop_siglip_embedding_cache.json"
    cache = load_cache(cache_path)
    vectors: dict[str, list[float]] = {}
    for index, view in enumerate([v for v in views if v.get("usable_for_retrieval", True)], start=1):
        view_path = Path(str(view["view_path"]))
        key = "|".join([provider.provider_id, str(view["view_id"]), sha256(view_path)])
        if key not in cache:
            print(f"Embedding {index}/{len(views)} {view['view_id']}", flush=True)
            cache[key] = {
                "provider": provider.provider_id,
                "view_id": view["view_id"],
                "view_type": view["view_type"],
                "image_sha256": sha256(view_path),
                "vector": provider.embed(view_path),
            }
            if index % 25 == 0:
                write_json(cache_path, cache)
        vectors[str(view["view_id"])] = [float(v) for v in cache[key]["vector"]]
    write_json(cache_path, cache)
    return vectors


def aggregate_product(rows: list[Json]) -> list[Json]:
    grouped: dict[str, list[Json]] = defaultdict(list)
    for row in rows:
        grouped[str(row["product_id"])].append(row)
    candidates = []
    for pid, items in grouped.items():
        sims = sorted([float(item["similarity"]) for item in items], reverse=True)
        best = sims[0]
        mean_top3 = sum(sims[:3]) / min(3, len(sims))
        evidence = len(sims)
        score = 0.70 * best + 0.25 * mean_top3 + 0.05 * min(1.0, evidence / 6)
        candidates.append({"product_id": pid, "score": score, "best_similarity": best, "mean_top3_similarity": mean_top3, "evidence_count": evidence})
    ranked = sorted(candidates, key=lambda item: float(item["score"]), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def evaluate_approach(name: str, views: list[Json], vectors: dict[str, list[float]], allowed_types: set[str], top_k: int) -> Json:
    usable = [v for v in views if str(v["view_type"]) in allowed_types and str(v["view_id"]) in vectors]
    by_image: dict[str, list[Json]] = defaultdict(list)
    by_product: dict[str, set[str]] = defaultdict(set)
    for view in usable:
        by_image[str(view["image_id"])].append(view)
        by_product[str(view["product_id"])].add(str(view["image_id"]))
    probes = [image_id for image_id, image_views in sorted(by_image.items()) if len(by_product[str(image_views[0]["product_id"])]) >= 2]
    probe_rows = []
    for image_id in probes:
        query_views = by_image[image_id]
        truth = str(query_views[0]["product_id"])
        scored = []
        for qv in query_views:
            qvec = vectors[str(qv["view_id"])]
            for cv in usable:
                if str(cv["image_id"]) == image_id:
                    continue
                scored.append(
                    {
                        "product_id": str(cv["product_id"]),
                        "candidate_image_id": str(cv["image_id"]),
                        "candidate_view_id": str(cv["view_id"]),
                        "query_view_id": str(qv["view_id"]),
                        "similarity": vector_dot(qvec, vectors[str(cv["view_id"])]),
                    }
                )
        raw = sorted(scored, key=lambda item: float(item["similarity"]), reverse=True)[:top_k]
        ranked = aggregate_product(raw)
        truth_rank = next((int(item["rank"]) for item in ranked if item["product_id"] == truth), None)
        top = ranked[0] if ranked else None
        probe_rows.append(
            {
                "query_image_id": image_id,
                "truth_product_id": truth,
                "rank": truth_rank,
                "top_product_id": top.get("product_id") if top else None,
                "top_score": top.get("score") if top else None,
                "top_candidates": ranked[:5],
            }
        )
    evaluated = len(probe_rows)
    return {
        "approach": name,
        "view_types": sorted(allowed_types),
        "evaluated_probes": evaluated,
        "top1": sum(1 for row in probe_rows if row["rank"] == 1),
        "top1_accuracy": sum(1 for row in probe_rows if row["rank"] == 1) / evaluated if evaluated else 0.0,
        "top3": sum(1 for row in probe_rows if row["rank"] is not None and row["rank"] <= 3),
        "top3_recall": sum(1 for row in probe_rows if row["rank"] is not None and row["rank"] <= 3) / evaluated if evaluated else 0.0,
        "top5": sum(1 for row in probe_rows if row["rank"] is not None and row["rank"] <= 5),
        "top5_recall": sum(1 for row in probe_rows if row["rank"] is not None and row["rank"] <= 5) / evaluated if evaluated else 0.0,
        "missing_correct_candidate": sum(1 for row in probe_rows if row["rank"] is None),
        "examples": [row for row in probe_rows if row["rank"] != 1][:10],
    }


def render_contact_sheet(path: Path, report: Json, views: list[Json]) -> None:
    out_dir = path.parent
    thumbs = out_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    by_image: dict[str, list[Json]] = defaultdict(list)
    for view in views:
        by_image[str(view["image_id"])].append(view)
    cards = []
    for image_id, image_views in list(sorted(by_image.items()))[:30]:
        figs = []
        for view in sorted(image_views, key=lambda v: str(v["view_type"])):
            src = Path(str(view["view_path"]))
            dst = thumbs / f"{view['view_id']}.jpg"
            if src.exists() and not dst.exists():
                make_thumbnail(src, dst)
            if dst.exists():
                figs.append(
                    "<figure>"
                    f"<img src='{html.escape('thumbs/' + dst.name)}' alt=''>"
                    f"<figcaption>{html.escape(str(view['view_type']))}<br>{html.escape(str(view.get('source')))}<br>{html.escape(str(view.get('box')))}</figcaption>"
                    "</figure>"
                )
        cards.append(f"<section><h2>{html.escape(image_id)}</h2><div>{''.join(figs)}</div></section>")
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Crop SigLIP evaluation views</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4}"
        "section{background:white;border:1px solid #ddd;border-radius:8px;margin:0 0 18px;padding:14px}div{display:flex;flex-wrap:wrap;gap:10px}"
        "figure{margin:0;width:220px;border:1px solid #eee;padding:6px;background:#fafafa}img{width:220px;height:220px;object-fit:contain;background:#eee}figcaption{font-size:12px}</style>"
        "</head><body><h1>Crop SigLIP evaluation views</h1>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def markdown_report(report: Json, html_path: Path) -> str:
    lines = [
        "# Offline crop SigLIP retrieval evaluation",
        "",
        "Date: 2026-07-05",
        "Branch: `raw-intake-embedding-consensus`",
        "",
        "## Scope",
        "",
        "Read-only offline experiment. It does not write to detector Postgres or production data. Hidden product holdout remains excluded.",
        "",
        "## Split",
        "",
        "```text",
        *[f"{k}: {v}" for k, v in report["split"].items()],
        "```",
        "",
        "## Results",
        "",
        "| Approach | Views | Probes | Top-1 | Top-3 | Top-5 | Missing |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for result in report["results"]:
        lines.append(
            f"| {result['approach']} | {', '.join(result['view_types'])} | {result['evaluated_probes']} | "
            f"{result['top1_accuracy']:.2%} | {result['top3_recall']:.2%} | {result['top5_recall']:.2%} | {result['missing_correct_candidate']} |"
        )
    best = max(report["results"], key=lambda item: float(item["top1_accuracy"])) if report["results"] else None
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            "This bounded dev run proves the missing crop path is now executable and measurable offline. It is not a full production gate: only a subset of dev products was evaluated, and the generated profiles are heuristic, not persisted VLM profiles.",
            "",
            f"Best Top-1 in this run: `{best['approach']}` at {float(best['top1_accuracy']):.2%}." if best else "No results.",
            "",
            "The result is promising enough to scale the experiment and improve crop/profile generation before touching live detector DB rows.",
            "",
            "## View counts",
            "",
            "```text",
            *[f"{k}: {v}" for k, v in sorted(report["view_counts"].items())],
            "```",
            "",
            "## Contact sheet",
            "",
            "```text",
            str(html_path),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-products", type=int, default=60)
    parser.add_argument("--max-images-per-product", type=int, default=4)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--provider", default="siglip")
    parser.add_argument("--model-id", default="google/siglip-base-patch16-224")
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--offline-model-cache", action="store_true")
    parser.add_argument("--include-center-views", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = read_images(args.database_url)
    selected, split = select_dev_rows(
        rows,
        hidden_ratio=args.hidden_ratio,
        seed=args.seed,
        max_products=args.max_products,
        max_images_per_product=args.max_images_per_product,
    )
    views = materialize_views(selected, out_dir, args.include_center_views)
    vectors = embed_views(views, out_dir, args)
    approaches = {
        "full_image_only": {"full_image"},
        "vlm_context_only": {"vlm_context"},
        "owlv2_padded_only": {"owlv2_padded"},
        "owlv2_context_only": {"owlv2_context"},
        "profile_crop_views_only": {"vlm_context", "owlv2_padded", "owlv2_context"},
        "all_profile_views": {"full_image", "vlm_context", "owlv2_padded", "owlv2_context"},
    }
    if args.include_center_views:
        approaches["center_views_only"] = {"center70", "center50"}
        approaches["all_views_with_centers"] = {"full_image", "vlm_context", "owlv2_padded", "owlv2_context", "center70", "center50"}
    results = [evaluate_approach(name, views, vectors, types, args.top_k) for name, types in approaches.items()]
    report = {
        "schema_version": "1.0",
        "inputs": {
            "read_only_db": True,
            "uses_filename_tokens": False,
            "uses_product_id_as_evaluation_label_only": True,
            "provider": args.provider,
            "model_id": args.model_id,
            "max_products": args.max_products,
            "max_images_per_product": args.max_images_per_product,
            "top_k": args.top_k,
        },
        "split": split,
        "view_counts": dict(sorted(defaultdict(int, ((k, sum(1 for v in views if v["view_type"] == k)) for k in {str(v["view_type"]) for v in views})).items())),
        "results": results,
    }
    report_path = out_dir / "crop_siglip_retrieval_eval.json"
    html_path = out_dir / "crop_siglip_contact_sheet.html"
    md_path = Path("docs/RAW_INTAKE_DB_CROP_SIGLIP_EVAL.md")
    write_json(report_path, report)
    render_contact_sheet(html_path, report, views)
    md_path.write_text(markdown_report(report, html_path), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(report_path),
                "markdown": str(md_path),
                "contact_sheet": str(html_path),
                "selected_images": split["selected_images"],
                "results": [
                    {"approach": r["approach"], "top1": r["top1_accuracy"], "top3": r["top3_recall"], "top5": r["top5_recall"]}
                    for r in results
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
