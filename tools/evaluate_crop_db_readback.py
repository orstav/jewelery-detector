#!/usr/bin/env python3
"""Validate staged inactive crop embeddings by reading them back from DB."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_category_aware_prototypes import cosine, hidden_products, parse_vector  # noqa: E402
from tools.evaluate_studio_live_split import infer_shot_role  # noqa: E402
from tools.jewelry_detector_db import connect  # noqa: E402

Json = dict[str, Any]
PREPROCESS_VERSION = "jewelry-crop-v1"


def normalize_view(row: tuple[Any, ...]) -> Json:
    crop_id = str(row[3])
    suffix = crop_id.rsplit(":", 1)[-1] if ":" in crop_id else crop_id
    if str(row[4]) == "full_image":
        suffix = "db_full"
    else:
        suffix = f"crop:{suffix}"
    return {
        "embedding_id": int(row[0]),
        "product_id": str(row[1]),
        "image_id": str(row[2]),
        "crop_id": crop_id,
        "view_type": str(row[4]),
        "preprocess_version": str(row[5]),
        "active": bool(row[6]),
        "embedding": parse_vector(str(row[7])),
        "source_uri": str(row[8]),
        "view_name": suffix,
    }


def read_views(url: str) -> list[Json]:
    with connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH pilot_images AS (
              SELECT DISTINCT image_id
              FROM image_embeddings
              WHERE preprocess_version = %s
                AND active = false
                AND product_id IS NOT NULL
            )
            SELECT e.id, e.product_id, e.image_id, e.crop_id, e.view_type,
                   e.preprocess_version, e.active, e.embedding::text, p.source_uri
            FROM image_embeddings e
            JOIN pilot_images pi ON pi.image_id = e.image_id
            JOIN product_images p ON p.image_id = e.image_id
            WHERE e.product_id IS NOT NULL
              AND e.embedding_dim = 768
              AND (
                (e.active = true AND e.view_type = 'full_image')
                OR (e.active = false AND e.preprocess_version = %s)
              )
            ORDER BY e.product_id, e.image_id, e.view_type, e.crop_id
            """,
            (PREPROCESS_VERSION, PREPROCESS_VERSION),
        )
        return [normalize_view(row) for row in cur.fetchall()]


def build_images(views: list[Json], shot_role: str, hidden_ratio: float, seed: int) -> tuple[list[Json], dict[str, dict[str, tuple[float, ...]]]]:
    role_filtered = []
    for row in views:
        role = infer_shot_role(row["source_uri"])
        if shot_role != "any" and role != shot_role:
            continue
        copied = dict(row)
        copied["shot_role"] = role
        role_filtered.append(copied)
    products = sorted({str(row["product_id"]) for row in role_filtered})
    hidden = hidden_products(products, hidden_ratio, seed)
    by_image_meta: dict[str, Json] = {}
    vectors: dict[str, dict[str, tuple[float, ...]]] = defaultdict(dict)
    for row in role_filtered:
        if row["product_id"] in hidden:
            continue
        role = row["shot_role"]
        by_image_meta.setdefault(
            row["image_id"],
            {"image_id": row["image_id"], "product_id": row["product_id"], "source_uri": row["source_uri"], "shot_role": role},
        )
        vectors[row["image_id"]][row["view_name"]] = row["embedding"]
    metas = [meta for meta in by_image_meta.values() if "db_full" in vectors[meta["image_id"]] and any(k.startswith("crop:") for k in vectors[meta["image_id"]])]
    metas.sort(key=lambda row: (row["product_id"], row["image_id"]))
    return metas, vectors


def score_product(query_views: dict[str, tuple[float, ...]], candidate_images: list[Json], vectors: dict[str, dict[str, tuple[float, ...]]], approach: str, query_image_id: str) -> float | None:
    scores: list[float] = []
    for cand in candidate_images:
        if cand["image_id"] == query_image_id:
            continue
        cand_views = vectors[cand["image_id"]]
        if approach == "db_full_only":
            scores.append(cosine(query_views["db_full"], cand_views["db_full"]))
        elif approach == "additive_max_all":
            for qvec in query_views.values():
                for cvec in cand_views.values():
                    scores.append(cosine(qvec, cvec))
        elif approach == "additive_same_view_max":
            for name, qvec in query_views.items():
                if name in cand_views:
                    scores.append(cosine(qvec, cand_views[name]))
        elif approach == "crop_center_same_view":
            for name in ("crop:center_070", "crop:center_050"):
                if name in query_views and name in cand_views:
                    scores.append(cosine(query_views[name], cand_views[name]))
        elif approach == "hybrid_full_center":
            full = cosine(query_views["db_full"], cand_views["db_full"])
            center_scores = [cosine(query_views[name], cand_views[name]) for name in ("crop:center_070", "crop:center_050") if name in query_views and name in cand_views]
            center = max(center_scores) if center_scores else full
            scores.append(0.45 * full + 0.55 * center)
        elif approach == "hybrid_full_crop_max":
            full = cosine(query_views["db_full"], cand_views["db_full"])
            crop_scores = [cosine(qvec, cvec) for qname, qvec in query_views.items() for cname, cvec in cand_views.items() if qname != "db_full" and cname != "db_full"]
            crop_best = max(crop_scores) if crop_scores else full
            scores.append(0.35 * full + 0.65 * crop_best)
        else:
            raise ValueError(approach)
    return max(scores) if scores else None


