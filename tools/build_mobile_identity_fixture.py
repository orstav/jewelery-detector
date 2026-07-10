#!/usr/bin/env python3
"""Build the 2025-03-19/web identity-review fixture for the mobile prototype.

Read-only inputs: Stav SQLite and source ZIP. Writes only prototype fixture files.
No Airtable/Drive/Shopify/WhatsApp writes.
"""
from __future__ import annotations
import json, re, shutil, sqlite3, zipfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
SQLITE = WORKSPACE / 'state/stav/stav.sqlite'
READINESS_REPORT = WORKSPACE / 'workbench/readiness-validator/readiness-report-v1.json'
RAW_ROOT = WORKSPACE / 'workbench/package-prep/raw-intake/dropbox-stav-main-2026-06-07'
SOURCE_ZIP = RAW_ROOT / 'source/stav.zip'
OUT = ROOT / 'web/mobile-clustering-prototype/public/real-data'
DATA_JS = ROOT / 'web/mobile-clustering-prototype/public/data.js'
LEGACY_DATA_JS = ROOT / 'web/mobile-clustering-prototype/data.js'
MANIFEST = OUT / 'manifest.json'
DETECTOR_CANDIDATES = OUT / 'mobile_detector_candidates.json'
BATCH_ID = 'dropbox-2025-03-19-web'
SOURCE_PREFIX = '2025-03-19/web/'
EXPECTED = 42
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp'}
PRESERVE = ('catalog_', 'mobile_detector_candidates.json')
CATALOG_IMAGE_ARCHIVES = [
    # Canonical refreshed local Drive catalog. Older SQLite rows can still point at
    # /tmp/stav-drive-catalog, so resolve by catalog id + image name under this root.
    Path('/home/server/.openclaw/workspace/data/stav/drive-catalog'),
    Path('/home/server/.openclaw/workspace/data/stav/catalog-embeddings/full_catalog_expand_2026-05-31/catalog'),
    Path('/home/server/.openclaw/workspace/archive/stav-full-index-2026-05-31/catalog'),
    Path('/home/server/.openclaw/workspace/workbench/readiness-validator/17-analysis/images'),
    Path('/home/server/.openclaw/workspace/archive/cache-top-level-2026-06-12'),
    Path('/home/server/.openclaw/workspace/archive/cache-adhoc-2026-06-12/design-validation/imgs'),
    Path('/home/server/.openclaw/workspace/archive/temp-work/tmp_upload_candidates'),
]


def product_type(catalog_id: str) -> str:
    prefix = (catalog_id or '')[:1].upper()
    return {'R': 'טבעת', 'E': 'עגילים', 'N': 'שרשרת', 'B': 'צמיד'}.get(prefix, 'תכשיט')


def display_name_from_image(catalog_id: str, image_name: str) -> str:
    stem = Path(image_name).stem
    clean = re.sub(r'^' + re.escape(catalog_id) + r'[_\-\s]*', '', stem, flags=re.I)
    clean = re.sub(r'[_\-]+', ' ', clean).strip()
    clean = re.sub(r'\b(front|frontal|angled|angle|side|detail|lifestyle|yellow|white|rose|silver|gold|web|res|print|none|product|01|02|03|04|05)\b', '', clean, flags=re.I)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean or catalog_id


def load_catalog_name_map() -> dict[str, dict[str, str | None]]:
    """Return best read-only catalog names for the mobile picker.

    drive_catalog_index is image-only, so the previous picker derived names from
    filenames. Prefer Hebrew names from the local readiness export when present,
    while retaining filename-derived aliases for search and HAL verification.
    """
    if not READINESS_REPORT.exists():
        return {}
    data = json.loads(READINESS_REPORT.read_text(encoding='utf-8'))
    catalog_names: dict[str, dict[str, str | None]] = {}
    for row in data.get('products', []):
        catalog_id = (row.get('catalog_id') or '').strip()
        if not catalog_id:
            continue
        catalog_names[catalog_id] = {
            'hebrew': (row.get('name_he') or '').strip(),
            'english': (row.get('name_en') or '').strip(),
            'recordId': row.get('record_id'),
        }
    return catalog_names


