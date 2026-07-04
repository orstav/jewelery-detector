#!/usr/bin/env python3
"""Mine hard-negative product pairs from the current detector embedding DB.

Read-only benchmark helper. It uses stored image/crop embeddings and pgvector
retrieval, then records cases where the current product aggregation ranks the
wrong product above the truth product. Catalog product IDs are used only as
benchmark labels/split keys; filenames are never used as matching features.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
    hidden_products,
    query_candidates,
    read_catalog_embeddings,
)

Json = dict[str, Any]


def product_family(product_id: str) -> str:
    """Coarse adjacent-ID family bucket for diagnostics only.

    This is intentionally not a model feature. It helps reviewers sort the
    mined error list and spot likely same-design/sibling collisions.
    """
    prefix = "".join(ch for ch in product_id if ch.isalpha()) or product_id[:1]
    digits = "".join(ch for ch in product_id if ch.isdigit())
    if not digits:
        return product_id
    number = int(digits)
    return f"{prefix}{number // 10:03d}x"


def similarity_band(score: float | None) -> str:
    if score is None:
        return "missing"
    if score >= 0.95:
        return ">=0.95"
    if score >= 0.90:
        return "0.90-0.95"
    if score >= 0.85:
        return "0.85-0.90"
    return "<0.85"


def classify_negative(item: Json) -> str:
    margin = item.get("wrong_minus_truth_margin")
    truth = item["truth_product_id"]
    wrong = item["wrong_top_product_id"]
    if margin is not None and margin <= item["close_margin_threshold"]:
        return "close_margin_sibling_risk"
    if product_family(truth) == product_family(wrong):
        return "same_prefix_family_risk"
    if item.get("truth_rank") is None:
        return "truth_missing_from_top_k"
    if item["truth_rank"] <= 5:
        return "rerankable_top5_error"
    return "broad_retrieval_error"


def ranked_candidates(raw_rows: list[Json]) -> list[Json]:
    ranked = jdb.aggregate_product_candidates(raw_rows)
    # Keep only JSON-serializable scalar summaries needed for review.
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


def mine_hard_negatives(args: argparse.Namespace) -> Json:
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
    pair_counts: Counter[tuple[str, str]] = Counter()
    type_counts: Counter[str] = Counter()
    band_counts: Counter[str] = Counter()
    correct_missing = 0

    for probe in probes:
        raw = query_candidates(args.database_url, probe, ref_products=dev_products, top_k=args.top_k)
        ranked = ranked_candidates(raw)
        top = ranked[0] if ranked else None
        truth_candidate = next((cand for cand in ranked if cand["product_id"] == probe.product_id), None)
        truth_rank = truth_candidate.get("rank") if truth_candidate else None
        if truth_rank == 1:
            continue
        if args.only_rerankable and (truth_rank is None or truth_rank > args.rerankable_rank):
            continue
        if truth_candidate is None:
            correct_missing += 1

        wrong_score = float(top["score"]) if top and top.get("score") is not None else None
        truth_score = float(truth_candidate["score"]) if truth_candidate and truth_candidate.get("score") is not None else None
        wrong_minus_truth = wrong_score - truth_score if wrong_score is not None and truth_score is not None else None
        item: Json = {
            "query_image_id": probe.image_id,
            "query_crop_id": probe.crop_id,
            "truth_product_id": probe.product_id,
            "wrong_top_product_id": top.get("product_id") if top else None,
            "truth_rank": truth_rank,
            "wrong_top_score": wrong_score,
            "truth_score": truth_score,
            "wrong_minus_truth_margin": wrong_minus_truth,
            "top_margin": top.get("margin") if top else None,
            "close_margin_threshold": args.close_margin,
            "truth_family": product_family(probe.product_id),
            "wrong_family": product_family(str(top.get("product_id"))) if top else None,
            "top_candidates": ranked[: args.candidate_limit],
        }
        item["negative_type"] = classify_negative(item)
        item["wrong_score_band"] = similarity_band(wrong_score)
        negatives.append(item)
        if item["wrong_top_product_id"]:
            pair_counts[(item["truth_product_id"], item["wrong_top_product_id"])] += 1
        type_counts[item["negative_type"]] += 1
        band_counts[item["wrong_score_band"]] += 1

    negatives.sort(
        key=lambda item: (
            item["negative_type"] != "close_margin_sibling_risk",
            item["truth_rank"] if item["truth_rank"] is not None else 999,
            -(item["wrong_top_score"] or 0),
            item["truth_product_id"],
        )
    )

    top_pairs = [
        {"truth_product_id": truth, "wrong_top_product_id": wrong, "count": count}
        for (truth, wrong), count in pair_counts.most_common(args.pair_limit)
    ]
    summary: Json = {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "stored image embeddings only; filenames/catalog names not used",
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "top_k": args.top_k,
            "only_rerankable": args.only_rerankable,
            "rerankable_rank": args.rerankable_rank,
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
            "embeddings": len(rows),
            "products": len(product_ids),
            "images": len({row.image_id for row in rows}),
            "models": sorted({row.embedding_model for row in rows}),
            "preprocess_versions": sorted({row.preprocess_version for row in rows}),
        },
        "counts": {
            "evaluated_probes": len(probes),
            "hard_negatives": len(negatives),
            "truth_missing_from_top_k": correct_missing,
            "type_counts": dict(type_counts.most_common()),
            "wrong_score_bands": dict(band_counts.most_common()),
        },
        "top_pairs": top_pairs,
        "hard_negatives": negatives[: args.limit],
    }
    return summary


def write_csv(path: Path, negatives: list[Json]) -> None:
    fields = [
        "negative_type",
        "truth_product_id",
        "wrong_top_product_id",
        "truth_rank",
        "wrong_top_score",
        "truth_score",
        "wrong_minus_truth_margin",
        "top_margin",
        "query_image_id",
        "query_crop_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in negatives:
            writer.writerow({field: item.get(field) for field in fields})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--csv-output")
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--pair-limit", type=int, default=40)
    parser.add_argument("--candidate-limit", type=int, default=5)
    parser.add_argument("--close-margin", type=float, default=0.015)
    parser.add_argument("--only-rerankable", action="store_true")
    parser.add_argument("--rerankable-rank", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = mine_hard_negatives(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.csv_output:
        write_csv(Path(args.csv_output), report["hard_negatives"])
    print(
        json.dumps(
            {
                "output": str(output),
                "csv_output": args.csv_output,
                "counts": report["counts"],
                "top_pairs": report["top_pairs"][:10],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
