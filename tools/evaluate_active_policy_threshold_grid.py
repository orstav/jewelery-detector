#!/usr/bin/env python3
"""Evaluate active detector policy threshold grids without DB writes.

This uses runtime-style active-policy candidates, then applies candidate/review/auto
threshold combinations to measure safe auto-match behavior. Product IDs are only
evaluation labels.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import jewelry_detector_db as jdb  # noqa: E402
from tools.evaluate_active_policy_reranker_grid import build_cache  # noqa: E402

Json = dict[str, Any]


def floats(csv: str) -> list[float]:
    return [float(v.strip()) for v in csv.split(",") if v.strip()]


def policy_grid(args: argparse.Namespace) -> list[Json]:
    policies: list[Json] = []
    for cand, review, auto, margin in itertools.product(
        floats(args.candidate_min_scores),
        floats(args.review_min_scores),
        floats(args.auto_match_scores),
        floats(args.margin_thresholds),
    ):
        if review < cand or auto < review:
            continue
        policies.append(
            {
                "candidate_min_score": cand,
                "review_min_score": review,
                "auto_match_score": auto,
                "margin_threshold": margin,
                "label": f"cand={cand:.3f}_review={review:.3f}_auto={auto:.3f}_margin={margin:.3f}",
            }
        )
    return policies


def evaluate_policy(cached: list[Json], policy: Json, args: argparse.Namespace) -> Json:
    counts: Counter[str] = Counter()
    by_split: dict[str, Counter[str]] = defaultdict(Counter)
    wrong_examples: list[Json] = []
    for item in cached:
        truth = str(item["truth_product_id"])
        split = str(item.get("shot_role") or "unknown")
        decision = jdb.decide_match(item["candidates"], policy)
        status = str(decision["status"])
        selected = str(decision.get("selected_product_id") or "")
        bucket = by_split[split]
        if status == "matched":
            if selected == truth:
                counts["auto_correct"] += 1
                bucket["auto_correct"] += 1
            else:
                counts["auto_wrong"] += 1
                bucket["auto_wrong"] += 1
                if len(wrong_examples) < args.example_limit:
                    wrong_examples.append(
                        {
                            "query_image_id": item.get("query_image_id"),
                            "truth_product_id": truth,
                            "selected_product_id": selected,
                            "shot_role": split,
                            "decision": decision,
                            "top_candidates": item["candidates"][:5],
                        }
                    )
        elif status == "needs_review":
            counts["review"] += 1
            bucket["review"] += 1
        else:
            counts["no_match"] += 1
            bucket["no_match"] += 1
    total = len(cached)
    auto_total = counts["auto_correct"] + counts["auto_wrong"]
    split_metrics = {}
    for split, bucket in sorted(by_split.items()):
        split_total = sum(bucket.values())
        split_auto = bucket["auto_correct"] + bucket["auto_wrong"]
        split_metrics[split] = {
            "total": split_total,
            "auto_total": split_auto,
            "auto_correct": bucket["auto_correct"],
            "auto_wrong": bucket["auto_wrong"],
            "review": bucket["review"],
            "no_match": bucket["no_match"],
            "auto_precision": bucket["auto_correct"] / split_auto if split_auto else 0.0,
            "correct_auto_recall": bucket["auto_correct"] / split_total if split_total else 0.0,
        }
    result = {
        "policy": policy,
        "total": total,
        "auto_total": auto_total,
        "auto_correct": counts["auto_correct"],
        "auto_wrong": counts["auto_wrong"],
        "review": counts["review"],
        "no_match": counts["no_match"],
        "auto_precision": counts["auto_correct"] / auto_total if auto_total else 0.0,
        "correct_auto_recall": counts["auto_correct"] / total if total else 0.0,
        "wrong_auto_rate": counts["auto_wrong"] / total if total else 0.0,
        "split_metrics": split_metrics,
        "wrong_examples": wrong_examples,
    }
    result["safe_to_deploy"] = bool(
        result["auto_wrong"] <= args.safe_max_wrong
        and result["auto_precision"] >= args.safe_auto_precision
        and result["correct_auto_recall"] >= args.safe_auto_recall
        and all(m["auto_wrong"] <= args.safe_max_wrong_per_split for m in split_metrics.values())
    )
    return result


def score_result(result: Json) -> tuple[float, float, float, float]:
    return (
        -1000 * result["auto_wrong"]
        + 250 * result["auto_precision"]
        + 120 * result["correct_auto_recall"]
        - 0.01 * result["review"],
        result["auto_precision"],
        result["correct_auto_recall"],
        -result["auto_wrong"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shot-role", choices=["any", "live", "studio", "unknown"], default="any")
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--candidate-min-scores", default="0.82,0.84,0.86,0.88")
    parser.add_argument("--review-min-scores", default="0.86,0.88,0.90,0.92")
    parser.add_argument("--auto-match-scores", default="0.93,0.94,0.95,0.96,0.97,0.98")
    parser.add_argument("--margin-thresholds", default="0.03,0.04,0.05,0.06,0.08,0.10")
    parser.add_argument("--safe-auto-precision", type=float, default=0.97)
    parser.add_argument("--safe-auto-recall", type=float, default=0.05)
    parser.add_argument("--safe-max-wrong", type=int, default=0)
    parser.add_argument("--safe-max-wrong-per-split", type=int, default=0)
    parser.add_argument("--example-limit", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cached, metadata = build_cache(args)
    policies = policy_grid(args)
    results = [evaluate_policy(cached, p, args) for p in policies]
    active_policy = jdb.load_active_policy(args.database_url)
    current = evaluate_policy(cached, {**active_policy, "label": "current_active_db_policy"}, args)
    ranked = sorted(results, key=score_result, reverse=True)
    safe = [r for r in ranked if r["safe_to_deploy"]]
    report = {
        "schema_version": "active-policy-threshold-grid-v1",
        "inputs": {"writes_detector_db": False, "uses_external_api": False, "uses_product_id_as_evaluation_label_only": True, "shot_role": args.shot_role, "top_k": args.top_k},
        "metadata": metadata,
        "current_active_policy_result": current,
        "best_by_score": ranked[:20],
        "safe_to_deploy_candidates": safe[:20],
        "all_results": ranked,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "current": {k: current[k] for k in ["auto_total", "auto_correct", "auto_wrong", "auto_precision", "correct_auto_recall"]},
        "best": {k: ranked[0][k] for k in ["policy", "auto_total", "auto_correct", "auto_wrong", "auto_precision", "correct_auto_recall", "safe_to_deploy"]},
        "safe_candidates": len(safe),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
