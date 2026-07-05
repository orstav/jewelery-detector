#!/usr/bin/env python3
"""Insert the approved crop embedding pilot as inactive DB rows.

This is intentionally narrow:
- reads local crop-profile JSONL artifacts;
- inserts/updates `image_profiles` for crop-profile-v1;
- inserts `image_embeddings` for jewelry-crop-v1 with active=false;
- never activates rows;
- can run in preflight mode without writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.jewelry_cluster_benchmark import TransformerImageEmbeddingProvider, sha256  # noqa: E402
from tools.jewelry_detector_db import connect, vector_literal  # noqa: E402

Json = dict[str, Any]
PROFILE_MODEL = "deterministic-jewelry-cropper"
PROFILE_VERSION = "crop-profile-v1"
PREPROCESS_VERSION = "jewelry-crop-v1"


def load_profiles(paths: list[Path]) -> list[Json]:
    by_image: dict[str, Json] = {}
    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                profile = json.loads(line)
                if profile.get("status") != "ready":
                    continue
                if profile.get("model") != PROFILE_MODEL or profile.get("prompt_version") != PROFILE_VERSION:
                    raise ValueError(f"unexpected profile model/version in {path}: {profile.get('model')} {profile.get('prompt_version')}")
                image_id = str(profile["image_id"])
                existing = by_image.get(image_id)
                if existing and existing.get("source_sha256") != profile.get("source_sha256"):
                    raise ValueError(f"conflicting duplicate profile for image_id={image_id}")
                by_image[image_id] = profile
    return sorted(by_image.values(), key=lambda item: (str(item.get("product_id", "")), str(item["image_id"])))


def cache_key(provider_id: str, path: Path) -> str:
    return f"{provider_id}|{sha256(path)}"


def embed_preview(provider: TransformerImageEmbeddingProvider, path: Path, cache: dict[str, Json]) -> list[float]:
    key = cache_key(provider.provider_id, path)
    if key not in cache:
        cache[key] = {"provider": provider.provider_id, "path": str(path), "vector": provider.embed(path)}
    return [float(x) for x in cache[key]["vector"]]


def profile_rows(profiles: list[Json]) -> list[tuple[Any, ...]]:
    rows = []
    for profile in profiles:
        rows.append(
            (
                profile["image_id"],
                profile["source_sha256"],
                PROFILE_MODEL,
                PROFILE_VERSION,
                int(profile["max_image_size"]),
                profile["cache_key"],
                json.dumps(profile["profile"], sort_keys=True),
                json.dumps({"source": "crop-profile-dryrun", "shot_role": profile.get("shot_role")}, sort_keys=True),
                "ready",
            )
        )
    return rows


def build_embedding_rows(profiles: list[Json], provider: TransformerImageEmbeddingProvider, cache: dict[str, Json]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for index, profile in enumerate(profiles, start=1):
        for crop in profile["profile"].get("crops", []):
            if not crop.get("usable_for_retrieval", True):
                continue
            suffix = str(crop["crop_id_suffix"])
            if suffix == "full":
                continue
            preview = Path(crop["preview_jpeg"])
            if not preview.exists():
                raise FileNotFoundError(preview)
            vector = embed_preview(provider, preview, cache)
            rows.append(
                (
                    profile.get("product_id"),
                    profile["image_id"],
                    crop["crop_id"],
                    crop["view_type"],
                    json.dumps(crop["box"], sort_keys=True),
                    f"{PROFILE_VERSION}:{crop['source']}",
                    json.dumps(crop.get("risk_flags", []), sort_keys=True),
                    vector_literal(vector),
                    provider.provider_id,
                    PREPROCESS_VERSION,
                    len(vector),
                    profile["source_sha256"],
                    False,
                )
            )
        if index % 50 == 0:
            print(f"prepared embeddings for {index}/{len(profiles)} profiles", flush=True)
    return rows


def db_counts(url: str) -> Json:
    with connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM image_profiles
            WHERE model=%s AND prompt_version=%s
            """,
            (PROFILE_MODEL, PROFILE_VERSION),
        )
        profiles = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT active, COUNT(*) FROM image_embeddings
            WHERE preprocess_version=%s
            GROUP BY active
            ORDER BY active
            """,
            (PREPROCESS_VERSION,),
        )
        embeddings = {str(bool(active)).lower(): int(count) for active, count in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM image_embeddings WHERE view_type='full_image' AND active=true")
        full_active = int(cur.fetchone()[0])
    return {"profiles": profiles, "embeddings_by_active": embeddings, "active_full_image_rows": full_active}


def insert_rows(url: str, profiles: list[Json], embeddings: list[tuple[Any, ...]], *, commit: bool) -> Json:
    before = db_counts(url)
    if before["embeddings_by_active"].get("true", 0):
        raise RuntimeError(f"active {PREPROCESS_VERSION} rows already exist; refusing to stage")
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO image_profiles (
                  image_id, source_sha256, model, prompt_version, max_image_size,
                  cache_key, profile_json, raw_response_json, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (source_sha256, model, prompt_version, max_image_size) DO UPDATE SET
                  image_id = EXCLUDED.image_id,
                  cache_key = EXCLUDED.cache_key,
                  profile_json = EXCLUDED.profile_json,
                  raw_response_json = EXCLUDED.raw_response_json,
                  status = EXCLUDED.status
                """,
                profile_rows(profiles),
            )
            cur.executemany(
                """
                INSERT INTO image_embeddings (
                  product_id, image_id, crop_id, view_type, crop_box, crop_source,
                  risk_flags, embedding, embedding_model, preprocess_version,
                  embedding_dim, source_sha256, active
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::vector, %s, %s, %s, %s, %s)
                ON CONFLICT (crop_id, embedding_model, preprocess_version) DO UPDATE SET
                  product_id = EXCLUDED.product_id,
                  image_id = EXCLUDED.image_id,
                  view_type = EXCLUDED.view_type,
                  crop_box = EXCLUDED.crop_box,
                  crop_source = EXCLUDED.crop_source,
                  risk_flags = EXCLUDED.risk_flags,
                  embedding = EXCLUDED.embedding,
                  embedding_dim = EXCLUDED.embedding_dim,
                  source_sha256 = EXCLUDED.source_sha256,
                  active = false
                """,
                embeddings,
            )
        if commit:
            conn.commit()
        else:
            conn.rollback()
    after = db_counts(url) if commit else before
    return {"before": before, "after": after, "committed": commit, "profiles_input": len(profiles), "embeddings_input": len(embeddings)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--profile-jsonl", action="append", required=True)
    parser.add_argument("--output", default="workbench/crop-embedding-pilot/pilot_insert_report.json")
    parser.add_argument("--cache-path", default="workbench/crop-embedding-pilot/embedding_cache.json")
    parser.add_argument("--model-id", default="google/siglip-base-patch16-224")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--offline-model-cache", action="store_true")
    parser.add_argument("--commit", action="store_true", help="Actually commit inactive staged rows. Without this, rolls back after preflight.")
    args = parser.parse_args()

    profiles = load_profiles([Path(path) for path in args.profile_jsonl])
    provider = TransformerImageEmbeddingProvider("siglip", args.model_id, device=args.device, image_size=args.image_size, local_files_only=args.offline_model_cache)
    cache_path = Path(args.cache_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    embeddings = build_embedding_rows(profiles, provider, cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    report = insert_rows(args.database_url, profiles, embeddings, commit=args.commit)
    report["profile_jsonl"] = args.profile_jsonl
    report["preprocess_version"] = PREPROCESS_VERSION
    report["profile_version"] = PROFILE_VERSION
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
