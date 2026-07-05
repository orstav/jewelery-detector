#!/usr/bin/env python3
"""Evaluate cheap crop-localized perceptual-hash retrieval on detector DB catalog images.

Read-only. This is not a final model; it tests whether simple jewelry-localized
views and image hashes add useful identity signal before spending time on full
crop-embedding generation. Product IDs are used only as evaluation truth/split
labels, never as matching inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_db_embedding_retrieval import connect, hidden_products
from tools.jewelry_cluster_benchmark import open_pillow_image, pillow_resample_lanczos

Json = dict[str, Any]
HashFn = Callable[[Json, Json], float]


@dataclass(frozen=True)
class ImageRow:
    image_id: str
    product_id: str
    source_uri: str


def read_product_images(url: str) -> list[ImageRow]:
    with connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT image_id, product_id, source_uri
            FROM product_images
            WHERE product_id IS NOT NULL AND status IN ('active', 'ready')
            ORDER BY product_id, image_id
            """
        )
        return [ImageRow(str(a), str(b), str(c)) for a, b, c in cur.fetchall()]


def hamming_hex(left: str, right: str) -> int:
    if not left or not right:
        return 10**9
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def average_hash_from_image(image: Any, size: int = 8) -> str:
    img = image.convert("RGB").resize((size, size), pillow_resample_lanczos())
    pixels = list(img.getdata())
    grays = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    avg = sum(grays) / len(grays)
    bits = ["1" if gray >= avg else "0" for gray in grays]
    return f"{int(''.join(bits), 2):0{size * size // 4}x}"


def difference_hash_from_image(image: Any, width: int = 9, height: int = 8) -> str:
    img = image.convert("RGB").resize((width, height), pillow_resample_lanczos())
    pixels = list(img.getdata())
    grays = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    bits: list[str] = []
    for y in range(height):
        for x in range(width - 1):
            bits.append("1" if grays[y * width + x] > grays[y * width + x + 1] else "0")
    return f"{int(''.join(bits), 2):016x}"


