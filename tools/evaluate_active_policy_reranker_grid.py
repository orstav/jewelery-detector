#!/usr/bin/env python3
"""Grid-search production-realistic rerank formulas over active-policy candidates.

Read-only: builds the same active-policy candidate rows as runtime matching, then
reranks product candidates with deterministic formulas over candidate evidence
features only. Product IDs are labels/split only.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_active_policy_retrieval import (  # noqa: E402
    hidden_products,
    payload_for_image,
    query_runtime_candidates,
    read_active_embeddings,
)
from tools import jewelry_detector_db as jdb  # noqa: E402

Json = dict[str, Any]
ScoreFn = Callable[[Json], float]


def val(candidate: Json, key: str, default: float = 0.0) -> float:
    try:
        return float(candidate.get(key, default) or default)
    except Exception:
        return default


def cap(candidate: Json, key: str, max_value: int) -> float:
    try:
        return min(max(int(candidate.get(key, 0) or 0), 0), max_value) / max_value
    except Exception:
        return 0.0


def rank_with(candidates: list[Json], scorer: ScoreFn) -> list[Json]:
    out = []
    for candidate in candidates:
        item = dict(candidate)
        item["rerank_score"] = scorer(item)
        out.append(item)
    ranked = sorted(
        out,
        key=lambda item: (
            val(item, "rerank_score"),
            val(item, "score"),
            val(item, "best_similarity"),
            val(item, "mean_top3_similarity"),
        ),
        reverse=True,
    )
    for idx, item in enumerate(ranked, start=1):
        item["rerank_rank"] = idx
        next_score = val(ranked[idx], "rerank_score") if idx < len(ranked) else 0.0
        item["rerank_margin"] = val(item, "rerank_score") - next_score
    return ranked


def formulas() -> list[tuple[str, ScoreFn]]:
    out: list[tuple[str, ScoreFn]] = [
        ("current_score", lambda c: val(c, "score")),
        ("best_only", lambda c: val(c, "best_similarity")),
        ("mean_top3_only", lambda c: val(c, "mean_top3_similarity")),
        ("coverage_light", lambda c: val(c, "score") + 0.010 * cap(c, "query_crop_count", 4) + 0.006 * cap(c, "candidate_crop_count", 6)),
        ("spike_penalty", lambda c: val(c, "score") - 0.08 * max(val(c, "best_similarity") - val(c, "mean_top3_similarity") - 0.04, 0.0)),
    ]
    weights = [0.2, 0.35, 0.5, 0.65, 0.8]
    bonuses = [0.0, 0.006, 0.012, 0.018]
    penalties = [0.0, 0.04, 0.08, 0.12]
    for best_w, bonus, penalty in itertools.product(weights, bonuses, penalties):
        mean_w = 1.0 - best_w
        name = f"grid_best{best_w:.2f}_mean{mean_w:.2f}_bonus{bonus:.3f}_penalty{penalty:.2f}"
        out.append(
            (
                name,
                lambda c, bw=best_w, mw=mean_w, b=bonus, p=penalty: (
                    bw * val(c, "best_similarity")
                    + mw * val(c, "mean_top3_similarity")
                    + b * cap(c, "query_crop_count", 4)
                    + b * 0.6 * cap(c, "candidate_crop_count", 6)
                    - p * max(val(c, "best_similarity") - val(c, "mean_top3_similarity") - 0.04, 0.0)
                ),
            )
        )
    return out


def rank_metrics(ranks: list[int | None]) -> Json:
    total = len(ranks)
    return {
        "evaluated_probes": total,
        "top1_accuracy": sum(1 for r in ranks if r == 1) / total if total else 0.0,
        "top3_recall": sum(1 for r in ranks if r is not None and r <= 3) / total if total else 0.0,
        "top5_recall": sum(1 for r in ranks if r is not None and r <= 5) / total if total else 0.0,
        "missing_correct_candidate": sum(1 for r in ranks if r is None),
    }


def build_cache(args: argparse.Namespace) -> tuple[list[Json], Json]:
    rows = read_active_embeddings(args.database_url)
    policy = jdb.load_active_policy(args.database_url)
    by_image: dict[str, list[Json]] = defaultdict(list)
    by_product_images: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_image[row["image_id"]].append(row)
        by_product_images[row["product_id"]].add(row["image_id"])
    product_ids = sorted(by_product_images)
    hidden = hidden_products(product_ids, args.hidden_ratio, args.seed)
    dev_products = set(product_ids) - hidden
    image_groups = [items for _image_id, items in sorted(by_image.items()) if items[0]["product_id"] in dev_products and len(by_product_images[items[0]["product_id"]]) >= 2]
    if args.shot_role != "any":
        image_groups = [items for items in image_groups if items[0]["shot_role"] == args.shot_role]
    if args.max_probes:
        image_groups = image_groups[: args.max_probes]
    cached = []
    for image_rows in image_groups:
        payload = payload_for_image(image_rows, mode="runtime_live_gate")
        effective = jdb.effective_candidate_policy(payload, policy)
        candidates = query_runtime_candidates(args.database_url, payload, policy, dev_products, args.top_k)
        cached.append(
            {
                "query_image_id": image_rows[0]["image_id"],
                "truth_product_id": image_rows[0]["product_id"],
                "shot_role": image_rows[0]["shot_role"],
                "candidate_policy_mode": effective.get("candidate_policy_mode"),
                "candidates": candidates,
            }
        )
    metadata = {
        "total_products": len(product_ids),
        "dev_products": len(dev_products),
        "hidden_products": len(hidden),
        "hidden_evaluated": False,
        "shot_roles": dict(Counter(item["shot_role"] for item in cached)),
        "probes": len(cached),
    }
    return cached, metadata


def evaluate_formula(name: str, scorer: ScoreFn, cached: list[Json]) -> Json:
    ranks_by_split: dict[str, list[int | None]] = defaultdict(list)
    examples: list[Json] = []
    for item in cached:
        truth = str(item["truth_product_id"])
        ranked = rank_with(item["candidates"], scorer)
        rank = next((idx for idx, candidate in enumerate(ranked, start=1) if str(candidate["product_id"]) == truth), None)
        ranks_by_split["all"].append(rank)
        ranks_by_split[str(item["shot_role"])].append(rank)
        if rank != 1 and len(examples) < 10:
            examples.append({"query_image_id": item["query_image_id"], "truth_product_id": truth, "shot_role": item["shot_role"], "rank": rank, "top_candidates": ranked[:5]})
    result = {"approach": name, **rank_metrics(ranks_by_split["all"])}
    result["split_metrics"] = {split: rank_metrics(ranks) for split, ranks in sorted(ranks_by_split.items()) if split != "all"}
    result["examples"] = examples
    return result


def evaluate(args: argparse.Namespace) -> Json:
    cached, metadata = build_cache(args)
    results = [evaluate_formula(name, scorer, cached) for name, scorer in formulas()]
    baseline = next(row for row in results if row["approach"] == "current_score")
    for row in results:
        row["delta_top1_vs_current"] = row["top1_accuracy"] - baseline["top1_accuracy"]
        row["delta_top5_vs_current"] = row["top5_recall"] - baseline["top5_recall"]
        for split, metrics in row["split_metrics"].items():
            base_split = baseline["split_metrics"].get(split, {})
            metrics["delta_top1_vs_current"] = metrics.get("top1_accuracy", 0.0) - base_split.get("top1_accuracy", 0.0)
            metrics["delta_top5_vs_current"] = metrics.get("top5_recall", 0.0) - base_split.get("top5_recall", 0.0)
    deployable = [
        row for row in results
        if row["delta_top1_vs_current"] >= args.min_top1_gain
        and row["delta_top5_vs_current"] >= args.min_top5_gain
        and all(split["delta_top1_vs_current"] >= args.max_split_top1_regression for split in row["split_metrics"].values())
        and all(split["delta_top5_vs_current"] >= args.max_split_top5_regression for split in row["split_metrics"].values())
    ]
    return {
        "schema_version": "active-policy-reranker-grid-v1",
        "inputs": {
            "writes_detector_db": False,
            "uses_external_api": False,
            "uses_product_id_as_evaluation_label_only": True,
            "top_k": args.top_k,
            "shot_role": args.shot_role,
        },
        "metadata": metadata,
        "baseline": baseline,
        "best_by_top1": sorted(results, key=lambda r: (r["top1_accuracy"], r["top5_recall"]), reverse=True)[:10],
        "best_by_top5": sorted(results, key=lambda r: (r["top5_recall"], r["top1_accuracy"]), reverse=True)[:10],
        "deployable_candidates": sorted(deployable, key=lambda r: (r["delta_top1_vs_current"], r["delta_top5_vs_current"]), reverse=True),
        "results": sorted(results, key=lambda r: (r["top1_accuracy"], r["top5_recall"]), reverse=True),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shot-role", choices=["any", "live", "studio", "unknown"], default="any")
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--min-top1-gain", type=float, default=0.005)
    parser.add_argument("--min-top5-gain", type=float, default=0.0)
    parser.add_argument("--max-split-top1-regression", type=float, default=-0.002)
    parser.add_argument("--max-split-top5-regression", type=float, default=-0.002)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "baseline": {k: report["baseline"][k] for k in ("top1_accuracy", "top5_recall")},
        "best": {k: report["best_by_top1"][0][k] for k in ("approach", "top1_accuracy", "top5_recall", "delta_top1_vs_current", "delta_top5_vs_current")},
        "deployable_candidates": len(report["deployable_candidates"]),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
