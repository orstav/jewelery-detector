#!/usr/bin/env python3
"""Evaluate detector DB match-policy grid on catalog dev products.

Read-only. Uses stored embeddings and pgvector retrieval to build product
candidates once, then applies many decision policies offline. Product IDs are
used only as benchmark truth/split labels; filenames are never used as matching
features.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
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


def parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def parse_int_or_all_list(value: str) -> list[int | None]:
    parsed: list[int | None] = []
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        parsed.append(None if item in {"all", "none", "prefix"} else int(item))
    return parsed


def build_probe_candidates(args: argparse.Namespace) -> tuple[list[Json], Json]:
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

    cached: list[Json] = []
    for probe in probes:
        raw = query_candidates(args.database_url, probe, ref_products=dev_products, top_k=args.top_k)
        cached.append(
            {
                "query_image_id": probe.image_id,
                "query_crop_id": probe.crop_id,
                "truth_product_id": probe.product_id,
                "candidates": jdb.aggregate_product_candidates(raw),
            }
        )

    split = {
        "seed": args.seed,
        "hidden_ratio": args.hidden_ratio,
        "total_products": len(product_ids),
        "dev_products": len(dev_products),
        "hidden_products": len(hidden),
        "hidden_products_sha256": hashlib.sha256("\n".join(sorted(hidden)).encode()).hexdigest(),
        "hidden_evaluated": False,
    }
    db_summary = {
        "embeddings": len(rows),
        "products": len(product_ids),
        "images": len({row.image_id for row in rows}),
        "models": sorted({row.embedding_model for row in rows}),
        "preprocess_versions": sorted({row.preprocess_version for row in rows}),
    }
    return cached, {"split": split, "db_summary": db_summary}


def evaluate_policy(cached: list[Json], policy: Json) -> Json:
    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    wrong_examples: list[Json] = []
    review_correct_top1 = 0
    review_wrong_top1 = 0
    no_match_correct_top1 = 0
    for item in cached:
        candidates = item["candidates"]
        truth = item["truth_product_id"]
        decision = jdb.decide_match(candidates, policy)
        status = str(decision["status"])
        reason = str(decision["reason"])
        reason_counts[f"{status}:{reason}"] += 1
        selected = decision.get("selected_product_id")
        top_product = candidates[0]["product_id"] if candidates else None
        if status == "matched":
            if selected == truth:
                counts["auto_correct"] += 1
            else:
                counts["auto_wrong"] += 1
                if len(wrong_examples) < 25:
                    wrong_examples.append(
                        {
                            "truth_product_id": truth,
                            "selected_product_id": selected,
                            "query_image_id": item["query_image_id"],
                            "query_crop_id": item["query_crop_id"],
                            "top_score": candidates[0].get("score") if candidates else None,
                            "top_margin": candidates[0].get("margin") if candidates else None,
                            "top_candidates": candidates[:5],
                        }
                    )
        elif status == "needs_review":
            counts["review"] += 1
            if top_product == truth:
                review_correct_top1 += 1
            else:
                review_wrong_top1 += 1
        else:
            counts["no_match"] += 1
            if top_product == truth:
                no_match_correct_top1 += 1

    total = len(cached)
    auto_total = counts["auto_correct"] + counts["auto_wrong"]
    result = {
        "policy": policy,
        "total_probes": total,
        "auto_total": auto_total,
        "auto_correct": counts["auto_correct"],
        "auto_wrong": counts["auto_wrong"],
        "review": counts["review"],
        "no_match": counts["no_match"],
        "auto_coverage": auto_total / total if total else 0.0,
        "correct_auto_recall": counts["auto_correct"] / total if total else 0.0,
        "wrong_auto_rate": counts["auto_wrong"] / total if total else 0.0,
        "auto_precision": counts["auto_correct"] / auto_total if auto_total else 0.0,
        "review_rate": counts["review"] / total if total else 0.0,
        "review_correct_top1": review_correct_top1,
        "review_wrong_top1": review_wrong_top1,
        "no_match_correct_top1": no_match_correct_top1,
        "reason_counts": dict(reason_counts.most_common()),
        "wrong_examples": wrong_examples,
    }
    return result


def rank_result(result: Json, baseline: Json) -> tuple[float, float, float, float]:
    # Prefer large wrong-auto reduction while preserving correct auto recall.
    wrong_reduction = baseline["auto_wrong"] - result["auto_wrong"]
    correct_loss = baseline["auto_correct"] - result["auto_correct"]
    review_added = result["review"] - baseline["review"]
    score = wrong_reduction - (0.65 * max(correct_loss, 0)) - (0.05 * max(review_added, 0))
    return (score, result["auto_precision"], result["correct_auto_recall"], -result["review_rate"])


def policy_grid(args: argparse.Namespace) -> list[Json]:
    base = {
        "candidate_min_score": args.candidate_min_score,
        "review_min_score": args.review_min_score,
        "auto_match_score": args.auto_match_score,
        "margin_threshold": args.margin_threshold,
    }
    policies: list[Json] = [{**base, "dense_family_guard_enabled": False, "label": "baseline_no_dense_guard"}]
    for margin, delta, window in itertools.product(
        parse_float_list(args.same_design_review_margins),
        parse_float_list(args.dense_family_score_deltas),
        parse_int_or_all_list(args.same_family_numeric_windows),
    ):
        policy = {
            **base,
            "dense_family_guard_enabled": True,
            "same_design_review_margin": margin,
            "dense_family_score_delta": delta,
            "same_family_numeric_window": window,
            "label": f"guard_margin={margin:g}_delta={delta:g}_window={'all' if window is None else window}",
        }
        policies.append(policy)
    return policies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--candidate-min-score", type=float, default=0.82)
    parser.add_argument("--review-min-score", type=float, default=0.86)
    parser.add_argument("--auto-match-score", type=float, default=0.93)
    parser.add_argument("--margin-threshold", type=float, default=0.03)
    parser.add_argument("--same-design-review-margins", default="0.04,0.05,0.06,0.07,0.08")
    parser.add_argument("--dense-family-score-deltas", default="0.02,0.03,0.04,0.05,0.06,0.08")
    parser.add_argument("--same-family-numeric-windows", default="1,2,5,10,all")
    args = parser.parse_args()

    cached, metadata = build_probe_candidates(args)
    results = [evaluate_policy(cached, policy) for policy in policy_grid(args)]
    baseline = results[0]
    for result in results:
        result["delta_vs_baseline"] = {
            "auto_correct": result["auto_correct"] - baseline["auto_correct"],
            "auto_wrong": result["auto_wrong"] - baseline["auto_wrong"],
            "review": result["review"] - baseline["review"],
            "no_match": result["no_match"] - baseline["no_match"],
            "auto_precision_pp": (result["auto_precision"] - baseline["auto_precision"]) * 100,
            "correct_auto_recall_pp": (result["correct_auto_recall"] - baseline["correct_auto_recall"]) * 100,
            "wrong_auto_rate_pp": (result["wrong_auto_rate"] - baseline["wrong_auto_rate"]) * 100,
        }
    ranked = sorted(results[1:], key=lambda result: rank_result(result, baseline), reverse=True)
    report = {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "stored image embeddings only; filenames/catalog names not used",
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "top_k": args.top_k,
        },
        **metadata,
        "baseline": baseline,
        "best_by_tradeoff": ranked[:10],
        "all_results": [baseline] + ranked,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "baseline": {
                    key: baseline[key]
                    for key in ["auto_total", "auto_correct", "auto_wrong", "review", "no_match", "auto_precision", "correct_auto_recall"]
                },
                "best_by_tradeoff": [
                    {
                        "label": item["policy"]["label"],
                        "auto_correct": item["auto_correct"],
                        "auto_wrong": item["auto_wrong"],
                        "review": item["review"],
                        "auto_precision": item["auto_precision"],
                        "correct_auto_recall": item["correct_auto_recall"],
                        "delta": item["delta_vs_baseline"],
                    }
                    for item in ranked[:5]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