def rank_products(query: Json, by_product: dict[str, list[Json]], vectors: dict[str, dict[str, tuple[float, ...]]], approach: str) -> list[Json]:
    ranked = []
    for product_id, product_images in by_product.items():
        score = score_product(vectors[query["image_id"]], product_images, vectors, approach, query["image_id"])
        if score is not None:
            ranked.append({"product_id": product_id, "score": score})
    return sorted(ranked, key=lambda row: float(row["score"]), reverse=True)


def metric(name: str, ranks: list[int | None]) -> Json:
    total = len(ranks)
    return {
        "approach": name,
        "evaluated_probes": total,
        "top1_accuracy": sum(1 for rank in ranks if rank == 1) / total if total else 0.0,
        "top3_recall": sum(1 for rank in ranks if rank is not None and rank <= 3) / total if total else 0.0,
        "top5_recall": sum(1 for rank in ranks if rank is not None and rank <= 5) / total if total else 0.0,
        "missing_correct_candidate": sum(1 for rank in ranks if rank is None),
    }


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def evaluate(args: argparse.Namespace) -> Json:
    views = read_views(args.database_url)
    images, vectors = build_images(views, args.shot_role, args.hidden_ratio, args.seed)
    by_product: dict[str, list[Json]] = defaultdict(list)
    for image in images:
        by_product[image["product_id"]].append(image)
    probes = [image for image in images if len(by_product[image["product_id"]]) >= 2]
    approaches = ["db_full_only", "additive_same_view_max", "additive_max_all", "crop_center_same_view", "hybrid_full_center", "hybrid_full_crop_max"]
    ranks: dict[str, list[int | None]] = {name: [] for name in approaches}
    for query in probes:
        for approach in approaches:
            ranked = rank_products(query, by_product, vectors, approach)
            rank = None
            for idx, cand in enumerate(ranked, start=1):
                if cand["product_id"] == query["product_id"]:
                    rank = idx
                    break
            ranks[approach].append(rank)
    metrics = [metric(name, ranks[name]) for name in approaches]
    baseline = next(row for row in metrics if row["approach"] == "db_full_only")
    for row in metrics:
        row["delta_top1_vs_db_full"] = row["top1_accuracy"] - baseline["top1_accuracy"]
        row["delta_top5_vs_db_full"] = row["top5_recall"] - baseline["top5_recall"]
    return {
        "schema_version": "crop-db-readback-v1",
        "inputs": {"shot_role": args.shot_role, "preprocess_version": PREPROCESS_VERSION, "hidden_evaluated": False, "writes_detector_db": False},
        "split": {"selected_products": len(by_product), "selected_images": len(images), "evaluated_probes": len(probes)},
        "metrics": metrics,
    }


def write_markdown(report: Json, path: Path) -> None:
    metrics = sorted(report["metrics"], key=lambda row: -float(row["top1_accuracy"]))
    lines = [
        "# Crop DB Readback Validation",
        "",
        "Read-only validation against staged inactive `jewelry-crop-v1` DB rows.",
        "",
        "## Inputs",
        "",
        f"- shot role: `{report['inputs']['shot_role']}`",
        f"- preprocess version: `{report['inputs']['preprocess_version']}`",
        f"- hidden evaluated: `{str(report['inputs']['hidden_evaluated']).lower()}`",
        f"- writes detector DB: `{str(report['inputs']['writes_detector_db']).lower()}`",
        "",
        "## Split",
        "",
        f"- products: {report['split']['selected_products']}",
        f"- images: {report['split']['selected_images']}",
        f"- probes: {report['split']['evaluated_probes']}",
        "",
        "## Metrics",
        "",
        "| Approach | Top-1 | Top-5 | Δ Top-1 | Δ Top-5 | Missing correct |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(f"| `{row['approach']}` | {pct(row['top1_accuracy'])} | {pct(row['top5_recall'])} | {pct(row['delta_top1_vs_db_full'])} | {pct(row['delta_top5_vs_db_full'])} | {row['missing_correct_candidate']} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--shot-role", choices=["any", "studio_or_product", "live_or_lifestyle", "unknown"], default="any")
    parser.add_argument("--hidden-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--output", default="workbench/crop-embedding-pilot/db_readback.json")
    parser.add_argument("--markdown", default="docs/CROP_DB_READBACK_VALIDATION.md")
    args = parser.parse_args()
    report = evaluate(args)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown(report, Path(args.markdown))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
