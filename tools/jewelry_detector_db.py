"""Postgres/pgvector persistence helpers for the jewelry detector CLI."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

JsonDict = dict[str, Any]
JsonList = list[JsonDict]


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS product_images (
  id BIGSERIAL PRIMARY KEY,
  image_id TEXT NOT NULL UNIQUE,
  product_id TEXT,
  source_uri TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS image_profiles (
  id BIGSERIAL PRIMARY KEY,
  image_id TEXT NOT NULL REFERENCES product_images(image_id) ON DELETE CASCADE,
  source_sha256 TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  max_image_size INTEGER NOT NULL,
  cache_key TEXT NOT NULL,
  profile_json JSONB NOT NULL,
  raw_response_json JSONB,
  status TEXT NOT NULL DEFAULT 'ready',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_sha256, model, prompt_version, max_image_size)
);

CREATE TABLE IF NOT EXISTS image_embeddings (
  id BIGSERIAL PRIMARY KEY,
  product_id TEXT,
  image_id TEXT NOT NULL REFERENCES product_images(image_id) ON DELETE CASCADE,
  crop_id TEXT NOT NULL,
  view_type TEXT NOT NULL,
  crop_box JSONB NOT NULL,
  crop_source TEXT NOT NULL,
  risk_flags JSONB NOT NULL,
  embedding vector(768) NOT NULL,
  embedding_model TEXT NOT NULL,
  preprocess_version TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL,
  source_sha256 TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (crop_id, embedding_model, preprocess_version)
);

CREATE TABLE IF NOT EXISTS matching_policies (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  embedding_model TEXT NOT NULL,
  preprocess_version TEXT NOT NULL,
  top_k INTEGER NOT NULL,
  candidate_min_score DOUBLE PRECISION NOT NULL,
  auto_match_score DOUBLE PRECISION NOT NULL,
  review_min_score DOUBLE PRECISION NOT NULL,
  margin_threshold DOUBLE PRECISION NOT NULL,
  active BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS match_attempts (
  id BIGSERIAL PRIMARY KEY,
  input_image_id TEXT NOT NULL REFERENCES product_images(image_id) ON DELETE CASCADE,
  policy_id BIGINT NOT NULL REFERENCES matching_policies(id),
  status TEXT NOT NULL CHECK (status IN ('matched', 'needs_review', 'no_match', 'failed')),
  selected_product_id TEXT,
  confidence DOUBLE PRECISION,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS match_candidates (
  id BIGSERIAL PRIMARY KEY,
  match_attempt_id BIGINT NOT NULL REFERENCES match_attempts(id) ON DELETE CASCADE,
  product_id TEXT NOT NULL,
  embedding_id BIGINT NOT NULL REFERENCES image_embeddings(id),
  rank INTEGER NOT NULL,
  similarity DOUBLE PRECISION NOT NULL,
  score DOUBLE PRECISION NOT NULL,
  margin DOUBLE PRECISION,
  decision_reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS image_embeddings_compat_idx
  ON image_embeddings (active, embedding_model, preprocess_version, embedding_dim)
  WHERE product_id IS NOT NULL;
"""

POLICY_SQL = """
INSERT INTO matching_policies (
  name,
  embedding_model,
  preprocess_version,
  top_k,
  candidate_min_score,
  auto_match_score,
  review_min_score,
  margin_threshold,
  active
) VALUES (
  'jewelry-siglip-v1',
  'siglip-google_siglip-base-patch16-224-cpu-s224',
  'jewelry-evidence-v1',
  20,
  0.82,
  0.93,
  0.86,
  0.03,
  true
) ON CONFLICT (name) DO UPDATE SET
  embedding_model = EXCLUDED.embedding_model,
  preprocess_version = EXCLUDED.preprocess_version,
  top_k = EXCLUDED.top_k,
  candidate_min_score = EXCLUDED.candidate_min_score,
  auto_match_score = EXCLUDED.auto_match_score,
  review_min_score = EXCLUDED.review_min_score,
  margin_threshold = EXCLUDED.margin_threshold,
  active = EXCLUDED.active;
"""

VECTOR_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS image_embeddings_embedding_hnsw_idx
  ON image_embeddings
  USING hnsw (embedding vector_cosine_ops)
  WHERE active = true AND product_id IS NOT NULL;
