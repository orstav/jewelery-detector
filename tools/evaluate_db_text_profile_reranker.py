#!/usr/bin/env python3
"""Evaluate read-only VLM/text-profile reranking on catalog DB candidates.

This benchmark does not call external APIs and does not write to the database. It
uses stored image_profiles.profile_json when available. If a profile is missing,
it builds a deterministic proxy text profile from production-available embedding
metadata (view_type, crop_source, risk_flags), deliberately excluding filenames,
image IDs, crop IDs, and catalog/product IDs from reranking features.

Catalog product_id is used only for split labels and metric truth, matching the
existing DB evaluation harnesses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import jewelry_detector_db as jdb
from tools.evaluate_db_embedding_retrieval import EmbeddingRow, connect, hidden_products, query_candidates, read_catalog_embeddings

Json = dict[str, Any]
ScoreFn = Callable[[Json, list[Json]], float]

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*", re.IGNORECASE)
STOP_TOKENS = {
    "unknown",
    "uncertain",
    "image",
    "full",
    "crop",
    "view",
    "source",
    "true",
    "false",
    "none",
}


@dataclass(frozen=True)
class Approach:
    name: str
    description: str
    score_fn: ScoreFn


def f(candidate: Json, key: str, default: float = 0.0) -> float:
    try:
        return float(candidate.get(key, default) or default)
    except Exception:
        return default


def clamp_count(value: Any, cap: int) -> int:
    try:
        return min(max(int(value), 0), cap)
    except Exception:
        return 0


def tokenize_text(value: Any) -> set[str]:
    tokens = {match.group(0).lower() for match in TOKEN_RE.finditer(str(value))}
    return {token for token in tokens if len(token) > 2 and token not in STOP_TOKENS}


def profile_tokens(profile: Json | None) -> set[str]:
    """Extract production-safe visual/profile tokens from a stored VLM profile."""
    if not isinstance(profile, dict):
        return set()
    tokens: set[str] = set()
    for key in ("scene_type", "background_type", "recommended_evidence_policy"):
        tokens.update(tokenize_text(profile.get(key, "")))
    if profile.get("has_hand") is True:
        tokens.add("has_hand")
    if profile.get("has_person") is True:
        tokens.add("has_person")
    for flag in profile.get("quality_flags", []) or []:
        tokens.update(tokenize_text(flag))
    for item in profile.get("jewelry_items", []) or []:
        if not isinstance(item, dict):
            continue
        for key in ("type", "dominance", "object_completeness"):
            tokens.update(tokenize_text(item.get(key, "")))
        for feature in item.get("identity_features", []) or []:
            tokens.update(tokenize_text(feature))
    # Crop-profile-v1 rows store deterministic crop evidence under `crops`
    # rather than VLM jewelry_items. Treat those as weak text-profile tokens so
    # the evaluator measures all currently stored profile rows instead of
    # falling back to embedding-row metadata for every image.
    for crop in profile.get("crops", []) or []:
        if not isinstance(crop, dict):
            continue
        for key in ("view_type", "source", "crop_id_suffix"):
            tokens.update(tokenize_text(crop.get(key, "")))
        for flag in crop.get("risk_flags", []) or []:
            tokens.update(tokenize_text(flag))
    return tokens


def proxy_tokens_from_embedding_metadata(*, view_type: Any = "", crop_source: Any = "", risk_flags: Any = None) -> set[str]:
    """Build deterministic no-ID text-profile proxy tokens from crop metadata."""
    tokens = set()
    tokens.update(tokenize_text(view_type))
    tokens.update(tokenize_text(crop_source))
    for flag in risk_flags or []:
        tokens.update(tokenize_text(flag))
    return tokens


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def read_stored_profiles(url: str) -> dict[str, Json]:
    """Read latest stored profile per image_id. Read-only SELECT only."""
    sql = """
        SELECT DISTINCT ON (image_id) image_id, profile_json
        FROM image_profiles
        WHERE status = 'ready'
        ORDER BY image_id, created_at DESC, id DESC
    """
    try:
        with connect(url) as con, con.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as exc:
        # Older/local DBs might not have image_profiles. Keep evaluator usable
        # with deterministic proxy profiles rather than failing the benchmark.
        print(f"WARN: could not read image_profiles; using metadata proxies only: {exc}", file=sys.stderr)
        return {}
    profiles: dict[str, Json] = {}
    for image_id, raw_profile in rows:
        if isinstance(raw_profile, dict):
            profiles[str(image_id)] = raw_profile
    return profiles


def build_image_token_map(rows: list[EmbeddingRow], stored_profiles: dict[str, Json]) -> tuple[dict[str, set[str]], Json]:
    tokens_by_image: dict[str, set[str]] = {image_id: profile_tokens(profile) for image_id, profile in stored_profiles.items()}
    proxy_counts = Counter()
    for row in rows:
        image_id = str(row.image_id)
        if tokens_by_image.get(image_id):
            continue
        proxy_counts["images_with_proxy_tokens"] += 1 if image_id not in tokens_by_image else 0
        tokens_by_image.setdefault(image_id, set()).update(
            proxy_tokens_from_embedding_metadata(
                view_type=row.view_type,
                crop_source=row.crop_source,
                risk_flags=row.risk_flags or [],
            )
        )
    images_with_stored_tokens = sum(1 for image_id in {row.image_id for row in rows} if profile_tokens(stored_profiles.get(image_id)))
    images_with_tokens = sum(1 for image_id in {row.image_id for row in rows} if tokens_by_image.get(image_id))
    summary = {
        "stored_profiles_read": len(stored_profiles),
        "catalog_images": len({row.image_id for row in rows}),
        "images_with_stored_profile_tokens": images_with_stored_tokens,
        "images_with_any_text_profile_tokens": images_with_tokens,
        "fallback": "embedding_metadata_proxy_for_missing_or_empty_profiles",
    }
    return tokens_by_image, summary


def candidate_text_features(raw_rows: list[Json], query: EmbeddingRow, image_tokens: dict[str, set[str]]) -> dict[str, Json]:
    query_tokens = set(image_tokens.get(str(query.image_id), set()))
    by_product_rows: dict[str, list[Json]] = defaultdict(list)
    for row in raw_rows:
        by_product_rows[str(row["product_id"])].append(row)
    out: dict[str, Json] = {}
    for product_id, rows in by_product_rows.items():
        candidate_token_sets = []
        for row in rows:
            image_id = str(row.get("candidate_image_id", ""))
            candidate_tokens = set(image_tokens.get(image_id, set()))
            if not candidate_tokens:
                candidate_tokens = proxy_tokens_from_embedding_metadata(
                    view_type=row.get("candidate_view_type"),
                    crop_source=row.get("candidate_crop_source"),
                    risk_flags=row.get("candidate_risk_flags") or [],
                )
            candidate_token_sets.append(candidate_tokens)
        agreements = [jaccard(query_tokens, tokens) for tokens in candidate_token_sets if tokens]
        union_tokens: set[str] = set().union(*candidate_token_sets) if candidate_token_sets else set()
        out[product_id] = {
            "text_profile_agreement": max(agreements) if agreements else 0.0,
            "text_profile_mean_agreement": sum(agreements) / len(agreements) if agreements else 0.0,
            "query_text_token_count": len(query_tokens),
            "candidate_text_token_count": len(union_tokens),
            "text_profile_source": "stored_or_proxy",
        }
    return out


def rerank(candidates: list[Json], score_fn: ScoreFn) -> list[Json]:
    scored = []
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
            "Baseline current product consensus score, no text-profile feature.",
            lambda c, allc: f(c, "score"),
        ),
        Approach(
            "02_text_profile_agreement_only",
            "Stored/proxy text-profile token Jaccard only; diagnostic, not sufficient alone.",
            lambda c, allc: f(c, "text_profile_agreement"),
        ),
        Approach(
            "03_current_plus_small_text_bonus",
            "Current score plus small bounded text-profile agreement bonus.",
            lambda c, allc: f(c, "score") + 0.025 * f(c, "text_profile_agreement"),
        ),
        Approach(
            "04_current_plus_mean_text_bonus",
            "Current score plus candidate product mean text-profile agreement bonus.",
            lambda c, allc: f(c, "score") + 0.020 * f(c, "text_profile_mean_agreement"),
        ),
        Approach(
            "05_balanced_embedding_text",
            "Embedding consensus with moderate text-profile agreement and coverage bonus.",
            lambda c, allc: (
                0.92 * f(c, "score")
                + 0.08 * f(c, "text_profile_agreement")
                + 0.003 * min(clamp_count(c.get("query_text_token_count"), 20), clamp_count(c.get("candidate_text_token_count"), 20))
            ),
        ),
        Approach(
            "06_text_guarded_singleton_penalty",
            "Penalize singleton embedding spikes only when text-profile agreement is absent.",
            lambda c, allc: (
                f(c, "score")
                + 0.020 * f(c, "text_profile_agreement")
                - (0.025 if clamp_count(c.get("evidence_count"), 99) <= 1 and f(c, "text_profile_agreement") <= 0.0 else 0.0)
            ),
        ),
    ]


def build_probe_candidates(args: argparse.Namespace) -> tuple[list[Json], Json]:
    rows = read_catalog_embeddings(args.database_url, model=args.model, preprocess_version=args.preprocess_version)
    stored_profiles = read_stored_profiles(args.database_url)
    image_tokens, text_summary = build_image_token_map(rows, stored_profiles)
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
        features_by_product = candidate_text_features(raw, probe, image_tokens)
        for candidate in candidates:
            candidate.update(features_by_product.get(str(candidate["product_id"]), {}))
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
        "text_profile_summary": text_summary,
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
        counts["top1"] += int(rank == 1)
        counts["top3"] += int(rank is not None and rank <= 3)
        counts["top5"] += int(rank is not None and rank <= 5)
        counts["missing"] += int(rank is None)
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
    return {
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
            "algorithm_inputs": "stored DB image/crop embeddings + stored image_profiles.profile_json when available + deterministic embedding-metadata text proxies for missing profiles",
            "uses_external_api": False,
            "mutates_database": False,
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "uses_truth_product_id_as_feature": False,
            "top_k": args.top_k,
        },
        **metadata,
        "best_approach": best,
        "results": ranked,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "best": {
                    "approach": best["approach"] if best else None,
                    "top1_accuracy": best["top1_accuracy"] if best else None,
                    "top3_recall": best["top3_recall"] if best else None,
                    "top5_recall": best["top5_recall"] if best else None,
                    "auto_precision": best["auto_precision"] if best else None,
                    "correct_auto_recall": best["correct_auto_recall"] if best else None,
                    "auto_wrong": best["auto_wrong"] if best else None,
                },
                "text_profile_summary": report["text_profile_summary"],
                "top5": [
                    {
                        "approach": r["approach"],
                        "top1_accuracy": r["top1_accuracy"],
                        "top3_recall": r["top3_recall"],
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
