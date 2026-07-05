#!/usr/bin/env python3
"""Evaluate the active detector DB policy on catalog images without writes.

This is a production-realistic smoke/eval harness for the DB runtime policy:
- product_id is used only as the evaluation label/split;
- filenames/source paths are used only for weak shot-role routing, mirroring the
  current live/studio profile gate where available;
- no detector DB writes are performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import jewelry_detector_db as jdb  # noqa: E402

Json = dict[str, Any]

LIVE_TOKENS = ("lifestyle", "model", "frontal", "frontal_", "on_model", "wear", "worn")
STUDIO_TOKENS = ("front", "side", "angled", "print", "web", "png", "crop", "high_res", "old")


def infer_shot_role(source_uri: str) -> str:
    text = source_uri.lower()
    if any(token in text for token in LIVE_TOKENS):
        return "live"
    if any(token in text for token in STUDIO_TOKENS):
        return "studio"
    return "unknown"


def hidden_products(product_ids: list[str], ratio: float, seed: int) -> set[str]:
    ids = sorted(product_ids)
    if not ids or ratio <= 0:
        return set()
    rng = random.Random(seed)
    shuffled = ids[:]
    rng.shuffle(shuffled)
    count = max(1, round(len(ids) * ratio))
    return set(shuffled[:count])


def parse_vector_text(value: str) -> list[float]:
    return [float(part) for part in value.strip("[]").split(",") if part]


def read_active_embeddings(url: str) -> list[Json]:
    with jdb.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.product_id, e.image_id, e.crop_id, e.view_type,
                   e.crop_source, COALESCE(e.risk_flags, '[]'::jsonb),
                   e.embedding::text, e.embedding_model, e.preprocess_version,
                   e.embedding_dim, e.source_sha256, p.source_uri
            FROM image_embeddings e
            JOIN product_images p ON p.image_id = e.image_id
            WHERE e.active = true
              AND e.product_id IS NOT NULL
              AND e.embedding_dim = 768
            ORDER BY e.product_id, e.image_id, e.view_type, e.crop_id
            """
        )
        rows = []
        for row in cur.fetchall():
            rows.append(
                {
                    "embedding_id": int(row[0]),
                    "product_id": str(row[1]),
                    "image_id": str(row[2]),
                    "crop_id": str(row[3]),
                    "view_type": str(row[4]),
                    "crop_source": str(row[5]),
                    "risk_flags": list(row[6] or []),
                    "embedding": str(row[7]),
                    "embedding_model": str(row[8]),
                    "preprocess_version": str(row[9]),
                    "embedding_dim": int(row[10]),
                    "source_sha256": str(row[11]),
                    "source_uri": str(row[12]),
                    "shot_role": infer_shot_role(str(row[12])),
                }
            )
        return rows


def payload_for_image(image_rows: list[Json], *, mode: str) -> Json:
    full = next((row for row in image_rows if row["view_type"] == "full_image"), image_rows[0])
    shot_role = str(full["shot_role"])
    if mode == "force_live":
        profile = {"profile_scene_type": "model_lifestyle", "profile_has_person": True, "profile_has_hand": False}
    elif mode == "force_studio":
        profile = {"profile_scene_type": "clean_product", "profile_has_person": False, "profile_has_hand": False}
    elif shot_role == "live":
        profile = {"profile_scene_type": "model_lifestyle", "profile_has_person": True, "profile_has_hand": False}
    else:
        profile = {"profile_scene_type": "clean_product", "profile_has_person": False, "profile_has_hand": False}
    return {
        "schema_version": "1.0",
        "status": "ready",
        "image_id": full["image_id"],
        "source_uri": full["source_uri"],
        "source_sha256": full["source_sha256"],
        "embedding_model": full["embedding_model"],
        "preprocess_version": full["preprocess_version"],
        "embedding_dim": full["embedding_dim"],
        **profile,
        "crops": [
            {
                "crop_id": row["crop_id"],
                "view_type": row["view_type"],
                "box": [0, 0, 1, 1],
                "source": row["crop_source"],
                "risk_flags": row["risk_flags"],
                "usable_for_retrieval": True,
                "embedding": parse_vector_text(row["embedding"]),
            }
            for row in image_rows
        ],
    }


