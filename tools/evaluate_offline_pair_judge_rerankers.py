#!/usr/bin/env python3
"""Offline Top-K pair-judge/reranker harness for jewelry detector candidates.

The harness consumes already-retrieved candidates (JSON cache) or, optionally,
builds that cache from read-only DB retrieval. It does not call external APIs,
does not mutate the detector DB, and does not use filenames or the query truth
product ID as algorithm inputs. Candidate product IDs are used only for metrics,
sibling-confusion diagnostics, and output labels.
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
JudgeFn = Callable[[Json, list[Json]], str]

CROP_VIEW_MARKERS = {
    "center_object",
    "detail_object",
    "vlm_context",
    "owlv2_padded",
    "owlv2_context",
    "center50",
    "center70",
    "crop",
}


@dataclass(frozen=True)
class Proxy:
    name: str
    description: str
    score_fn: ScoreFn
    judge_fn: JudgeFn


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except Exception:
        return default


def cap(value: Any, maximum: int) -> int:
    return min(max(as_int(value), 0), maximum)


def candidate_score(candidate: Json) -> float:
    return as_float(candidate.get("score", candidate.get("similarity", candidate.get("best_similarity", 0.0))))


def best_similarity(candidate: Json) -> float:
    return as_float(candidate.get("best_similarity", candidate_score(candidate)))


def mean_top3(candidate: Json) -> float:
    return as_float(candidate.get("mean_top3_similarity", candidate_score(candidate)))


def top_gap(candidate: Json) -> float:
    return max(best_similarity(candidate) - mean_top3(candidate), 0.0)


def close_score_competitors(candidate: Json, candidates: list[Json], delta: float) -> int:
    score = candidate_score(candidate)
    product_id = str(candidate.get("product_id", ""))
    return sum(
        1
        for other in candidates
        if str(other.get("product_id", "")) != product_id and abs(score - candidate_score(other)) <= delta
    )


def current_score(candidate: Json, _candidates: list[Json]) -> float:
    return candidate_score(candidate)


def best_only(candidate: Json, _candidates: list[Json]) -> float:
    return best_similarity(candidate)


def consensus_score(candidate: Json, _candidates: list[Json]) -> float:
    return (
        0.44 * best_similarity(candidate)
        + 0.46 * mean_top3(candidate)
        + 0.018 * cap(candidate.get("query_crop_count"), 4)
        + 0.010 * cap(candidate.get("candidate_crop_count"), 5)
        + 0.006 * cap(candidate.get("evidence_count"), 6)
        - 0.12 * top_gap(candidate)
    )


def strict_pair_score(candidate: Json, candidates: list[Json]) -> float:
    return consensus_score(candidate, candidates) - 0.018 * min(close_score_competitors(candidate, candidates, 0.035), 4)


def score_floor_judge(candidate: Json, _candidates: list[Json]) -> str:
    if best_similarity(candidate) >= 0.94 and candidate_score(candidate) >= 0.91:
        return "same_product"
    if best_similarity(candidate) < 0.84:
        return "different"
    return "unsure"


def consensus_judge(candidate: Json, candidates: list[Json]) -> str:
    del candidates
    evidence = cap(candidate.get("evidence_count"), 99)
    query_views = cap(candidate.get("query_crop_count"), 99)
    candidate_views = cap(candidate.get("candidate_crop_count"), 99)
    if candidate_score(candidate) >= 0.89 and mean_top3(candidate) >= 0.87 and evidence >= 2 and top_gap(candidate) <= 0.075:
        return "same_product"
    if candidate_score(candidate) >= 0.87 and query_views >= 2 and candidate_views >= 2 and top_gap(candidate) <= 0.055:
        return "same_product"
    if best_similarity(candidate) < 0.82 or (evidence <= 1 and top_gap(candidate) > 0.10):
        return "different"
    return "unsure"


def ambiguity_aware_judge(candidate: Json, candidates: list[Json]) -> str:
    base = consensus_judge(candidate, candidates)
    if base != "same_product":
        return base
    # Pair-judge proxy: if many candidate products have nearly identical scores,
    # treat the pair as unresolved rather than auto-match. This does not use
    # product IDs for scoring; only score density among already retrieved items.
    if close_score_competitors(candidate, candidates, 0.025) >= 2:
        return "unsure"
    return "same_product"


def proxies() -> list[Proxy]:
    return [
        Proxy("01_current_score", "Current product aggregate score; judge is score floor only.", current_score, score_floor_judge),
        Proxy("02_best_similarity", "Single best retrieved candidate similarity; judge is score floor only.", best_only, score_floor_judge),
        Proxy("03_consensus_pair_judge", "Best/mean/evidence deterministic pair-judge proxy.", consensus_score, consensus_judge),
        Proxy("04_ambiguity_aware_pair_judge", "Consensus proxy with close-score ambiguity routed to review.", strict_pair_score, ambiguity_aware_judge),
    ]


def infer_split(item: Json, ranked: list[Json]) -> str:
    explicit = str(item.get("split") or item.get("query_split") or item.get("scene_split") or "").lower()
    if explicit in {"live", "studio"}:
        return explicit
    texts: list[str] = []
    for key in ("query_view_type", "query_crop_id", "query_crop_source", "query_preprocess_version"):
        if item.get(key) is not None:
            texts.append(str(item[key]).lower())
    for candidate in ranked[:5]:
        for key in ("query_view_type", "query_crop_id", "query_crop_source"):
            if candidate.get(key) is not None:
                texts.append(str(candidate[key]).lower())
        for flag in candidate.get("risk_flags", []) or []:
            texts.append(str(flag).lower())
    joined = " ".join(texts)
    if any(marker in joined for marker in CROP_VIEW_MARKERS) or "live" in joined or "model" in joined:
        return "live"
    return "studio"


def rerank(candidates: list[Json], proxy: Proxy) -> list[Json]:
    scored: list[Json] = []
    for candidate in candidates:
        item = dict(candidate)
        item["proxy_score"] = proxy.score_fn(item, candidates)
        item["pair_judge_decision"] = proxy.judge_fn(item, candidates)
        scored.append(item)
    ranked = sorted(
        scored,
        key=lambda item: (
            as_float(item.get("proxy_score")),
            candidate_score(item),
            best_similarity(item),
            cap(item.get("evidence_count"), 100),
        ),
        reverse=True,
    )
    for idx, item in enumerate(ranked, start=1):
        item["proxy_rank"] = idx
        item["proxy_margin"] = as_float(item["proxy_score"]) - (as_float(ranked[idx].get("proxy_score")) if idx < len(ranked) else 0.0)
    return ranked


def family_relation(left: str, right: str, window: int) -> bool:
    if not left or not right or left == right:
        return False
    return jdb.same_candidate_family(left, right, numeric_window=window)


def metric_bucket() -> Json:
    return {
        "evaluated_probes": 0,
        "top1": 0,
        "top3": 0,
        "top5": 0,
        "missing_correct_candidate": 0,
        "auto_total": 0,
        "auto_correct": 0,
        "auto_wrong": 0,
        "review_or_no_match": 0,
        "same_design_sibling_cases": 0,
        "same_design_sibling_top1_wrong": 0,
        "dense_score_ambiguity_cases": 0,
    }


def finalize_bucket(bucket: Json) -> Json:
    total = as_int(bucket["evaluated_probes"])
    auto_total = as_int(bucket["auto_total"])
    bucket["top1_accuracy"] = bucket["top1"] / total if total else 0.0
    bucket["top3_recall"] = bucket["top3"] / total if total else 0.0
    bucket["top5_recall"] = bucket["top5"] / total if total else 0.0
    bucket["auto_precision"] = bucket["auto_correct"] / auto_total if auto_total else 0.0
    bucket["correct_auto_recall"] = bucket["auto_correct"] / total if total else 0.0
    bucket["wrong_auto_rate"] = bucket["auto_wrong"] / total if total else 0.0
    bucket["sibling_top1_wrong_rate"] = bucket["same_design_sibling_top1_wrong"] / bucket["same_design_sibling_cases"] if bucket["same_design_sibling_cases"] else 0.0
    return bucket


def update_bucket(bucket: Json, item: Json, ranked: list[Json], args: argparse.Namespace, examples: list[Json]) -> None:
    truth = str(item.get("truth_product_id") or "")
    if not truth:
        return
    bucket["evaluated_probes"] += 1
    rank = next((idx for idx, candidate in enumerate(ranked, start=1) if str(candidate.get("product_id")) == truth), None)
    if rank == 1:
        bucket["top1"] += 1
    if rank is not None and rank <= 3:
        bucket["top3"] += 1
    if rank is not None and rank <= 5:
        bucket["top5"] += 1
    if rank is None:
        bucket["missing_correct_candidate"] += 1
    top = ranked[0] if ranked else None
    if not top:
        bucket["review_or_no_match"] += 1
        return
    close_family = [c for c in ranked[: args.sibling_window_top_k] if family_relation(str(c.get("product_id", "")), truth, args.sibling_numeric_window)]
    if close_family:
        bucket["same_design_sibling_cases"] += 1
    if close_score_competitors(top, ranked[: args.sibling_window_top_k], args.close_score_delta) >= args.close_score_min_competitors:
        bucket["dense_score_ambiguity_cases"] += 1
    auto_match = (
        top.get("pair_judge_decision") == "same_product"
        and as_float(top.get("proxy_score")) >= args.auto_score
        and as_float(top.get("proxy_margin")) >= args.auto_margin
    )
    if auto_match:
        bucket["auto_total"] += 1
        if str(top.get("product_id")) == truth:
            bucket["auto_correct"] += 1
        else:
            bucket["auto_wrong"] += 1
            if close_family:
                bucket["same_design_sibling_top1_wrong"] += 1
            if len(examples) < args.example_limit:
                examples.append(
                    {
                        "query_image_id": item.get("query_image_id"),
                        "query_crop_id": item.get("query_crop_id"),
                        "truth_product_id": truth,
                        "selected_product_id": top.get("product_id"),
                        "truth_rank": rank,
                        "split": infer_split(item, ranked),
                        "selected_proxy_score": top.get("proxy_score"),
                        "selected_proxy_margin": top.get("proxy_margin"),
                        "same_design_sibling_near_truth": [c.get("product_id") for c in close_family[:5]],
                        "top_candidates": ranked[:5],
                    }
                )
    else:
        bucket["review_or_no_match"] += 1


def evaluate_proxy(cached: list[Json], proxy: Proxy, args: argparse.Namespace) -> Json:
    overall = metric_bucket()
    by_split: dict[str, Json] = defaultdict(metric_bucket)
    examples: list[Json] = []
    for item in cached:
        ranked = rerank(list(item.get("candidates") or []), proxy)
        split = infer_split(item, ranked)
        update_bucket(overall, item, ranked, args, examples)
        update_bucket(by_split[split], item, ranked, args, [])
    result = {
        "approach": proxy.name,
        "description": proxy.description,
        **finalize_bucket(overall),
        "split_metrics": {name: finalize_bucket(bucket) for name, bucket in sorted(by_split.items())},
        "wrong_auto_examples": examples,
    }
    split_metrics = result["split_metrics"]
    result["safe_to_deploy"] = bool(
        as_int(result["evaluated_probes"]) >= args.min_eval_probes
        and as_int(result["auto_total"]) > 0
        and as_float(result["auto_precision"]) >= args.safe_auto_precision
        and as_float(result["correct_auto_recall"]) >= args.safe_auto_recall
        and as_int(result["auto_wrong"]) <= args.safe_max_wrong
        and isinstance(split_metrics, dict)
        and all(as_int(split["auto_wrong"]) <= args.safe_max_wrong_per_split for split in split_metrics.values())
        and as_int(result["same_design_sibling_top1_wrong"]) <= args.safe_max_sibling_wrong
    )
    return result


def load_candidate_cache(path: Path) -> tuple[list[Json], Json]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata: Json = {"source": str(path)}
    if isinstance(payload, list):
        return payload, metadata
    if isinstance(payload, dict):
        metadata.update({k: v for k, v in payload.items() if k in {"split", "db_summary", "inputs", "schema_version"}})
        if isinstance(payload.get("probes"), list):
            return payload["probes"], metadata
        if isinstance(payload.get("candidate_cache"), list):
            return payload["candidate_cache"], metadata
        if isinstance(payload.get("items"), list):
            return payload["items"], metadata
        # Best-effort support for earlier crop-eval reports: examples contain
        # existing Top-K candidates but are not full probe coverage.
        examples = []
        for result in payload.get("results", []) or []:
            for example in result.get("examples", []) or []:
                if isinstance(example.get("top_candidates"), list):
                    examples.append(
                        {
                            "query_image_id": example.get("query_image_id"),
                            "query_crop_id": example.get("query_crop_id"),
                            "truth_product_id": example.get("truth_product_id"),
                            "query_split": "live" if any(v != "full_image" for v in result.get("view_types", [])) else "studio",
                            "candidates": example["top_candidates"],
                        }
                    )
        if examples:
            metadata["partial_examples_only"] = True
            return examples, metadata
    raise ValueError(f"unsupported candidate cache shape: {path}")


def build_db_candidate_cache(args: argparse.Namespace) -> tuple[list[Json], Json]:
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
                "query_view_type": probe.view_type,
                "query_crop_source": probe.crop_source,
                "truth_product_id": probe.product_id,
                "candidates": candidates,
            }
        )
    metadata = {
        "source": "read_only_db_retrieval",
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
            "view_types": dict(sorted(Counter(row.view_type for row in rows).items())),
            "crop_sources": dict(sorted(Counter(row.crop_source for row in rows).items())),
        },
    }
    return cached, metadata


def rank_result(result: Json) -> tuple[float, float, float, float]:
    return (
        120 * as_float(result["auto_precision"])
        + 100 * as_float(result["top1_accuracy"])
        + 80 * as_float(result["correct_auto_recall"])
        - 160 * as_float(result["wrong_auto_rate"])
        - 30 * as_float(result["sibling_top1_wrong_rate"]),
        as_float(result["auto_precision"]),
        as_float(result["top1_accuracy"]),
        as_float(result["correct_auto_recall"]),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidate-cache", help="JSON file with existing retrieved candidates")
    source.add_argument("--database-url", help="optional read-only DB source used only to build an offline candidate cache")
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-k", type=int, default=80)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--example-limit", type=int, default=20)
    parser.add_argument("--auto-score", type=float, default=0.90)
    parser.add_argument("--auto-margin", type=float, default=0.025)
    parser.add_argument("--close-score-delta", type=float, default=0.035)
    parser.add_argument("--close-score-min-competitors", type=int, default=2)
    parser.add_argument("--sibling-window-top-k", type=int, default=5)
    parser.add_argument("--sibling-numeric-window", type=int, default=2)
    parser.add_argument("--min-eval-probes", type=int, default=100)
    parser.add_argument("--safe-auto-precision", type=float, default=0.97)
    parser.add_argument("--safe-auto-recall", type=float, default=0.20)
    parser.add_argument("--safe-max-wrong", type=int, default=0)
    parser.add_argument("--safe-max-wrong-per-split", type=int, default=0)
    parser.add_argument("--safe-max-sibling-wrong", type=int, default=0)
    parser.add_argument("--write-candidate-cache", help="optional path to save the DB-built candidate cache")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.candidate_cache:
        cached, metadata = load_candidate_cache(Path(args.candidate_cache))
    else:
        cached, metadata = build_db_candidate_cache(args)
        if args.write_candidate_cache:
            cache_path = Path(args.write_candidate_cache)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"schema_version": "1.0", "probes": cached, **metadata}, ensure_ascii=False, indent=2), encoding="utf-8")
    results = sorted([evaluate_proxy(cached, proxy, args) for proxy in proxies()], key=rank_result, reverse=True)
    best = results[0] if results else None
    report = {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "existing Top-K retrieved candidate aggregate features only",
            "uses_external_api": False,
            "mutates_database": False,
            "uses_filename_tokens": False,
            "uses_truth_product_id_as_feature": False,
            "uses_candidate_product_id_for_scoring": False,
            "candidate_product_id_use": "evaluation labels, sibling diagnostics, and output labels only",
            "top_k": args.top_k,
        },
        "candidate_count": sum(len(item.get("candidates") or []) for item in cached),
        "probe_count": len(cached),
        "metadata": metadata,
        "safe_gate": {
            "min_eval_probes": args.min_eval_probes,
            "safe_auto_precision": args.safe_auto_precision,
            "safe_auto_recall": args.safe_auto_recall,
            "safe_max_wrong": args.safe_max_wrong,
            "safe_max_wrong_per_split": args.safe_max_wrong_per_split,
            "safe_max_sibling_wrong": args.safe_max_sibling_wrong,
        },
        "best_approach": best,
        "safe_to_deploy_found": any(result["safe_to_deploy"] for result in results),
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "probe_count": report["probe_count"],
                "candidate_count": report["candidate_count"],
                "safe_to_deploy_found": report["safe_to_deploy_found"],
                "best": {
                    "approach": best.get("approach") if best else None,
                    "top1_accuracy": best.get("top1_accuracy") if best else None,
                    "top5_recall": best.get("top5_recall") if best else None,
                    "auto_precision": best.get("auto_precision") if best else None,
                    "correct_auto_recall": best.get("correct_auto_recall") if best else None,
                    "auto_wrong": best.get("auto_wrong") if best else None,
                    "safe_to_deploy": best.get("safe_to_deploy") if best else None,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
