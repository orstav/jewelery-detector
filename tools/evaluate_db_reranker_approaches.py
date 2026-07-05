#!/usr/bin/env python3
"""Compare 10 production-realistic DB reranking approaches on catalog dev products.

Read-only. Uses stored image/crop embeddings and pgvector retrieval as the
candidate generator, then reranks product candidates with deterministic feature
formulas that do not use filenames, probe product IDs, or catalog prefixes as
matching inputs. Product IDs are used only for evaluation labels and split.
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

from tools import jewelry_detector_db as jdb
from tools.evaluate_db_embedding_retrieval import EmbeddingRow, hidden_products, query_candidates, read_catalog_embeddings

Json = dict[str, Any]
ScoreFn = Callable[[Json, list[Json]], float]


@dataclass(frozen=True)
class Approach:
    name: str
    description: str
    score_fn: ScoreFn


def clamp_count(value: Any, cap: int) -> int:
    try:
        return min(max(int(value), 0), cap)
    except Exception:
        return 0


def f(candidate: Json, key: str, default: float = 0.0) -> float:
    try:
        return float(candidate.get(key, default) or default)
    except Exception:
        return default


def same_family_close_count(candidate: Json, candidates: list[Json], *, delta: float = 0.08, window: int | None = None) -> int:
    product_id = str(candidate.get("product_id", ""))
    score = f(candidate, "score")
    count = 0
    for other in candidates:
        other_id = str(other.get("product_id", ""))
        if other_id == product_id:
            continue
        if abs(score - f(other, "score")) > delta:
            continue
        if jdb.same_candidate_family(product_id, other_id, numeric_window=window):
            count += 1
    return count


def rerank(candidates: list[Json], score_fn: ScoreFn) -> list[Json]:
    scored: list[Json] = []
    for candidate in candidates:
        item = dict(candidate)
        item["rerank_score"] = score_fn(item, candidates)
        scored.append(item)
    ranked = sorted(
        scored,
        key=lambda item: (
            f(item, "rerank_score"),
            f(item, "score"),
            f(item, "best_similarity"),
            clamp_count(item.get("evidence_count"), 100),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, start=1):
        item["rerank_rank"] = index
        next_score = f(ranked[index], "rerank_score") if index < len(ranked) else 0.0
        item["rerank_margin"] = f(item, "rerank_score") - next_score
    return ranked


def approaches() -> list[Approach]:
    return [
        Approach(
            "01_current_engine_score",
            "Current product score: 0.6*best similarity + 0.4*top-3 same-product mean.",
            lambda c, allc: f(c, "score"),
        ),
        Approach(
            "02_best_similarity_only",
            "Single best retrieved crop/image similarity.",
            lambda c, allc: f(c, "best_similarity", f(c, "score")),
        ),
        Approach(
            "03_top3_mean_only",
            "Mean of best up to three same-product similarities.",
            lambda c, allc: f(c, "mean_top3_similarity", f(c, "score")),
        ),
        Approach(
            "04_best_plus_evidence_bonus",
            "Best similarity plus small cap-limited evidence-count bonus.",
            lambda c, allc: f(c, "best_similarity", f(c, "score")) + 0.006 * clamp_count(c.get("evidence_count"), 6),
        ),
        Approach(
            "05_mean_plus_query_coverage",
            "Top-3 mean plus bonus for agreement across query crops.",
            lambda c, allc: f(c, "mean_top3_similarity", f(c, "score")) + 0.018 * clamp_count(c.get("query_crop_count"), 4),
        ),
        Approach(
            "06_balanced_consensus",
            "Balanced best/mean score with query/evidence/candidate coverage bonuses.",
            lambda c, allc: (
                0.50 * f(c, "best_similarity", f(c, "score"))
                + 0.35 * f(c, "mean_top3_similarity", f(c, "score"))
                + 0.012 * clamp_count(c.get("query_crop_count"), 4)
                + 0.006 * clamp_count(c.get("evidence_count"), 6)
                + 0.004 * clamp_count(c.get("candidate_crop_count"), 6)
            ),
        ),
        Approach(
            "07_penalize_singleton_spike",
            "Penalize one-off high-similarity spikes when same-product evidence is inconsistent.",
            lambda c, allc: (
                0.45 * f(c, "best_similarity", f(c, "score"))
                + 0.55 * f(c, "mean_top3_similarity", f(c, "score"))
                - (0.035 if clamp_count(c.get("evidence_count"), 99) <= 1 else 0.0)
                - (0.020 if f(c, "best_similarity") - f(c, "mean_top3_similarity") > 0.08 else 0.0)
            ),
        ),
        Approach(
            "08_asset_consensus",
            "Prioritize multiple candidate assets/crops and query-crop agreement.",
            lambda c, allc: (
                0.40 * f(c, "best_similarity", f(c, "score"))
                + 0.45 * f(c, "mean_top3_similarity", f(c, "score"))
                + 0.020 * clamp_count(c.get("candidate_crop_count"), 4)
                + 0.018 * clamp_count(c.get("query_crop_count"), 4)
            ),
        ),
        Approach(
            "09_dense_family_penalty_window2",
            "Current score minus penalty for close same-code-family competitors within +/-2.",
            lambda c, allc: f(c, "score") - 0.018 * min(same_family_close_count(c, allc, delta=0.08, window=2), 4),
        ),
        Approach(
            "10_consistency_and_density",
            "Reward consistent top evidence and coverage, penalize dense family ambiguity.",
            lambda c, allc: (
                0.35 * f(c, "best_similarity", f(c, "score"))
                + 0.55 * f(c, "mean_top3_similarity", f(c, "score"))
                + 0.014 * clamp_count(c.get("query_crop_count"), 4)
                + 0.008 * clamp_count(c.get("candidate_crop_count"), 5)
                - 0.012 * min(same_family_close_count(c, allc, delta=0.08, window=2), 4)
                - 0.015 * max(f(c, "best_similarity") - f(c, "mean_top3_similarity") - 0.05, 0.0) * 10
            ),
        ),
    ]


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
        candidates = jdb.aggregate_product_candidates(raw)
        cached.append(
            {
                "query_image_id": probe.image_id,
                "query_crop_id": probe.crop_id,
                "truth_product_id": probe.product_id,
                "candidates": candidates,
            }
        )
    metadata = {
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
    }
    return cached, metadata


def topk_metrics(name: str, description: str, cached: list[Json], score_fn: ScoreFn, args: argparse.Namespace) -> Json:
    total = len(cached)
    counts = Counter()
    auto = Counter()
    examples: list[Json] = []
    for item in cached:
        truth = item["truth_product_id"]
        ranked = rerank(item["candidates"], score_fn)
        rank = None
        for idx, candidate in enumerate(ranked, start=1):
            if candidate["product_id"] == truth:
                rank = idx
                break
        if rank == 1:
            counts["top1"] += 1
        if rank is not None and rank <= 3:
            counts["top3"] += 1
        if rank is not None and rank <= 5:
            counts["top5"] += 1
        if rank is None:
            counts["missing"] += 1
        top = ranked[0] if ranked else None
        if top and f(top, "rerank_score") >= args.auto_score and f(top, "rerank_margin") >= args.auto_margin:
            if top["product_id"] == truth:
                auto["correct"] += 1
            else:
                auto["wrong"] += 1
                if len(examples) < args.example_limit:
                    examples.append(
                        {
                            "truth_product_id": truth,
                            "selected_product_id": top["product_id"],
                            "query_image_id": item["query_image_id"],
                            "query_crop_id": item["query_crop_id"],
                            "selected_score": top["rerank_score"],
                            "selected_margin": top["rerank_margin"],
                            "truth_rank": rank,
                            "top_candidates": ranked[:5],
                        }
                    )
        else:
            auto["review_or_no_match"] += 1
    auto_total = auto["correct"] + auto["wrong"]
    result = {
        "approach": name,
        "description": description,
        "evaluated_probes": total,
        "top1": counts["top1"],
        "top3": counts["top3"],
        "top5": counts["top5"],
        "missing_correct_candidate": counts["missing"],
        "top1_accuracy": counts["top1"] / total if total else 0.0,
        "top3_recall": counts["top3"] / total if total else 0.0,
        "top5_recall": counts["top5"] / total if total else 0.0,
        "auto_score_threshold": args.auto_score,
        "auto_margin_threshold": args.auto_margin,
        "auto_total": auto_total,
        "auto_correct": auto["correct"],
        "auto_wrong": auto["wrong"],
        "review_or_no_match": auto["review_or_no_match"],
        "auto_precision": auto["correct"] / auto_total if auto_total else 0.0,
        "correct_auto_recall": auto["correct"] / total if total else 0.0,
        "wrong_auto_rate": auto["wrong"] / total if total else 0.0,
        "wrong_examples": examples,
    }
    result["very_good"] = bool(
        result["top1_accuracy"] >= args.very_good_top1
        and result["auto_precision"] >= args.very_good_precision
        and result["correct_auto_recall"] >= args.very_good_recall
        and result["auto_wrong"] <= args.very_good_max_wrong
    )
    return result


def rank_result(result: Json) -> tuple[float, float, float, float]:
    score = (
        100 * result["top1_accuracy"]
        + 80 * result["auto_precision"]
        + 60 * result["correct_auto_recall"]
        - 120 * result["wrong_auto_rate"]
    )
    return (score, result["top1_accuracy"], result["auto_precision"], result["correct_auto_recall"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--example-limit", type=int, default=20)
    parser.add_argument("--auto-score", type=float, default=0.93)
    parser.add_argument("--auto-margin", type=float, default=0.03)
    parser.add_argument("--very-good-top1", type=float, default=0.75)
    parser.add_argument("--very-good-precision", type=float, default=0.95)
    parser.add_argument("--very-good-recall", type=float, default=0.40)
    parser.add_argument("--very-good-max-wrong", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cached, metadata = build_probe_candidates(args)
    raw_results = [topk_metrics(a.name, a.description, cached, a.score_fn, args) for a in approaches()]
    ranked = sorted(raw_results, key=rank_result, reverse=True)
    best = ranked[0] if ranked else None
    report = {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "stored image/crop embeddings + candidate-side aggregate features only",
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "uses_truth_product_id_as_feature": False,
            "top_k": args.top_k,
        },
        **metadata,
        "success_gate": {
            "very_good_top1": args.very_good_top1,
            "very_good_precision": args.very_good_precision,
            "very_good_recall": args.very_good_recall,
            "very_good_max_wrong": args.very_good_max_wrong,
        },
        "best_approach": best,
        "very_good_found": any(r["very_good"] for r in ranked),
        "results": ranked,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "very_good_found": report["very_good_found"],
                "best": {
                    "approach": best["approach"] if best else None,
                    "top1_accuracy": best["top1_accuracy"] if best else None,
                    "top3_recall": best["top3_recall"] if best else None,
                    "top5_recall": best["top5_recall"] if best else None,
                    "auto_precision": best["auto_precision"] if best else None,
                    "correct_auto_recall": best["correct_auto_recall"] if best else None,
                    "auto_wrong": best["auto_wrong"] if best else None,
                    "auto_total": best["auto_total"] if best else None,
                },
                "top5": [
                    {
                        "approach": r["approach"],
                        "top1_accuracy": r["top1_accuracy"],
                        "top3_recall": r["top3_recall"],
                        "top5_recall": r["top5_recall"],
                        "auto_precision": r["auto_precision"],
                        "correct_auto_recall": r["correct_auto_recall"],
                        "auto_wrong": r["auto_wrong"],
                    }
                    for r in ranked[:5]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