def query_runtime_candidates(url: str, payload: Json, policy: Json, ref_products: set[str], top_k: int) -> list[Json]:
    effective = jdb.effective_candidate_policy(payload, policy)
    preprocess_versions = jdb.policy_preprocess_versions(effective)
    active_states = jdb.policy_active_states(effective)
    view_types = jdb.policy_view_types(effective)
    view_filter = "AND view_type = ANY(%s)" if view_types else ""
    rows: list[Json] = []
    with jdb.connect(url) as conn, conn.cursor() as cur:
        for crop in payload.get("crops", []):
            if view_types and str(crop.get("view_type")) not in set(view_types):
                continue
            params: list[Any] = [
                jdb.vector_literal(crop["embedding"]),
                active_states,
                sorted(ref_products),
                str(payload["image_id"]),
                policy["embedding_model"],
                preprocess_versions,
                int(payload["embedding_dim"]),
            ]
            if view_types:
                params.append(view_types)
            params.extend([jdb.vector_literal(crop["embedding"]), int(top_k)])
            cur.execute(
                f"""
                SELECT id, product_id, image_id, crop_id, view_type, preprocess_version, active,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM image_embeddings
                WHERE active = ANY(%s)
                  AND product_id = ANY(%s)
                  AND image_id <> %s
                  AND embedding_model = %s
                  AND preprocess_version = ANY(%s)
                  AND embedding_dim = %s
                  {view_filter}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                tuple(params),
            )
            for rank, row in enumerate(cur.fetchall(), start=1):
                rows.append(
                    {
                        "query_crop_id": crop["crop_id"],
                        "query_view_type": crop["view_type"],
                        "query_risk_flags": crop.get("risk_flags", []),
                        "embedding_id": int(row[0]),
                        "product_id": str(row[1]),
                        "candidate_image_id": str(row[2]),
                        "candidate_crop_id": str(row[3]),
                        "candidate_view_type": str(row[4]),
                        "candidate_preprocess_version": str(row[5]),
                        "candidate_active": bool(row[6]),
                        "rank": rank,
                        "similarity": float(row[7]),
                    }
                )
    return jdb.aggregate_product_candidates(rows)


def summarize_probe(truth: str, ranked: list[Json]) -> Json:
    rank = None
    for index, candidate in enumerate(ranked, start=1):
        if str(candidate["product_id"]) == truth:
            rank = index
            break
    top = ranked[0] if ranked else {}
    return {
        "rank": rank,
        "top_product_id": top.get("product_id"),
        "top_score": top.get("score"),
        "top_candidate_policy_mode": None,
        "top_candidates": ranked[:5],
    }


def metric(name: str, probes: list[Json]) -> Json:
    total = len(probes)
    top1 = sum(1 for probe in probes if probe["rank"] == 1)
    top3 = sum(1 for probe in probes if probe["rank"] is not None and probe["rank"] <= 3)
    top5 = sum(1 for probe in probes if probe["rank"] is not None and probe["rank"] <= 5)
    return {
        "approach": name,
        "evaluated_probes": total,
        "top1_accuracy": top1 / total if total else 0.0,
        "top3_recall": top3 / total if total else 0.0,
        "top5_recall": top5 / total if total else 0.0,
        "missing_correct_candidate": sum(1 for probe in probes if probe["rank"] is None),
    }


def evaluate(args: argparse.Namespace) -> Json:
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
    image_groups = [items for image_id, items in sorted(by_image.items()) if items[0]["product_id"] in dev_products and len(by_product_images[items[0]["product_id"]]) >= 2]
    if args.shot_role != "any":
        image_groups = [items for items in image_groups if items[0]["shot_role"] == args.shot_role]
    if args.max_probes:
        image_groups = image_groups[: args.max_probes]
    modes = ["runtime_live_gate", "force_studio", "force_live"]
    probes_by_mode: dict[str, list[Json]] = {mode: [] for mode in modes}
    examples: dict[str, list[Json]] = {mode: [] for mode in modes}
    policy_modes = Counter()
    for image_rows in image_groups:
        truth = str(image_rows[0]["product_id"])
        for mode in modes:
            payload = payload_for_image(image_rows, mode=mode)
            effective = jdb.effective_candidate_policy(payload, policy)
            policy_modes[f"{mode}:{effective.get('candidate_policy_mode')}"] += 1
            ranked = query_runtime_candidates(args.database_url, payload, policy, dev_products, args.top_k)
            probe = summarize_probe(truth, ranked)
            probe.update(
                {
                    "query_image_id": image_rows[0]["image_id"],
                    "truth_product_id": truth,
                    "shot_role": image_rows[0]["shot_role"],
                    "candidate_policy_mode": effective.get("candidate_policy_mode"),
                    "versions": effective.get("preprocess_versions"),
                    "view_types": effective.get("view_types"),
                }
            )
            probes_by_mode[mode].append(probe)
            if probe["rank"] != 1 and len(examples[mode]) < 12:
                examples[mode].append(probe)
    metrics = [metric(mode, probes_by_mode[mode]) for mode in modes]
    baseline = next(row for row in metrics if row["approach"] == "force_studio")
    for row in metrics:
        row["delta_top1_vs_force_studio"] = row["top1_accuracy"] - baseline["top1_accuracy"]
        row["delta_top5_vs_force_studio"] = row["top5_recall"] - baseline["top5_recall"]
    return {
        "schema_version": "active-policy-retrieval-eval-v1",
        "inputs": {
            "shot_role": args.shot_role,
            "hidden_ratio": args.hidden_ratio,
            "seed": args.seed,
            "top_k": args.top_k,
            "writes_detector_db": False,
            "uses_filename_tokens_for_matching": False,
            "uses_product_id_as_evaluation_label_only": True,
        },
        "split": {
            "total_products": len(product_ids),
            "dev_products": len(dev_products),
            "hidden_products": len(hidden),
            "hidden_products_sha256": hashlib.sha256("\n".join(sorted(hidden)).encode()).hexdigest(),
            "evaluated_probes": len(image_groups),
            "shot_roles": dict(Counter(items[0]["shot_role"] for items in image_groups)),
        },
        "active_policy": {k: policy[k] for k in sorted(policy) if k != "id"},
        "policy_modes": dict(policy_modes),
        "metrics": metrics,
        "examples": examples,
    }


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def write_markdown(report: Json, path: Path) -> None:
    lines = [
        "# Active Policy Retrieval Evaluation",
        "",
        "Read-only evaluation of the active detector DB policy on catalog dev products.",
        "",
        "## Inputs",
        "",
        f"- shot role: `{report['inputs']['shot_role']}`",
        f"- top_k: `{report['inputs']['top_k']}`",
        f"- writes detector DB: `{str(report['inputs']['writes_detector_db']).lower()}`",
        f"- uses filename tokens for matching: `{str(report['inputs']['uses_filename_tokens_for_matching']).lower()}`",
        "",
        "## Split",
        "",
        f"- total products: {report['split']['total_products']}",
        f"- dev products: {report['split']['dev_products']}",
        f"- hidden products: {report['split']['hidden_products']}",
        f"- evaluated probes: {report['split']['evaluated_probes']}",
        f"- shot roles: `{json.dumps(report['split']['shot_roles'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Metrics",
        "",
        "| Approach | Probes | Top-1 | Top-3 | Top-5 | Δ Top-1 vs full/studio | Δ Top-5 vs full/studio | Missing correct |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(report["metrics"], key=lambda item: -float(item["top1_accuracy"])):
        lines.append(
            f"| `{row['approach']}` | {row['evaluated_probes']} | {pct(row['top1_accuracy'])} | {pct(row['top3_recall'])} | {pct(row['top5_recall'])} | "
            f"{pct(row['delta_top1_vs_force_studio'])} | {pct(row['delta_top5_vs_force_studio'])} | {row['missing_correct_candidate']} |"
        )
    lines += ["", "## Policy modes", "", "```json", json.dumps(report["policy_modes"], indent=2, sort_keys=True), "```", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown-out")
    parser.add_argument("--shot-role", choices=["any", "live", "studio", "unknown"], default="any")
    parser.add_argument("--hidden-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=704)
    parser.add_argument("--top-k", type=int, default=50)
    parser.add_argument("--max-probes", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = evaluate(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, Path(args.markdown_out))
    print(json.dumps({"out": str(out), "metrics": report["metrics"], "policy_modes": report["policy_modes"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
