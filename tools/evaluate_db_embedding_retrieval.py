#!/usr/bin/env python3
"""Evaluate current pgvector embedding retrieval on catalog products.

This is a read-only benchmark: it uses catalog product_id labels only as truth,
not as production inputs. Queries are image/crop embeddings already stored in
Postgres. Filename tokens and probe catalog prefixes are not used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import jewelry_detector_db as jdb


Json = dict[str, Any]


@dataclass(frozen=True)
class EmbeddingRow:
    embedding_id: int
    product_id: str
    image_id: str
    crop_id: str
    embedding: str
    embedding_model: str
    preprocess_version: str
    embedding_dim: int
    view_type: str = "unknown"
    crop_source: str = "unknown"
    risk_flags: list[str] | None = None


def connect(url: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit("Install psycopg[binary] or run with: uv run --with 'psycopg[binary]' ...") from exc
    return psycopg.connect(url)


def read_catalog_embeddings(url: str, *, model: str | None = None, preprocess_version: str | None = None) -> list[EmbeddingRow]:
    clauses = ["active = true", "product_id IS NOT NULL"]
    params: list[Any] = []
    if model:
        clauses.append("embedding_model = %s")
        params.append(model)
    if preprocess_version:
        clauses.append("preprocess_version = %s")
        params.append(preprocess_version)
    sql = f"""
        SELECT id, product_id, image_id, crop_id, embedding::text,
               embedding_model, preprocess_version, embedding_dim,
               COALESCE(view_type, 'unknown') AS view_type,
               COALESCE(crop_source, 'unknown') AS crop_source,
               COALESCE(risk_flags, '[]'::jsonb) AS risk_flags
        FROM image_embeddings
        WHERE {' AND '.join(clauses)}
        ORDER BY product_id, image_id, crop_id
    """
    with connect(url) as con, con.cursor() as cur:
        cur.execute(sql, params)
        return [EmbeddingRow(*row) for row in cur.fetchall()]


def hidden_products(product_ids: list[str], ratio: float, seed: int) -> set[str]:
    ids = sorted(product_ids)
    if not ids or ratio <= 0:
        return set()
    rng = random.Random(seed)
    shuffled = ids[:]
    rng.shuffle(shuffled)
    count = max(1, round(len(ids) * ratio))
    return set(sorted(shuffled[:count]))


def query_candidates(url: str, query: EmbeddingRow, *, ref_products: set[str], top_k: int) -> list[Json]:
    if not ref_products:
        return []
    with connect(url) as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT id, product_id, image_id, crop_id,
                   COALESCE(view_type, 'unknown') AS view_type,
                   COALESCE(crop_source, 'unknown') AS crop_source,
                   COALESCE(risk_flags, '[]'::jsonb) AS risk_flags,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM image_embeddings
            WHERE active = true
              AND product_id = ANY(%s)
              AND image_id <> %s
              AND embedding_model = %s
              AND preprocess_version = %s
              AND embedding_dim = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (
                query.embedding,
                sorted(ref_products),
                query.image_id,
                query.embedding_model,
                query.preprocess_version,
                query.embedding_dim,
                query.embedding,
                top_k,
            ),
        )
        rows = []
        for rank, row in enumerate(cur.fetchall(), start=1):
            rows.append(
                {
                    "query_crop_id": query.crop_id,
                    "query_view_type": query.view_type,
                    "query_crop_source": query.crop_source,
                    "query_risk_flags": query.risk_flags or [],
                    "embedding_id": row[0],
                    "product_id": row[1],
                    "candidate_image_id": row[2],
                    "candidate_crop_id": row[3],
                    "candidate_view_type": row[4],
                    "candidate_crop_source": row[5],
                    "candidate_risk_flags": row[6] or [],
                    "rank": rank,
                    "similarity": float(row[7]),
                }
            )
        return rows


def old_aggregate(rows: list[Json]) -> list[Json]:
    grouped: dict[str, Json] = {}
    for row in rows:
        pid = str(row["product_id"])
        sim = float(row["similarity"])
        if pid not in grouped or sim > float(grouped[pid]["score"]):
            grouped[pid] = {
                "product_id": pid,
                "embedding_id": row["embedding_id"],
                "score": sim,
                "similarity": sim,
                "best_crop_id": row["candidate_crop_id"],
                "query_crop_id": row["query_crop_id"],
                "risk_flags": row.get("query_risk_flags", []),
            }
    ranked = sorted(grouped.values(), key=lambda item: float(item["score"]), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
        next_score = float(ranked[index]["score"]) if index < len(ranked) else 0.0
        item["margin"] = float(item["score"]) - next_score
    return ranked


def metric_row(name: str, probes: list[Json]) -> Json:
    evaluated = len(probes)
    top1 = sum(1 for p in probes if p["rank"] == 1)
    top3 = sum(1 for p in probes if p["rank"] is not None and p["rank"] <= 3)
    top5 = sum(1 for p in probes if p["rank"] is not None and p["rank"] <= 5)
    missing = sum(1 for p in probes if p["rank"] is None)
    return {
        "approach": name,
        "evaluated_probes": evaluated,
        "top1": top1,
        "top1_accuracy": top1 / evaluated if evaluated else 0.0,
        "top3": top3,
        "top3_recall": top3 / evaluated if evaluated else 0.0,
        "top5": top5,
        "top5_recall": top5 / evaluated if evaluated else 0.0,
        "missing_correct_candidate": missing,
    }


def evaluate(url: str, args: argparse.Namespace) -> Json:
    rows = read_catalog_embeddings(url, model=args.model, preprocess_version=args.preprocess_version)
    product_ids = sorted({r.product_id for r in rows})
    hidden = hidden_products(product_ids, args.hidden_ratio, args.seed)
    dev_products = set(product_ids) - hidden

    by_product: dict[str, list[EmbeddingRow]] = {}
    for row in rows:
        by_product.setdefault(row.product_id, []).append(row)

    eligible = [r for r in rows if r.product_id in dev_products and len(by_product.get(r.product_id, [])) >= 2]
    if args.max_probes:
        eligible = eligible[: args.max_probes]

    probes_old: list[Json] = []
    probes_new: list[Json] = []
    examples: list[Json] = []
    for query in eligible:
        raw = query_candidates(url, query, ref_products=dev_products, top_k=args.top_k)
        for approach, aggregate, dest in [
            ("old_single_best_crop", old_aggregate, probes_old),
            ("new_product_consensus", jdb.aggregate_product_candidates, probes_new),
        ]:
            ranked = aggregate(raw)
            rank = None
            top = ranked[0] if ranked else None
            for idx, cand in enumerate(ranked, start=1):
                if cand["product_id"] == query.product_id:
                    rank = idx
                    break
            dest.append(
                {
                    "query_image_id": query.image_id,
                    "query_crop_id": query.crop_id,
                    "truth_product_id": query.product_id,
                    "rank": rank,
                    "top_product_id": top.get("product_id") if top else None,
                    "top_score": top.get("score") if top else None,
                    "top_margin": top.get("margin") if top else None,
                }
            )
            if approach == "new_product_consensus" and rank != 1 and len(examples) < args.example_limit:
                examples.append(dest[-1] | {"top_candidates": ranked[:5]})

    results = [metric_row("old_single_best_crop", probes_old), metric_row("new_product_consensus", probes_new)]
    return {
        "schema_version": "1.0",
        "inputs": {
            "algorithm_inputs": "stored image embeddings only; catalog product_id used only as evaluation truth/split label",
            "uses_filename_tokens": False,
            "uses_probe_catalog_id_as_feature": False,
            "top_k": args.top_k,
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
            "images": len({r.image_id for r in rows}),
            "models": sorted({r.embedding_model for r in rows}),
            "preprocess_versions": sorted({r.preprocess_version for r in rows}),
        },
        "results": results,
        "examples": examples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--model")
    parser.add_argument("--preprocess-version")
    parser.add_argument("--max-probes", type=int)
    parser.add_argument("--example-limit", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args.database_url, args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "results": report["results"], "split": report["split"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
