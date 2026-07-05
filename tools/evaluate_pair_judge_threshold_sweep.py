#!/usr/bin/env python3
"""Sweep auto-match thresholds over offline pair-judge candidate caches.

This read-only harness consumes an existing Top-K candidate cache produced by
`evaluate_offline_pair_judge_rerankers.py --write-candidate-cache`. It does not
call external APIs, does not read or write the detector DB, and does not use
filenames, image IDs, crop IDs, product IDs, or truth labels as scoring inputs.
Truth product IDs are used only for evaluation metrics.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.evaluate_offline_pair_judge_rerankers import (  # noqa: E402
    as_float,
    infer_split,
    load_candidate_cache,
    proxies,
    rerank,
)

Json = dict[str, Any]


def parse_csv_floats(value: str) -> list[float]:
    out: list[float] = []
    for part in value.split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    if not out:
        raise argparse.ArgumentTypeError("expected at least one numeric value")
    return out


def threshold_result(
    *,
    approach: str,
    ranked_items: list[tuple[Json, Json, str]],
    auto_score: float,
    auto_margin: float,
) -> Json:
    by_split: dict[str, Json] = defaultdict(lambda: {"auto_total": 0, "auto_correct": 0, "auto_wrong": 0})
    auto_total = 0
    auto_correct = 0
    auto_wrong = 0
    for item, top, split in ranked_items:
        if not (
            top.get("pair_judge_decision") == "same_product"
            and as_float(top.get("proxy_score")) >= auto_score
            and as_float(top.get("proxy_margin")) >= auto_margin
        ):
            continue
        auto_total += 1
        by_split[split]["auto_total"] += 1
        if str(top.get("product_id")) == str(item.get("truth_product_id")):
            auto_correct += 1
            by_split[split]["auto_correct"] += 1
        else:
            auto_wrong += 1
            by_split[split]["auto_wrong"] += 1
    probe_count = len(ranked_items)
    return {
        "approach": approach,
        "auto_score_threshold": auto_score,
        "auto_margin_threshold": auto_margin,
        "evaluated_probes": probe_count,
        "auto_total": auto_total,
        "auto_correct": auto_correct,
        "auto_wrong": auto_wrong,
        "auto_precision": auto_correct / auto_total if auto_total else 0.0,
        "correct_auto_recall": auto_correct / probe_count if probe_count else 0.0,
        "wrong_auto_rate": auto_wrong / probe_count if probe_count else 0.0,
        "split_metrics": dict(sorted(by_split.items())),
    }


def evaluate(args: argparse.Namespace) -> Json:
    cached, metadata = load_candidate_cache(Path(args.candidate_cache))
    results: list[Json] = []
    for proxy in proxies():
        ranked_items: list[tuple[Json, Json, str]] = []
        for item in cached:
            ranked = rerank(list(item.get("candidates") or []), proxy)
            if ranked:
                ranked_items.append((item, ranked[0], infer_split(item, ranked)))
        for auto_score in args.auto_scores:
            for auto_margin in args.auto_margins:
                results.append(
                    threshold_result(
                        approach=proxy.name,
                        ranked_items=ranked_items,
                        auto_score=auto_score,
                        auto_margin=auto_margin,
                    )
                )
    zero_wrong = [row for row in results if row["auto_wrong"] == 0]
    return {
        "schema_version": "pair-judge-threshold-sweep-v1",
        "inputs": {
            "algorithm_inputs": "existing Top-K retrieved candidate aggregate features only",
            "uses_external_api": False,
            "mutates_database": False,
            "uses_filename_tokens": False,
            "uses_truth_product_id_as_feature": False,
            "uses_candidate_product_id_for_scoring": False,
            "candidate_product_id_use": "evaluation labels and output labels only",
            "candidate_cache": args.candidate_cache,
            "auto_scores": args.auto_scores,
            "auto_margins": args.auto_margins,
        },
        "metadata": metadata,
        "probe_count": len(cached),
        "best_zero_wrong": sorted(zero_wrong, key=lambda row: (row["auto_correct"], row["correct_auto_recall"], row["auto_total"]), reverse=True)[: args.limit],
        "best_overall": sorted(results, key=lambda row: (row["auto_precision"], row["auto_correct"], -row["auto_wrong"]), reverse=True)[: args.limit],
        "results": sorted(results, key=lambda row: (row["auto_wrong"] == 0, row["auto_correct"], row["auto_precision"]), reverse=True),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--auto-scores", type=parse_csv_floats, default=parse_csv_floats("0.80,0.82,0.84,0.86,0.88,0.90,0.92,0.94,0.96,0.98,1.00"))
    parser.add_argument("--auto-margins", type=parse_csv_floats, default=parse_csv_floats("0,0.01,0.02,0.03,0.04,0.05,0.06,0.08,0.10,0.12,0.15,0.20"))
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    best_zero = report["best_zero_wrong"][0] if report["best_zero_wrong"] else None
    print(
        json.dumps(
            {
                "output": str(output),
                "probe_count": report["probe_count"],
                "best_zero_wrong": best_zero,
                "zero_wrong_candidates": len(report["best_zero_wrong"]),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