"""


def database_url(value: str | None) -> str:
    url = value or os.environ.get("DATABASE_URL", "")
    if not url:
        msg = "DATABASE_URL is not set; pass --database-url or set DATABASE_URL"
        raise RuntimeError(msg)
    return url


def connect(url: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:
        msg = "psycopg is required for DB commands. Install requirements-local.txt."
        raise RuntimeError(msg) from exc
    return psycopg.connect(url)


def read_json(path: Path) -> JsonDict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"expected JSON object: {path}"
        raise TypeError(msg)
    return payload


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def ensure_ready(payload: JsonDict, kind: str) -> None:
    if payload.get("status") == "error":
        raw_error = payload.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        msg = f"{kind} payload is an error: {error.get('message', 'unknown error')}"
        raise ValueError(msg)


def profile_dimensions(profile_payload: JsonDict) -> tuple[int | None, int | None]:
    profile = profile_payload.get("profile")
    if not isinstance(profile, dict):
        return None, None
    width = profile.get("image_width")
    height = profile.get("image_height")
    return int(width) if width else None, int(height) if height else None


def init_db(url: str, *, create_vector_index: bool = True) -> None:
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(POLICY_SQL)
            if create_vector_index:
                cur.execute(VECTOR_INDEX_SQL)
        conn.commit()


def store_profile(
    url: str,
    profile_payload: JsonDict,
    *,
    source_uri: str,
    product_id: str | None = None,
) -> None:
    ensure_ready(profile_payload, "profile")
    width, height = profile_dimensions(profile_payload)
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product_images (image_id, product_id, source_uri, sha256, width, height, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'ready')
                ON CONFLICT (image_id) DO UPDATE SET
                  product_id = COALESCE(EXCLUDED.product_id, product_images.product_id),
                  source_uri = EXCLUDED.source_uri,
                  sha256 = EXCLUDED.sha256,
                  width = COALESCE(EXCLUDED.width, product_images.width),
                  height = COALESCE(EXCLUDED.height, product_images.height),
                  status = 'ready'
                """,
                (
                    profile_payload["image_id"],
                    product_id,
                    source_uri,
                    profile_payload["source_sha256"],
                    width,
                    height,
                ),
            )
            cur.execute(
                """
                INSERT INTO image_profiles (
                  image_id, source_sha256, model, prompt_version, max_image_size,
                  cache_key, profile_json, raw_response_json, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'ready')
                ON CONFLICT (source_sha256, model, prompt_version, max_image_size) DO UPDATE SET
                  image_id = EXCLUDED.image_id,
                  cache_key = EXCLUDED.cache_key,
                  profile_json = EXCLUDED.profile_json,
                  raw_response_json = EXCLUDED.raw_response_json,
                  status = 'ready'
                """,
                (
                    profile_payload["image_id"],
                    profile_payload["source_sha256"],
                    profile_payload["model"],
                    profile_payload["prompt_version"],
                    profile_payload["max_image_size"],
                    profile_payload["cache_key"],
                    json.dumps(profile_payload["profile"]),
                    json.dumps(profile_payload.get("raw_response")),
                ),
            )
        conn.commit()