def clamp_box(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = box
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def center_square_box(width: int, height: int, fraction: float) -> tuple[int, int, int, int]:
    side = max(1, round(min(width, height) * fraction))
    return clamp_box(((width - side) // 2, (height - side) // 2, side, side), width, height)


def padded_box(box: tuple[int, int, int, int], width: int, height: int, pad: float = 0.35) -> tuple[int, int, int, int]:
    x, y, w, h = box
    px = round(w * pad)
    py = round(h * pad)
    return clamp_box((x - px, y - py, w + 2 * px, h + 2 * py), width, height)


def foreground_box_from_image(image: Any, sample_size: int = 384) -> tuple[int, int, int, int] | None:
    work = image.copy()
    work.thumbnail((sample_size, sample_size), pillow_resample_lanczos())
    width, height = work.size
    pixels = list(work.getdata())

    def pixel_at(x: int, y: int) -> tuple[int, int, int]:
        return pixels[y * width + x]

    border: list[tuple[int, int, int]] = []
    for x in range(width):
        border.append(pixel_at(x, 0))
        border.append(pixel_at(x, height - 1))
    for y in range(height):
        border.append(pixel_at(0, y))
        border.append(pixel_at(width - 1, y))
    background = tuple(sorted(pixel[channel] for pixel in border)[len(border) // 2] for channel in range(3))
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            red, green, blue = pixel_at(x, y)
            distance = abs(red - background[0]) + abs(green - background[1]) + abs(blue - background[2])
            if distance > 45 and not (red > 245 and green > 245 and blue > 245):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    scale_x = image.size[0] / width
    scale_y = image.size[1] / height
    x1 = round(min(xs) * scale_x)
    y1 = round(min(ys) * scale_y)
    x2 = round((max(xs) + 1) * scale_x)
    y2 = round((max(ys) + 1) * scale_y)
    return clamp_box((x1, y1, x2 - x1, y2 - y1), image.size[0], image.size[1])


def build_hash_record(row: ImageRow, *, max_side: int = 1600, include_foreground: bool = True) -> Json:
    source = Path(row.source_uri)
    record: Json = {
        "image_id": row.image_id,
        "product_id": row.product_id,
        "source_uri": row.source_uri,
        "views": {},
        "status": "ok",
    }
    if not source.exists():
        record["status"] = "missing_source"
        return record
    try:
        from PIL import ImageOps

        with open_pillow_image(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail((max_side, max_side), pillow_resample_lanczos())
            width, height = image.size
            boxes: dict[str, tuple[int, int, int, int]] = {
                "full": (0, 0, width, height),
                "center70": center_square_box(width, height, 0.70),
                "center50": center_square_box(width, height, 0.50),
            }
            fg = foreground_box_from_image(image) if include_foreground else None
            if fg:
                boxes["foreground_padded"] = padded_box(fg, width, height)
            else:
                boxes["foreground_padded"] = boxes["center70"]
            for view, box in boxes.items():
                x, y, w, h = box
                crop = image.crop((x, y, x + w, y + h))
                record["views"][view] = {
                    "box": [x, y, w, h],
                    "ahash": average_hash_from_image(crop),
                    "dhash": difference_hash_from_image(crop),
                }
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{exc.__class__.__name__}: {exc}"
    return record


def hash_similarity(left: str, right: str, bits: int = 64) -> float:
    return 1.0 - (hamming_hex(left, right) / bits)


def view_similarity(left: Json, right: Json, view: str, hash_name: str) -> float:
    lview = left.get("views", {}).get(view, {})
    rview = right.get("views", {}).get(view, {})
    return hash_similarity(str(lview.get(hash_name, "")), str(rview.get(hash_name, "")))


def max_view_similarity(left: Json, right: Json, views: list[str], hash_name: str) -> float:
    return max((view_similarity(left, right, view, hash_name) for view in views), default=0.0)


def combo_similarity(left: Json, right: Json, weights: dict[tuple[str, str], float]) -> float:
    total = sum(weights.values()) or 1.0
    return sum(weight * view_similarity(left, right, view, hash_name) for (view, hash_name), weight in weights.items()) / total


def approaches() -> dict[str, HashFn]:
    return {
        "01_full_ahash": lambda q, r: view_similarity(q, r, "full", "ahash"),
        "02_full_dhash": lambda q, r: view_similarity(q, r, "full", "dhash"),
        "03_center70_ahash": lambda q, r: view_similarity(q, r, "center70", "ahash"),
        "04_center70_dhash": lambda q, r: view_similarity(q, r, "center70", "dhash"),
        "05_center50_ahash": lambda q, r: view_similarity(q, r, "center50", "ahash"),
        "06_center50_dhash": lambda q, r: view_similarity(q, r, "center50", "dhash"),
        "07_foreground_ahash": lambda q, r: view_similarity(q, r, "foreground_padded", "ahash"),
        "08_foreground_dhash": lambda q, r: view_similarity(q, r, "foreground_padded", "dhash"),
        "09_best_view_dhash": lambda q, r: max_view_similarity(q, r, ["full", "center70", "center50", "foreground_padded"], "dhash"),
        "10_weighted_multi_hash": lambda q, r: combo_similarity(
            q,
            r,
            {
                ("full", "dhash"): 0.20,
                ("center70", "dhash"): 0.25,
                ("center50", "dhash"): 0.20,
                ("foreground_padded", "dhash"): 0.25,
                ("foreground_padded", "ahash"): 0.10,
            },
        ),
    }


def rank_products(query: Json, refs: list[Json], score_fn: HashFn, top_k: int) -> list[Json]:
    best_by_product: dict[str, Json] = {}
    for ref in refs:
        if ref["image_id"] == query["image_id"]:
            continue
        score = score_fn(query, ref)
        pid = str(ref["product_id"])
        current = best_by_product.get(pid)
        if current is None or score > float(current["score"]):
            best_by_product[pid] = {
                "product_id": pid,
                "score": score,
                "image_id": ref["image_id"],
            }
    ranked = sorted(best_by_product.values(), key=lambda x: float(x["score"]), reverse=True)[:top_k]
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
        next_score = float(ranked[idx]["score"]) if idx < len(ranked) else 0.0
        item["margin"] = float(item["score"]) - next_score
    return ranked


def evaluate(records: list[Json], args: argparse.Namespace) -> Json:
    ok = [r for r in records if r.get("status") == "ok"]
    product_ids = sorted({str(r["product_id"]) for r in ok})
    hidden = hidden_products(product_ids, args.hidden_ratio, args.seed)
    dev_products = set(product_ids) - hidden
    by_product: dict[str, list[Json]] = defaultdict(list)
    for record in ok:
        by_product[str(record["product_id"])].append(record)
    probes = [r for r in ok if r["product_id"] in dev_products and len(by_product[str(r["product_id"])]) >= 2]
    refs = [r for r in ok if r["product_id"] in dev_products]
    if args.max_probes:
        probes = probes[: args.max_probes]

    results = []
    examples: dict[str, list[Json]] = defaultdict(list)
    for name, score_fn in approaches().items():
        counts = Counter()
        for probe in probes:
            ranked = rank_products(probe, refs, score_fn, args.top_k)
            rank = None
            for candidate in ranked:
                if candidate["product_id"] == probe["product_id"]:
                    rank = candidate["rank"]
                    break
            if rank == 1:
                counts["top1"] += 1
            if rank is not None and rank <= 3:
                counts["top3"] += 1
            if rank is not None and rank <= 5:
                counts["top5"] += 1
            if rank is None:
                counts["missing"] += 1
            if rank != 1 and len(examples[name]) < args.example_limit:
                examples[name].append(
                    {
                        "query_image_id": probe["image_id"],
                        "truth_product_id": probe["product_id"],
                        "truth_rank": rank,
                        "top_candidates": ranked[:5],
                    }
                )
        total = len(probes)
        results.append(
            {
                "approach": name,
                "evaluated_probes": total,
                "top1": counts["top1"],
                "top3": counts["top3"],
                "top5": counts["top5"],
                "missing_correct_candidate": counts["missing"],
                "top1_accuracy": counts["top1"] / total if total else 0.0,
                "top3_recall": counts["top3"] / total if total else 0.0,
                "top5_recall": counts["top5"] / total if total else 0.0,
                "examples": examples[name],
            }
        )
    ranked_results = sorted(results, key=lambda x: (x["top1_accuracy"], x["top3_recall"], x["top5_recall"]), reverse=True)
    return {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "source image pixels -> full/center/foreground heuristic crops -> ahash/dhash only",
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "uses_truth_product_id_as_feature": False,
            "top_k": args.top_k,
        },
        "split": {
            "seed": args.seed,
            "hidden_ratio": args.hidden_ratio,
            "total_products": len(product_ids),
            "dev_products": len(dev_products),
            "hidden_products": len(hidden),
            "hidden_products_sha256": hashlib.sha256("\n".join(sorted(hidden)).encode()).hexdigest(),
            "hidden_evaluated": False,
        },
        "db_summary": {
            "images_read": len(records),
            "images_ok": len(ok),
            "images_missing_or_error": len(records) - len(ok),
        },
        "best_approach": ranked_results[0] if ranked_results else None,
        "results": ranked_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", help="optional hash cache JSON; defaults next to output")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--max-images", type=int, help="debug/smoke cap before split")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--example-limit", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    cache_path = Path(args.cache) if args.cache else output.with_suffix(".hash_cache.json")
    if cache_path.exists():
        records = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        rows = read_product_images(args.database_url)
        if args.max_images:
            rows = rows[: args.max_images]
        records = [build_hash_record(row) for row in rows]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    report = evaluate(records, args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    best = report.get("best_approach") or {}
    print(
        json.dumps(
            {
                "output": str(output),
                "cache": str(cache_path),
                "best": {
                    "approach": best.get("approach"),
                    "top1_accuracy": best.get("top1_accuracy"),
                    "top3_recall": best.get("top3_recall"),
                    "top5_recall": best.get("top5_recall"),
                },
                "images_ok": report["db_summary"]["images_ok"],
                "hidden_evaluated": report["split"]["hidden_evaluated"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
