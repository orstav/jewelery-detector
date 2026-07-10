#!/usr/bin/env python3
"""Export real detector top-k candidates for the Dalia mobile identity sorter.

Read-only against the detector Postgres DB and source images. Writes only a local
JSON artifact consumed by build_mobile_identity_fixture.py.

No Airtable/Drive/Shopify/WhatsApp writes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
SQLITE = WORKSPACE / 'state/stav/stav.sqlite'
PROTOTYPE = ROOT / 'web/mobile-clustering-prototype'
REAL_DATA = PROTOTYPE / 'public/real-data'
OUT = REAL_DATA / 'mobile_detector_candidates.json'
SOURCE_PREFIX = '2025-03-19/web/'
OPENCLAW_DETECTOR = Path('/home/server/.openclaw/workspace/apps/jewelery-detector')
DETECTOR_CLI = OPENCLAW_DETECTOR / 'tools/jewelry_detector.py'
PYTHON = OPENCLAW_DETECTOR / '.venv/bin/python'
MODEL = 'siglip-google_siglip-base-patch16-224-cpu-s224'
MODEL_ID = 'google/siglip-base-patch16-224'
PREPROCESS_VERSIONS = ('jewelry-evidence-v1', 'jewelry-crop-v1')
TOP_K_PER_CROP = 12
TOP_K_PRODUCTS = 5

Json = dict[str, Any]


def natural_key(path: str):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', path)]


def source_public_name(source_path: str) -> str:
    p = Path(source_path)
    return 'src_' + p.stem.replace(' ', '_') + p.suffix.lower()


def detector_database_url() -> str:
    if os.environ.get('DATABASE_URL'):
        return os.environ['DATABASE_URL']
    # Local dev/test convenience: read the already-running detector Postgres
    # container password without printing it. If this fails, caller must provide
    # DATABASE_URL securely in the environment.
    try:
        raw = subprocess.check_output(['docker', 'inspect', 'jewelery-detector-postgres'], text=True)
        env = dict(item.split('=', 1) for item in json.loads(raw)[0]['Config']['Env'] if '=' in item)
        password = env.get('POSTGRES_PASSWORD')
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError('DATABASE_URL is not set and local detector Postgres was not discoverable') from exc
    if not password:
        raise RuntimeError('local detector Postgres does not expose POSTGRES_PASSWORD')
    return f'postgresql://detector:{password}@127.0.0.1:55433/detector'


def load_source_rows() -> list[Json]:
    con = sqlite3.connect(SQLITE)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute('''
      select a.asset_id, a.source_path, a.sha256, pg.group_id, pg.status as group_status
      from assets a
      left join product_group_assets pga on pga.asset_id=a.asset_id
      left join product_groups pg on pg.group_id=pga.group_id
      where a.source_path like ?
      order by a.source_path
    ''', (SOURCE_PREFIX + '%',))]
    con.close()
    return sorted(rows, key=lambda r: natural_key(str(r['source_path'])))


def vector_literal(vector: list[float]) -> str:
    return '[' + ','.join(f'{float(value):.8f}' for value in vector) + ']'


def embed_image(image_path: Path, image_id: str, cache_dir: Path) -> Json:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f'{image_id}.embedding.json'
    if out.exists():
        payload = json.loads(out.read_text(encoding='utf-8'))
        if payload.get('embedding_model') == MODEL and payload.get('crops'):
            return payload
    py = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    env = {**os.environ, 'PYTHONPATH': str(OPENCLAW_DETECTOR)}
    command = [
        py, str(DETECTOR_CLI), 'embed',
        '--image', str(image_path),
        '--image-id', image_id,
        '--out', str(out),
        '--provider', 'siglip',
        '--model-id', MODEL_ID,
        '--device', 'cpu',
        '--offline-model-cache',
    ]
    result = subprocess.run(command, cwd=OPENCLAW_DETECTOR, env=env, text=True, capture_output=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError(f'embed failed for {image_path.name}: {(result.stderr or result.stdout)[-500:]}')
    return json.loads(out.read_text(encoding='utf-8'))


def query_candidates(database_url: str, embedding_payload: Json) -> list[Json]:
    sys.path.insert(0, str(OPENCLAW_DETECTOR))
    from tools import jewelry_detector_db as db  # type: ignore

    rows: list[Json] = []
    with db.connect(database_url) as conn, conn.cursor() as cur:
        for crop in embedding_payload.get('crops', []):
            if not isinstance(crop, dict) or not crop.get('usable_for_retrieval', True):
                continue
            embedding = crop.get('embedding')
            if not embedding:
                continue
            cur.execute(
                """
                SELECT id, product_id, image_id, crop_id, preprocess_version,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM image_embeddings
                WHERE active = true
                  AND product_id IS NOT NULL
                  AND embedding_model = %s
                  AND preprocess_version = ANY(%s)
                  AND embedding_dim = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    vector_literal(embedding),
                    MODEL,
                    list(PREPROCESS_VERSIONS),
                    int(embedding_payload['embedding_dim']),
                    vector_literal(embedding),
                    TOP_K_PER_CROP,
                ),
            )
            for rank, row in enumerate(cur.fetchall(), start=1):
                rows.append({
                    'query_crop_id': crop['crop_id'],
                    'query_view_type': crop.get('view_type'),
                    'query_risk_flags': crop.get('risk_flags', []),
                    'embedding_id': int(row[0]),
                    'product_id': str(row[1]),
                    'candidate_image_id': str(row[2]),
                    'candidate_crop_id': str(row[3]),
                    'candidate_preprocess_version': str(row[4]),
                    'crop_rank': rank,
                    'similarity': float(row[5]),
                })
    return rows