def store_embedding(
    url: str,
    embedding_payload: JsonDict,
    *,
    source_uri: str,
    product_id: str | None = None,
    allow_nonproduction_dim: bool = False,
) -> int:
    ensure_ready(embedding_payload, "embedding")
    embedding_dim = int(embedding_payload["embedding_dim"])
    if embedding_dim != 768 and not allow_nonproduction_dim:
        msg = f"embedding_dim must be 768 for pgvector storage; got {embedding_dim}"
        raise ValueError(msg)
    crops = [crop for crop in embedding_payload.get("crops", []) if isinstance(crop, dict)]
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product_images (image_id, product_id, source_uri, sha256, status)
                VALUES (%s, %s, %s, %s, 'ready')
                ON CONFLICT (image_id) DO UPDATE SET
                  product_id = COALESCE(EXCLUDED.product_id, product_images.product_id),
                  source_uri = EXCLUDED.source_uri,
                  sha256 = EXCLUDED.sha256,
                  status = 'ready'
                """,
                (embedding_payload["image_id"], product_id, source_uri, embedding_payload["source_sha256"]),
            )
            for crop in crops:
                if not crop.get("usable_for_retrieval", True):
                    continue
                cur.execute(
                    """
                    INSERT INTO image_embeddings (
                      product_id, image_id, crop_id, view_type, crop_box, crop_source,
                      risk_flags, embedding, embedding_model, preprocess_version,
                      embedding_dim, source_sha256, active
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::vector, %s, %s, %s, %s, true)
                    ON CONFLICT (crop_id, embedding_model, preprocess_version) DO UPDATE SET
                      product_id = EXCLUDED.product_id,
                      view_type = EXCLUDED.view_type,
                      crop_box = EXCLUDED.crop_box,
                      crop_source = EXCLUDED.crop_source,
                      risk_flags = EXCLUDED.risk_flags,
                      embedding = EXCLUDED.embedding,
                      embedding_dim = EXCLUDED.embedding_dim,
                      source_sha256 = EXCLUDED.source_sha256,
                      active = true
                    """,
                    (
                        product_id,
                        embedding_payload["image_id"],
                        crop["crop_id"],
                        crop["view_type"],
                        json.dumps(crop["box"]),
                        crop["source"],
                        json.dumps(crop.get("risk_flags", [])),
                        vector_literal(crop["embedding"]),
                        embedding_payload["embedding_model"],
                        embedding_payload["preprocess_version"],
                        embedding_dim,
                        embedding_payload["source_sha256"],
                    ),
                )
        conn.commit()
    return len(crops)


def load_active_policy(url: str, policy_name: str | None = None) -> JsonDict:
    with connect(url) as conn, conn.cursor() as cur:
        if policy_name:
            cur.execute(
                """
                SELECT id, name, embedding_model, preprocess_version, top_k,
                       candidate_min_score, auto_match_score, review_min_score,
                       margin_threshold
                FROM matching_policies
                WHERE name = %s AND active = true
                """,
                (policy_name,),
            )
        else:
            cur.execute(
                """
                SELECT id, name, embedding_model, preprocess_version, top_k,
                       candidate_min_score, auto_match_score, review_min_score,
                       margin_threshold
                FROM matching_policies
                WHERE active = true
                ORDER BY id
                LIMIT 1
                """
            )
        row = cur.fetchone()
    if row is None:
        msg = f"active matching policy not found: {policy_name or '<default>'}"
        raise ValueError(msg)
    return {
        "id": row[0],
        "name": row[1],
        "embedding_model": row[2],
        "preprocess_version": row[3],
        "top_k": row[4],
        "candidate_min_score": row[5],
        "auto_match_score": row[6],
        "review_min_score": row[7],
        "margin_threshold": row[8],
    }


def query_crop_candidates(url: str, embedding_payload: JsonDict, policy: JsonDict) -> JsonList:
    ensure_ready(embedding_payload, "embedding")
    rows: JsonList = []
    with connect(url) as conn, conn.cursor() as cur:
        for crop in embedding_payload.get("crops", []):
            if not isinstance(crop, dict) or not crop.get("usable_for_retrieval", True):
                continue
            cur.execute(
                """
                SELECT id, product_id, image_id, crop_id, 1 - (embedding <=> %s::vector) AS similarity
                FROM image_embeddings
                WHERE active = true
                  AND product_id IS NOT NULL
                  AND embedding_model = %s
                  AND preprocess_version = %s
                  AND embedding_dim = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    vector_literal(crop["embedding"]),
                    policy["embedding_model"],
                    policy["preprocess_version"],
                    int(embedding_payload["embedding_dim"]),
                    vector_literal(crop["embedding"]),
                    int(policy["top_k"]),
                ),
            )
            for index, row in enumerate(cur.fetchall(), start=1):
                rows.append(
                    {
                        "query_crop_id": crop["crop_id"],
                        "query_view_type": crop["view_type"],
                        "query_risk_flags": crop.get("risk_flags", []),
                        "embedding_id": row[0],
                        "product_id": row[1],
                        "candidate_image_id": row[2],
                        "candidate_crop_id": row[3],
                        "rank": index,
                        "similarity": float(row[4]),
                    }
                )
    return rows


