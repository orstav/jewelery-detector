#!/usr/bin/env python3
"""Sweep conservative multi-feature auto-match gates over pair-judge caches.

This read-only harness consumes an existing Top-K candidate cache produced by
`evaluate_offline_pair_judge_rerankers.py --write-candidate-cache`. It does not
call external APIs, does not read or write the detector DB, and does not use
filenames, image IDs, crop IDs, product IDs, or truth labels as scoring inputs.
Truth product IDs are used only for evaluation metrics.

Compared with `evaluate_pair_judge_threshold_sweep.py`, this explores additional
production-available ambiguity/quality gates for the selected top candidate:
mean-top3 similarity, spike gap (best minus mean), evidence count, query/candidate
view counts, and close-score competitor density.
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
    as_int,
    best_similarity,
    candidate_score,
    close_score_competitors,
    infer_split,
    load_candidate_cache,
    mean_top3,
    proxies,
    rerank,
    top_gap,
)

Json = dict[str, Any]


def parse_csv_floats(value: str) -> list[float]:
    out = [float(part.strip()) for part in value.split(",") if part.strip()]
    if not out:
        raise argparse.ArgumentTypeError("expected at least one numeric value")
    return out


def parse_csv_ints(value: str) -> list[int]:
    out = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not out:
        raise argparse.ArgumentTypeError("expected at least one integer value")
    return out


def top_rows(cached: list[Json]) -> list[Json]:
    rows: list[Json] = []
    for proxy in proxies():
        for item in cached:
            ranked = rerank(list(item.get("candidates") or []), proxy)
            if not ranked:
                continue
            top = ranked[0]
            rows.append(
                {
                    "approach": proxy.name,
                    "truth_product_id": str(item.get("truth_product_id")),
                    "selected_product_id": str(top.get("product_id")),
                    "split": infer_split(item, ranked),
                    "pair_judge_decision": top.get("pair_judge_decision"),
                    "proxy_score": as_float(top.get("proxy_score")),
                    "proxy_margin": as_float(top.get("proxy_margin")),
                    "candidate_score": candidate_score(top),
                    "best_similarity": best_similarity(top),
                    "mean_top3_similarity": mean_top3(top),
                    "top_gap": top_gap(top),
                    "evidence_count": as_int(top.get("evidence_count")),
                    "query_crop_count": as_int(top.get("query_crop_count")),
                    "candidate_crop_count": as_int(top.get("candidate_crop_count")),
                    "close_score_competitors_025": close_score_competitors(top, ranked, 0.025),
                    "close_score_competitors_050": close_score_competitors(top, ranked, 0.050),
                }
            )
    return rows


def passes(row: Json, gate: Json) -> bool:
    return (
        row["pair_judge_decision"] == "same_product"
        and row["approach"] == gate["approach"]
        and row["proxy_score"] >= gate["min_proxy_score"]
        and row["proxy_margin"] >= gate["min_proxy_margin"]
        and row["mean_top3_similarity"] >= gate["min_mean_top3_similarity"]
        and row["top_gap"] <= gate["max_top_gap"]
        and row["evidence_count"] >= gate["min_evidence_count"]
        and row["query_crop_count"] >= gate["min_query_crop_count"]
        and row["candidate_crop_count"] >= gate["min_candidate_crop_count"]
        and row["close_score_competitors_025"] <= gate["max_close_score_competitors_025"]
    )


def metrics_for(selected: list[Json], probe_count: int) -> Json:
    by_split: dict[str, Json] = defaultdict(lambda: {"auto_total": 0, "auto_correct": 0, "auto_wrong": 0})
    auto_correct = 0
    auto_wrong = 0
    examples = []
    for row in selected:
        split = str(row["split"])
        by_split[split]["auto_total"] += 1
        if row["truth_product_id"] == row["selected_product_id"]:
            auto_correct += 1
            by_split[split]["auto_correct"] += 1
        else:
            auto_wrong += 1
            by_split[split]["auto_wrong"] += 1
            if len(examples) < 10:
                examples.append(row)
    auto_total = len(selected)
    split_metrics = {}
    for split, bucket in sorted(by_split.items()):
        total = bucket["auto_total"]
        correct = bucket["auto_correct"]
        bucket["auto_precision"] = correct / total if total else 0.0
        split_metrics[split] = bucket
    return {
        "evaluated_probes": probe_count,
        "auto_total": auto_total,
        "auto_correct": auto_correct,
        "auto_wrong": auto_wrong,
        "auto_precision": auto_correct / auto_total if auto_total else 0.0,
        "correct_auto_recall": auto_correct / probe_count if probe_count else 0.0,
        "wrong_auto_rate": auto_wrong / probe_count if probe_count else 0.0,
        "split_metrics": split_metrics,
        "wrong_examples": examples,
    }


def evaluate(args: argparse.Namespace) -> Json:
    cached, metadata = load_candidate_cache(Path(args.candidate_cache))
    rows = top_rows(cached)
    gates: list[Json] = []
    for approach in sorted({row["approach"] for row in rows}):
        for min_proxy_score in args.min_proxy_scores:
            for min_proxy_margin in args.min_proxy_margins:
                for min_mean in args.min_mean_top3:
                    for max_gap in args.max_top_gaps:
                        for min_evidence in args.min_evidence_counts:
                            for min_query_crops in args.min_query_crop_counts:
                                for min_candidate_crops in args.min_candidate_crop_counts:
                                    for max_close in args.max_close_score_competitors_025:
                                        gate = {
                                            "approach": approach,
                                            "min_proxy_score": min_proxy_score,
                                            "min_proxy_margin": min_proxy_margin,
                                            "min_mean_top3_similarity": min_mean,
                                            "max_top_gap": max_gap,
                                            "min_evidence_count": min_evidence,
                                            "min_query_crop_count": min_query_crops,
                                            "min_candidate_crop_count": min_candidate_crops,
                                            "max_close_score_competitors_025": max_close,
                                        }
                                        selected = [row for row in rows if passes(row, gate)]
                                        if not selected:
                                            continue
                                        gates.append({**gate, **metrics_for(selected, len(cached))})
    zero_wrong = [gate for gate in gates if gate["auto_wrong"] == 0]
    safe_like = [
        gate
        for gate in gates
        if gate["evaluated_probes"] >= args.min_eval_probes
        and gate["auto_precision"] >= args.safe_auto_precision
        and gate["correct_auto_recall"] >= args.safe_auto_recall
        and gate["auto_wrong"] <= args.safe_max_wrong
    ]
    sort_key = lambda row: (row["auto_correct"], row["correct_auto_recall"], row["auto_precision"], -row["auto_wrong"])
    return {
        "schema_version": "pair-judge-multi-gate-sweep-v1",
        "inputs": {
            "algorithm_inputs": "existing Top-K retrieved candidate aggregate features only",
            "uses_external_api": False,
            "mutates_database": False,
            "uses_filename_tokens": False,
            "uses_truth_product_id_as_feature": False,
            "uses_candidate_product_id_for_scoring": False,
            "candidate_product_id_use": "evaluation labels and output labels only",
            "candidate_cache": args.candidate_cache,
        },
        "safe_gate": {
            "min_eval_probes": args.min_eval_probes,
            "safe_auto_precision": args.safe_auto_precision,
            "safe_auto_recall": args.safe_auto_recall,
            "safe_max_wrong": args.safe_max_wrong,
        },
        "metadata": metadata,
        "probe_count": len(cached),
        "top_row_count": len(rows),
        "zero_wrong_count": len(zero_wrong),
        "safe_like_count": len(safe_like),
        "best_zero_wrong": sorted(zero_wrong, key=sort_key, reverse=True)[: args.limit],
        "best_safe_like": sorted(safe_like, key=sort_key, reverse=True)[: args.limit],
        "best_overall": sorted(gates, key=lambda row: (row["auto_precision"], row["auto_correct"], -row["auto_wrong"]), reverse=True)[: args.limit],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-cache", required=True)
    parser.add_argument("--output", required=True)
    # Defaults intentionally stay small enough to run on the full offline cache
    # in a cron window. Pass denser CSV grids for targeted follow-up sweeps.
    parser.add_argument("--min-proxy-scores", type=parse_csv_floats, default=parse_csv_floats("0.90,0.94,0.98,0.99"))
    parser.add_argument("--min-proxy-margins", type=parse_csv_floats, default=parse_csv_floats("0.10,0.15,0.20,0.25"))
    parser.add_argument("--min-mean-top3", type=parse_csv_floats, default=parse_csv_floats("0,0.90,0.94,0.96"))
    parser.add_argument("--max-top-gaps", type=parse_csv_floats, default=parse_csv_floats("0.06,0.10,99"))
    parser.add_argument("--min-evidence-counts", type=parse_csv_ints, default=parse_csv_ints("0,2"))
    parser.add_argument("--min-query-crop-counts", type=parse_csv_ints, default=parse_csv_ints("0,1"))
    parser.add_argument("--min-candidate-crop-counts", type=parse_csv_ints, default=parse_csv_ints("0,2"))
    parser.add_argument("--max-close-score-competitors-025", type=parse_csv_ints, default=parse_csv_ints("0,2,99"))
    parser.add_argument("--min-eval-probes", type=int, default=100)
    parser.add_argument("--safe-auto-precision", type=float, default=0.97)
    parser.add_argument("--safe-auto-recall", type=float, default=0.05)
    parser.add_argument("--safe-max-wrong", type=int, default=0)
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "probe_count": report["probe_count"],
                "zero_wrong_count": report["zero_wrong_count"],
                "safe_like_count": report["safe_like_count"],
                "best_zero_wrong": report["best_zero_wrong"][0] if report["best_zero_wrong"] else None,
                "best_safe_like": report["best_safe_like"][0] if report["best_safe_like"] else None,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