def aggregate(rows: list[Json]) -> list[Json]:
    grouped: dict[str, Json] = {}
    crop_hits: dict[str, list[Json]] = defaultdict(list)
    for row in rows:
        pid = str(row['product_id'])
        crop_hits[pid].append(row)
        current = grouped.get(pid)
        if current is None or float(row['similarity']) > float(current['score']):
            grouped[pid] = {
                'product_id': pid,
                'score': float(row['similarity']),
                'similarity': float(row['similarity']),
                'best_similarity': float(row['similarity']),
                'embedding_id': row['embedding_id'],
                'candidate_image_id': row['candidate_image_id'],
                'candidate_crop_id': row['candidate_crop_id'],
                'candidate_preprocess_version': row['candidate_preprocess_version'],
                'query_crop_id': row['query_crop_id'],
                'query_view_type': row['query_view_type'],
                'risk_flags': row.get('query_risk_flags', []),
                'source': 'detector_db_embedding_topk',
            }
    ranked = sorted(grouped.values(), key=lambda item: float(item['score']), reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item['rank'] = idx
        hits = crop_hits[item['product_id']]
        item['mean_top3_similarity'] = sum(float(hit['similarity']) for hit in hits[:3]) / min(len(hits), 3)
        next_score = float(ranked[idx]['score']) if idx < len(ranked) else 0.0
        item['margin'] = float(item['score']) - next_score
    return ranked[:TOP_K_PRODUCTS]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(OUT))
    parser.add_argument('--cache-dir', default=str(WORKSPACE / 'workbench/mobile-detector-candidate-cache'))
    args = parser.parse_args()

    if not DETECTOR_CLI.exists():
        raise SystemExit(f'missing detector CLI: {DETECTOR_CLI}')
    database_url = detector_database_url()
    rows = load_source_rows()
    cache_dir = Path(args.cache_dir).resolve()
    output: Json = {
        'schema_version': 'mobile-detector-candidates-v1',
        'batch_id': 'dropbox-2025-03-19-web',
        'detector': {
            'source': 'detector_db_embedding_topk',
            'model': MODEL,
            'model_id': MODEL_ID,
            'preprocess_versions': list(PREPROCESS_VERSIONS),
            'top_k_products': TOP_K_PRODUCTS,
            'top_k_per_crop': TOP_K_PER_CROP,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'writes': {'airtable': False, 'drive': False, 'shopify': False, 'whatsapp': False},
        },
        'cards': {},
        'errors': [],
    }
    for index, row in enumerate(rows, start=1):
        public = REAL_DATA / source_public_name(str(row['source_path']))
        card_id = f'card-{index:03d}'
        image_id = f'mobile_{row["asset_id"]}'
        if not public.exists():
            output['errors'].append({'card_id': card_id, 'asset_id': row['asset_id'], 'reason': 'public_source_image_missing'})
            continue
        try:
            embedding = embed_image(public, image_id, cache_dir)
            raw_rows = query_candidates(database_url, embedding)
            candidates = aggregate(raw_rows)
        except Exception as exc:  # fail closed per-card, keep export auditable
            output['errors'].append({'card_id': card_id, 'asset_id': row['asset_id'], 'reason': exc.__class__.__name__, 'message': str(exc)[:300]})
            candidates = []
        output['cards'][card_id] = {
            'card_id': card_id,
            'asset_id': row['asset_id'],
            'source_path': row['source_path'],
            'source_sha256': row.get('sha256'),
            'detector_status': 'candidates' if candidates else 'no_candidates',
            'candidates': candidates,
        }
        print(json.dumps({'card_id': card_id, 'candidate_count': len(candidates), 'top': candidates[0]['product_id'] if candidates else None}, ensure_ascii=False), flush=True)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {
        'cards': len(output['cards']),
        'with_candidates': sum(1 for card in output['cards'].values() if card.get('candidates')),
        'errors': len(output['errors']),
        'out': str(out),
        'no_live_writes': True,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if output['cards'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