def catalog_image_public_name(catalog_id: str, image_name: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', image_name)
    return f'catalog_{catalog_id}_{safe}'


def local_catalog_image_path(catalog_id: str, indexed_path: str | None, image_name: str | None = None) -> Path | None:
    candidates = []
    if indexed_path:
        candidates.append(Path(indexed_path))
        # SQLite can contain stale /tmp/stav-drive-catalog paths. Try the same
        # catalog-id/filename under the canonical refreshed catalog root first.
        stale = Path(indexed_path)
        if stale.name:
            candidates.append(Path('/home/server/.openclaw/workspace/data/stav/drive-catalog') / catalog_id / stale.name)
    if image_name:
        candidates.append(Path('/home/server/.openclaw/workspace/data/stav/drive-catalog') / catalog_id / image_name)
    for root in CATALOG_IMAGE_ARCHIVES:
        if not root.exists():
            continue
        if image_name and (root / catalog_id / image_name).exists():
            candidates.append(root / catalog_id / image_name)
        if (root / catalog_id).exists():
            candidates.extend(sorted((root / catalog_id).glob('*.jpg')))
            candidates.extend(sorted((root / catalog_id).glob('*.png')))
        candidates.extend(sorted(root.glob(f'{catalog_id}*.jpg')))
        candidates.extend(sorted(root.glob(f'{catalog_id}*.png')))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_detector_candidate_export() -> dict[str, Any]:
    if not DETECTOR_CANDIDATES.exists():
        return {'schema_version': None, 'detector': None, 'cards': {}, 'errors': []}
    data = json.loads(DETECTOR_CANDIDATES.read_text(encoding='utf-8'))
    if data.get('batch_id') != BATCH_ID:
        return {'schema_version': data.get('schema_version'), 'detector': data.get('detector'), 'cards': {}, 'errors': [{'reason': 'batch_id_mismatch', 'batch_id': data.get('batch_id')}]}
    return data


def attach_detector_candidates(card_id: str, raw_candidates: dict[str, Any], product_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    card = (raw_candidates.get('cards') or {}).get(card_id) or {}
    candidates = []
    for raw in card.get('candidates') or []:
        catalog_id = str(raw.get('product_id') or '').strip()
        product = product_by_id.get(catalog_id)
        if not catalog_id or product is None:
            continue
        score = raw.get('score') or raw.get('similarity') or raw.get('best_similarity')
        candidates.append({
            **product,
            'id': catalog_id,
            'productId': catalog_id,
            'referenceProductId': catalog_id,
            'source': 'detector_db_embedding_topk',
            'provenance': 'detector_db_embedding_topk',
            'rank': raw.get('rank'),
            'score': score,
            'similarity': raw.get('similarity'),
            'best_similarity': raw.get('best_similarity'),
            'mean_top3_similarity': raw.get('mean_top3_similarity'),
            'margin': raw.get('margin'),
            'detectorScore': score,
            'detectorEvidence': {
                'source': 'detector_db_embedding_topk',
                'model': (raw_candidates.get('detector') or {}).get('model'),
                'modelId': (raw_candidates.get('detector') or {}).get('model_id'),
                'preprocessVersions': (raw_candidates.get('detector') or {}).get('preprocess_versions'),
                'score': score,
                'similarity': raw.get('similarity'),
                'rank': raw.get('rank'),
                'margin': raw.get('margin'),
                'embeddingId': raw.get('embedding_id'),
                'candidateImageId': raw.get('candidate_image_id'),
                'candidateCropId': raw.get('candidate_crop_id'),
                'queryCropId': raw.get('query_crop_id'),
                'queryViewType': raw.get('query_view_type'),
                'riskFlags': raw.get('risk_flags') or [],
                'generatedAt': (raw_candidates.get('detector') or {}).get('generated_at'),
            },
        })
    return candidates


def build_product_index(con: sqlite3.Connection) -> list[dict]:
    catalog_names = load_catalog_name_map()
    rows = [dict(r) for r in con.execute('''
      select catalog_id, image_name, local_path
      from drive_catalog_index
      where status='downloaded' and lower(image_name) like '%.jpg'
      order by catalog_id, image_name
    ''')]
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        by_id.setdefault(row['catalog_id'], []).append(row)
    index = []
    for catalog_id in sorted(by_id.keys(), key=natural_key):
        options = by_id[catalog_id]
        exact_options = [
            row for row in options
            if Path(row['image_name']).stem.upper().startswith(catalog_id.upper())
        ]
        preferred = None
        src_path = None
        public_name = None
        if exact_options:
            preferred = sorted(exact_options, key=lambda r: (0 if re.search(r'(front|frontal|angled|01)', r['image_name'], re.I) else 1, natural_key(r['image_name'])))[0]
            src_path = local_catalog_image_path(catalog_id, preferred['local_path'], preferred['image_name'])
            public_name = catalog_image_public_name(catalog_id, preferred['image_name'])
            if src_path:
                shutil.copyfile(src_path, OUT / public_name)
        else:
            # Do not show a thumbnail from a different catalog id. Search can still
            # find the product, but the visual picker must fail closed.
            preferred = sorted(options, key=lambda r: natural_key(r['image_name']))[0]
        technical_name = display_name_from_image(catalog_id, preferred['image_name'])
        catalog_name = catalog_names.get(catalog_id, {})
        hebrew_name = catalog_name.get('hebrew') or ''
        english_name = catalog_name.get('english') or technical_name
        name = hebrew_name or english_name or technical_name
        ptype = product_type(catalog_id)
        aliases = [catalog_id, name, hebrew_name, english_name, technical_name, preferred['image_name'], catalog_id.lower(), name.lower(), english_name.lower(), technical_name.lower()]
        index.append({
            'id': catalog_id,
            'name': name,
            'label': f'{catalog_id} · {name}',
            'aliases': aliases,
            'type': ptype,
            'family': name,
            'nameHe': hebrew_name,
            'nameEn': english_name,
            'technicalName': technical_name,
            'meta': f'מוצר קיים בקטלוג · {ptype} · תמונת ייחוס לקריאה בלבד',
            'image': {'id': f'catalog-{catalog_id}', 'src': f'/real-data/{public_name}'} if src_path else None,
            'catalogSource': 'drive_catalog_index',
            'imageName': preferred['image_name'],
        })
    return index

def natural_key(path: str):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', path)]

def out_name(source_path: str) -> str:
    p = Path(source_path)
    return 'src_' + p.stem.replace(' ', '_') + p.suffix.lower()

def zip_member(source_path: str) -> str | None:
    if not SOURCE_ZIP.exists():
        return None
    with zipfile.ZipFile(SOURCE_ZIP) as z:
        names = z.namelist()
        if source_path in names:
            return source_path
        matches = [n for n in names if n.endswith('/' + source_path) or n.endswith(source_path)]
        matches = [n for n in matches if Path(n).suffix.lower() in IMAGE_SUFFIXES]
        return sorted(matches, key=natural_key)[0] if matches else None

def reset_out() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    preserved = {p.name: p.read_bytes() for pat in PRESERVE for p in OUT.glob(pat + '*') if p.is_file()}
    for p in OUT.glob('*'):
        if p.is_file():
            p.unlink()
    for name, data in preserved.items():
        (OUT / name).write_bytes(data)

def main() -> None:
    reset_out()
    con = sqlite3.connect(SQLITE)
    con.row_factory = sqlite3.Row
    product_index = build_product_index(con)
    product_by_id = {str(product['id']): product for product in product_index}
    detector_export = load_detector_candidate_export()
    rows = [dict(r) for r in con.execute('''
      select a.asset_id, a.source_path, a.role_path, a.asset_status, a.width, a.height, a.sha256,
             pg.group_id, pg.status as group_status, pg.candidate_product_id
      from assets a
      left join product_group_assets pga on pga.asset_id=a.asset_id
      left join product_groups pg on pg.group_id=pga.group_id
      where a.source_path like ?
      order by a.source_path
    ''', (SOURCE_PREFIX + '%',))]
    rows.sort(key=lambda r: natural_key(r['source_path']))
    source_assets, review_cards, skipped, blocked = [], [], [], []
    for idx, row in enumerate(rows, start=1):
        asset = {
            'asset_id': row['asset_id'], 'source_path': row['source_path'], 'role_path': row['role_path'],
            'asset_status': row['asset_status'], 'width': row['width'], 'height': row['height'],
            'sha256': row['sha256'], 'product_group_id': row.get('group_id'), 'group_status': row.get('group_status'),
        }
        source_assets.append(asset)
        member = zip_member(row['source_path'])
        if not member:
            blocked.append({'asset_id': row['asset_id'], 'source_path': row['source_path'], 'reason': 'source_image_missing_from_zip'})
            continue
        filename = out_name(row['source_path'])
        with zipfile.ZipFile(SOURCE_ZIP) as z:
            with z.open(member) as src, (OUT / filename).open('wb') as dst:
                shutil.copyfileobj(src, dst)
        status = row.get('group_status') or 'unmapped'
        assumption = 'HAL מצא שהתמונה דורשת החלטת זהות: מוצר קיים, מוצר חדש, אותו עיצוב/מוצר אחר, תמונות מתוקנות מהצלם, כפול/לא רלוונטי או לא בטוחה.'
        if status == 'dropbox_likely_existing_visual_confirm':
            assumption = 'ייתכן שזה מוצר קיים או תמונות מתוקנות מהצלם — צריך בחירה אנושית, לא קישור אוטומטי.'
        elif status == 'dropbox_possible_duplicate_visual_confirm':
            assumption = 'ייתכן שזו כפילות/תמונה דומה, אבל HAL לא מחליט לבד — צריך החלטת דליה.'
        card_id = f'card-{idx:03d}'
        detector_candidates = attach_detector_candidates(card_id, detector_export, product_by_id)
        review_cards.append({
            'id': card_id, 'sourceRef': row.get('group_id') or row['asset_id'], 'title': f'2025-03-19/web · תמונה {idx}',
            'subtitle': status, 'initialStage': 'product_identity', 'reviewIntent': 'identity_decision', 'halAssumption': assumption,
            'rawStatus': status,
            'detectorStatus': 'candidates' if detector_candidates else 'no_candidates',
            'detectorSource': 'detector_db_embedding_topk' if detector_candidates else None,
            'photos': [{'id': row['asset_id'], 'src': f'/real-data/{filename}', 'sourcePath': row['source_path'], 'sourceKind': 'source_zip', 'role': 'identity_review_photo'}],
            'candidates': detector_candidates,
            'existingCandidates': detector_candidates,
            'existingCandidate': detector_candidates[0] if detector_candidates else None,
        })
    coverage = {'expected': EXPECTED, 'seen': len(source_assets), 'reviewable': len(review_cards), 'skipped': len(skipped), 'blocked': len(blocked)}
    detector_stats = {
        'schema_version': detector_export.get('schema_version'),
        'source': (detector_export.get('detector') or {}).get('source'),
        'model': (detector_export.get('detector') or {}).get('model'),
        'generated_at': (detector_export.get('detector') or {}).get('generated_at'),
        'cards_with_candidates': sum(1 for card in review_cards if card.get('candidates')),
        'candidate_total': sum(len(card.get('candidates') or []) for card in review_cards),
        'errors': len(detector_export.get('errors') or []),
    }
    manifest = {'batch_id': BATCH_ID, 'source': str(SQLITE), 'source_zip': str(SOURCE_ZIP), 'source_assets': source_assets, 'review_cards': review_cards, 'skipped_assets': skipped, 'blocked_assets': blocked, 'coverage': coverage, 'detector': detector_stats, 'generated_at': datetime.now(timezone.utc).isoformat()}
    if len(source_assets) != EXPECTED:
        raise SystemExit(f'expected {EXPECTED} assets, saw {len(source_assets)}')
    if coverage['reviewable'] == 0 and not (skipped or blocked):
        raise SystemExit('reviewable is 0 without explicit skipped/blocked reasons')
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    stats = {'batch_id': BATCH_ID, 'source': str(SQLITE), 'source_folder': SOURCE_PREFIX.rstrip('/'), 'source_assets_expected': EXPECTED, 'source_assets_seen': len(source_assets), 'groups_exported': len(review_cards), 'photos_exported': sum(len(c['photos']) for c in review_cards), 'coverage': coverage, 'detector': detector_stats, 'generated_at': manifest['generated_at'], 'no_live_writes': True}
    js = '\n'.join([
        'window.STAV_DATASET_VERSION = ' + json.dumps(BATCH_ID + '-v1', ensure_ascii=False) + ';',
        'window.STAV_DATASET_SOURCE = ' + json.dumps('SQLite assets + source ZIP: 2025-03-19/web', ensure_ascii=False) + ';',
        'window.STAV_SOURCE_ASSETS = ' + json.dumps(source_assets, ensure_ascii=False, indent=2) + ';',
        'window.STAV_REAL_GROUPS = ' + json.dumps(review_cards, ensure_ascii=False, indent=2) + ';',
        'window.STAV_REAL_DATASET_STATS = ' + json.dumps(stats, ensure_ascii=False, indent=2) + ';',
        'window.STAV_PRODUCT_INDEX = ' + json.dumps(product_index, ensure_ascii=False, indent=2) + ';',
        '',
    ])
    DATA_JS.write_text(js, encoding='utf-8')
    LEGACY_DATA_JS.write_text(js, encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False))

if __name__ == '__main__':
    main()
