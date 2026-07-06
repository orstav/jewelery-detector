#!/usr/bin/env python3
"""Audit active-policy Top-K misses for product-level candidate generation.

Read-only. Uses product IDs only as evaluation labels. Does not write detector DB,
Shopify, Airtable, Drive, or messaging surfaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_active_policy_reranker_grid import build_cache, rank_with, val  # noqa: E402
from tools.evaluate_active_policy_retrieval import read_active_embeddings  # noqa: E402

Json = dict[str, Any]


def source_maps(database_url: str) -> tuple[dict[str, str], dict[str, list[Json]]]:
    rows = read_active_embeddings(database_url)
    image_to_source: dict[str, str] = {}
    product_sources: dict[str, list[Json]] = defaultdict(list)
    seen_product_image: set[tuple[str, str]] = set()
    for row in rows:
        image_id = str(row["image_id"])
        product_id = str(row["product_id"])
        image_to_source.setdefault(image_id, str(row.get("source_uri") or ""))
        key = (product_id, image_id)
        if key not in seen_product_image:
            product_sources[product_id].append(
                {
                    "image_id": image_id,
                    "source_uri": str(row.get("source_uri") or ""),
                    "shot_role": str(row.get("shot_role") or "unknown"),
                }
            )
            seen_product_image.add(key)
    return image_to_source, product_sources


def rank_for_truth(item: Json) -> tuple[int | None, list[Json]]:
    ranked = rank_with(item["candidates"], lambda candidate: val(candidate, "score"))
    truth = str(item["truth_product_id"])
    rank = next((idx for idx, candidate in enumerate(ranked, start=1) if str(candidate["product_id"]) == truth), None)
    return rank, ranked


def classify_missing(item: Json, rank: int | None, ranked: list[Json], k: int) -> list[str]:
    flags: list[str] = []
    if rank is None:
        flags.append("truth_not_in_candidate_pool")
    elif rank > k:
        flags.append(f"truth_rank_gt_{k}")
    if item.get("candidate_policy_mode") == "live_additive_crop" or item.get("shot_role") == "live":
        flags.append("live_query")
    if len(ranked) < k:
        flags.append("candidate_pool_smaller_than_k")
    if ranked:
        top = ranked[0]
        if str(top.get("product_id")) != str(item["truth_product_id"]):
            flags.append("wrong_top1")
    return flags


def audit(args: argparse.Namespace) -> Json:
    image_to_source, product_sources = source_maps(args.database_url)
    cached, metadata = build_cache(args)

    misses: list[Json] = []
    ranks_by_split: dict[str, list[int | None]] = defaultdict(list)
    flag_counts: Counter[str] = Counter()

    for item in cached:
        rank, ranked = rank_for_truth(item)
        split = str(item.get("shot_role") or "unknown")
        ranks_by_split["all"].append(rank)
        ranks_by_split[split].append(rank)
        if rank is None or rank > args.k:
            flags = classify_missing(item, rank, ranked, args.k)
            flag_counts.update(flags)
            top_candidates = ranked[: args.show_candidates]
            misses.append(
                {
                    "query_image_id": item["query_image_id"],
                    "query_source_uri": image_to_source.get(str(item["query_image_id"])),
                    "truth_product_id": str(item["truth_product_id"]),
                    "truth_rank": rank,
                    "shot_role": split,
                    "candidate_policy_mode": item.get("candidate_policy_mode"),
                    "flags": flags,
                    "truth_reference_images": product_sources.get(str(item["truth_product_id"]), [])[: args.truth_refs],
                    "top_candidates": [
                        {
                            "rank": idx,
                            "product_id": str(candidate.get("product_id")),
                            "score": candidate.get("score"),
                            "best_similarity": candidate.get("best_similarity"),
                            "mean_top3_similarity": candidate.get("mean_top3_similarity"),
                            "candidate_image_ids": [row.get("candidate_image_id") for row in candidate.get("evidence", [])[:3]],
                        }
                        for idx, candidate in enumerate(top_candidates, start=1)
                    ],
                }
            )

    def metrics(ranks: list[int | None]) -> Json:
        total = len(ranks)
        return {
            "evaluated_probes": total,
            "top1": sum(1 for r in ranks if r == 1),
            "top3": sum(1 for r in ranks if r is not None and r <= 3),
            "top5": sum(1 for r in ranks if r is not None and r <= 5),
            f"top{args.k}": sum(1 for r in ranks if r is not None and r <= args.k),
            f"top{args.k}_misses": sum(1 for r in ranks if r is None or r > args.k),
        }

    summary = {split: metrics(ranks) for split, ranks in sorted(ranks_by_split.items())}
    for split_summary in summary.values():
        total = split_summary["evaluated_probes"] or 1
        for key in ["top1", "top3", "top5", f"top{args.k}", f"top{args.k}_misses"]:
            split_summary[key + "_rate"] = split_summary[key] / total

    return {
        "schema_version": "topk-miss-audit-v1",
        "inputs": {
            "writes_detector_db": False,
            "uses_external_api": False,
            "uses_product_id_as_evaluation_label_only": True,
            "hidden_evaluated": False,
            "k": args.k,
            "top_k_candidate_pool": args.top_k,
            "shot_role": args.shot_role,
        },
        "metadata": metadata,
        "summary": summary,
        "flag_counts": dict(flag_counts),
        "miss_count": len(misses),
        "misses": misses[: args.limit_misses],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--shot-role", choices=["any", "live", "studio", "unknown"], default="any")
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--show-candidates", type=int, default=10)
    parser.add_argument("--truth-refs", type=int, default=6)
    parser.add_argument("--limit-misses", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit(args)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    all_summary = result["summary"]["all"]
    print(
        json.dumps(
            {
                "output": str(path),
                "evaluated_probes": all_summary["evaluated_probes"],
                f"top{args.k}": all_summary[f"top{args.k}"],
                f"top{args.k}_rate": round(all_summary[f"top{args.k}_rate"], 4),
                "miss_count": result["miss_count"],
                "flag_counts": result["flag_counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