def aggregate_product_candidates(rows: JsonList) -> JsonList:
    """Collapse crop-level embedding retrieval rows into product candidates.

    The detector already retrieves by embeddings. This aggregation improves the
    existing engine by using more of that evidence instead of letting a single
    lucky crop decide Top-1. Confidence remains anchored to the best embedding
    similarity, with a small consensus bonus when the same product appears
    across multiple retrieved crops/query crops.
    """
    grouped: dict[str, JsonDict] = {}
    for row in rows:
        product_id = str(row["product_id"])
        similarity = float(row["similarity"])
        current = grouped.setdefault(
            product_id,
            {
                "product_id": product_id,
                "embedding_id": row["embedding_id"],
                "similarity": similarity,
                "best_similarity": similarity,
                "best_crop_id": row["candidate_crop_id"],
                "query_crop_id": row["query_crop_id"],
                "risk_flags": [],
                "similarities": [],
                "query_crop_ids": set(),
                "candidate_crop_ids": set(),
            },
        )
        current["similarities"].append(similarity)
        current["query_crop_ids"].add(str(row.get("query_crop_id") or ""))
        current["candidate_crop_ids"].add(str(row.get("candidate_crop_id") or ""))
        for flag in row.get("query_risk_flags", []) or []:
            if flag not in current["risk_flags"]:
                current["risk_flags"].append(flag)
        if similarity > float(current["best_similarity"]):
            current["embedding_id"] = row["embedding_id"]
            current["similarity"] = similarity
            current["best_similarity"] = similarity
            current["best_crop_id"] = row["candidate_crop_id"]
            current["query_crop_id"] = row["query_crop_id"]

    for item in grouped.values():
        similarities = sorted((float(v) for v in item["similarities"]), reverse=True)
        top3 = similarities[:3]
        evidence_count = len(similarities)
        query_crop_count = len([v for v in item["query_crop_ids"] if v])
        candidate_crop_count = len([v for v in item["candidate_crop_ids"] if v])
        mean_top3 = sum(top3) / len(top3) if top3 else 0.0
        best_similarity = float(item["best_similarity"])
        # A single high crop can be a lucky composition/background hit. Blend it
        # with the mean of the best retrieved evidence for the same product so
        # repeated, consistent product evidence ranks above one-off spikes.
        item["score"] = (0.6 * best_similarity) + (0.4 * mean_top3)
        item["mean_top3_similarity"] = mean_top3
        item["evidence_count"] = evidence_count
        item["query_crop_count"] = query_crop_count
        item["candidate_crop_count"] = candidate_crop_count
        item["similarities"] = similarities[:5]
        item["query_crop_ids"] = sorted(v for v in item["query_crop_ids"] if v)
        item["candidate_crop_ids"] = sorted(v for v in item["candidate_crop_ids"] if v)

    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            float(item["score"]),
            int(item.get("query_crop_count") or 0),
            int(item.get("evidence_count") or 0),
            float(item.get("best_similarity") or 0.0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
        next_score = float(ranked[index]["score"]) if index < len(ranked) else 0.0
        item["margin"] = float(item["score"]) - next_score
    return ranked


def decide_match(candidates: JsonList, policy: JsonDict) -> JsonDict:
    if not candidates:
        return {
            "status": "no_match",
            "selected_product_id": None,
            "confidence": 0.0,
            "reason": "no_candidates",
        }
    top = candidates[0]
    top_score = float(top["score"])
    margin = float(top.get("margin", 0.0))
    risk_flags = [str(flag) for flag in top.get("risk_flags", [])]
    if top_score < float(policy["candidate_min_score"]):
        return {
            "status": "no_match",
            "selected_product_id": None,
            "confidence": top_score,
            "reason": "below_candidate_min_score",
        }
    if risk_flags:
        return {
            "status": "needs_review",
            "selected_product_id": str(top["product_id"]),
            "confidence": top_score,
            "reason": "crop_risk_flags",
        }
    if top_score >= float(policy["auto_match_score"]) and margin >= float(policy["margin_threshold"]):
        return {
            "status": "matched",
            "selected_product_id": str(top["product_id"]),
            "confidence": top_score,
            "reason": "auto_match_score_and_margin",
        }
    if top_score >= float(policy["review_min_score"]) or margin < float(policy["margin_threshold"]):
        return {
            "status": "needs_review",
            "selected_product_id": str(top["product_id"]),
            "confidence": top_score,
            "reason": "review_threshold_or_low_margin",
        }
    return {
        "status": "no_match",
        "selected_product_id": None,
        "confidence": top_score,
        "reason": "below_review_min_score",
    }


def persist_match_attempt(url: str, input_image_id: str, policy: JsonDict, decision: JsonDict, candidates: JsonList) -> int:
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO match_attempts (
                  input_image_id, policy_id, status, selected_product_id, confidence, reason
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    input_image_id,
                    policy["id"],
                    decision["status"],
                    decision.get("selected_product_id"),
                    decision.get("confidence"),
                    decision["reason"],
                ),
            )
            attempt_id = int(cur.fetchone()[0])
            for candidate in candidates:
                cur.execute(
                    """
                    INSERT INTO match_candidates (
                      match_attempt_id, product_id, embedding_id, rank, similarity, score, margin, decision_reason
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        attempt_id,
                        candidate["product_id"],
                        candidate["embedding_id"],
                        candidate["rank"],
                        candidate["similarity"],
                        candidate["score"],
                        candidate.get("margin"),
                        decision["reason"] if int(candidate["rank"]) == 1 else "candidate",
                    ),
                )
        conn.commit()
    return attempt_id


def match_embedding(url: str, embedding_payload: JsonDict, *, policy_name: str | None = None, persist: bool = True) -> JsonDict:
    policy = load_active_policy(url, policy_name)
    rows = query_crop_candidates(url, embedding_payload, policy)
    candidates = aggregate_product_candidates(rows)
    decision = decide_match(candidates, policy)
    attempt_id = persist_match_attempt(url, str(embedding_payload["image_id"]), policy, decision, candidates) if persist else None
    return {
        "schema_version": "1.0",
        "image_id": embedding_payload["image_id"],
        "policy": {key: value for key, value in policy.items() if key != "id"},
        "match_attempt_id": attempt_id,
        "status": decision["status"],
        "selected_product_id": decision.get("selected_product_id"),
        "confidence": decision.get("confidence"),
        "reason": decision["reason"],
        "candidates": candidates,
    }
