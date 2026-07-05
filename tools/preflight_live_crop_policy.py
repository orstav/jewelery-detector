#!/usr/bin/env python3
"""Read-only preflight for the live-only crop retrieval gate.

This script does not write to matching_policies, image_embeddings, match_attempts,
or match_candidates. It simulates the post-deploy policy in memory and verifies
that:

- the currently active DB policy still returns full-image candidates;
- studio/legacy payloads stay full-image-only even when crop version is present;
- live-like payloads can use additive crop rows when crop rows are present.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

import jewelry_detector_db as db

Json = dict[str, Any]
Summary = dict[str, Any]


def parse_vector(text: str) -> list[float]:
    values = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not values:
        msg = "could not parse pgvector text"
        raise ValueError(msg)
    return [float(value) for value in values]


def fetch_probe_embedding(url: str) -> Json:
    with db.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT image_id, crop_id, view_type, embedding::text, embedding_model,
                   preprocess_version, embedding_dim, source_sha256
            FROM image_embeddings
            WHERE active = true
              AND product_id IS NOT NULL
              AND view_type = 'full_image'
              AND preprocess_version <> 'jewelry-crop-v1'
            ORDER BY id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        msg = "no active full-image probe embedding found"
        raise RuntimeError(msg)
    return {
        "schema_version": "1.0",
        "image_id": f"preflight_{row[0]}",
        "embedding_model": row[4],
        "preprocess_version": row[5],
        "embedding_dim": int(row[6]),
        "source_sha256": row[7],
        "crops": [
            {
                "crop_id": f"preflight_{row[1]}",
                "view_type": row[2],
                "box": [0, 0, 1, 1],
                "source": "preflight_existing_db_vector",
                "risk_flags": [],
                "usable_for_retrieval": True,
                "embedding": parse_vector(row[3]),
            }
        ],
        "warnings": ["preflight_db_vector_reuse_no_write"],
    }


def count_rows_by_version(url: str) -> dict[str, int]:
    with db.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT preprocess_version, active, COUNT(*)
            FROM image_embeddings
            GROUP BY preprocess_version, active
            ORDER BY preprocess_version, active
            """
        )
        rows = cur.fetchall()
    return {f"{row[0]}:{bool(row[1])}": int(row[2]) for row in rows}


def active_policies(url: str) -> list[Json]:
    with db.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, preprocess_version, active
            FROM matching_policies
            WHERE active = true
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": int(row[0]),
            "name": row[1],
            "preprocess_version": row[2],
            "active": bool(row[3]),
        }
        for row in rows
    ]


def future_policy(active_policy: Json, *, include_inactive_crops: bool) -> Json:
    policy = dict(active_policy)
    versions = db.policy_preprocess_versions(active_policy)
    if db.CROP_PREPROCESS_VERSION not in versions:
        versions = [*versions, db.CROP_PREPROCESS_VERSION]
    policy["preprocess_versions"] = versions
    policy["include_inactive_embeddings"] = include_inactive_crops
    return policy



def summarize_result(mode: str, policy: Json, candidates: list[Json]) -> Json:
    versions = sorted({str(item.get("candidate_preprocess_version")) for item in candidates})
    view_types = sorted({str(item.get("candidate_view_type")) for item in candidates})
    active_states = sorted({bool(item.get("candidate_active")) for item in candidates})
    return {
        "mode": mode,
        "candidate_count": len(candidates),
        "candidate_policy_mode": policy.get("candidate_policy_mode"),
        "versions": versions,
        "view_types": view_types,
        "active_states": active_states,
    }


def run(args: argparse.Namespace) -> int:
    url = db.database_url(args.database_url)
    probe = fetch_probe_embedding(url)
    active_policy = db.load_active_policy(url, args.policy)
    row_counts = count_rows_by_version(url)

    active_policy_rows = active_policies(url)

    current_effective = db.effective_candidate_policy(probe, active_policy)
    current_candidates = db.query_crop_candidates(url, probe, current_effective)

    simulated_policy = future_policy(active_policy, include_inactive_crops=args.include_inactive_crops)

    studio_probe = dict(probe)
    studio_probe["profile_scene_type"] = "clean_product"
    studio_effective = db.effective_candidate_policy(studio_probe, simulated_policy)
    studio_candidates = db.query_crop_candidates(url, studio_probe, studio_effective)

    live_probe = dict(probe)
    live_probe["profile_scene_type"] = "model_lifestyle"
    live_probe["profile_has_person"] = True
    live_effective = db.effective_candidate_policy(live_probe, simulated_policy)
    live_candidates = db.query_crop_candidates(url, live_probe, live_effective)

    summary: Summary = {
        "row_counts": row_counts,
        "active_policies": active_policy_rows,
        "current": summarize_result("current_active_policy", current_effective, current_candidates),
        "studio_simulated": summarize_result("studio_future_policy", studio_effective, studio_candidates),
        "live_simulated": summarize_result("live_future_policy", live_effective, live_candidates),
    }

    failures: list[str] = []
    if len(active_policy_rows) != 1:
        failures.append(f"expected exactly one active policy, found {len(active_policy_rows)}")
    if summary["current"]["candidate_count"] <= 0:
        failures.append("current active policy returned no candidates")
    if summary["current"]["candidate_policy_mode"] not in {"full_only", "studio_full_only"}:
        failures.append("current active policy did not keep legacy/studio payload full-image-only")
    if summary["studio_simulated"]["candidate_policy_mode"] != "studio_full_only":
        failures.append("studio simulated policy did not stay studio_full_only")
    if db.CROP_PREPROCESS_VERSION in summary["studio_simulated"]["versions"]:
        failures.append("studio simulated policy returned crop-version candidates")
    if summary["live_simulated"]["candidate_policy_mode"] != "live_additive_crop":
        failures.append("live simulated policy did not become live_additive_crop")
    if args.require_live_crop_candidates and db.CROP_PREPROCESS_VERSION not in summary["live_simulated"]["versions"]:
        failures.append("live simulated policy returned no crop-version candidates")

    summary["status"] = "fail" if failures else "pass"
    summary["failures"] = failures
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="Postgres URL; defaults to DATABASE_URL")
    parser.add_argument("--policy", help="active policy name; defaults to first active policy")
    parser.add_argument("--include-inactive-crops", action="store_true", help="simulate readback before crop activation")
    parser.add_argument("--require-live-crop-candidates", action="store_true", help="fail if simulated live policy cannot see crop-version rows")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
