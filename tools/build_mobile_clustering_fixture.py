#!/usr/bin/env python3
"""Build a real-data fixture for the Stav mobile clustering prototype.

Read-only over raw-intake staging artifacts. Copies local image files into
web/mobile-clustering-prototype/real-data and writes data.js consumed by app.js.
No Shopify/Airtable/Drive/detector DB writes.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT.parents[1] / "workbench/package-prep/raw-intake/dropbox-stav-main-2026-06-07"
STAGING = RAW_ROOT / "staging/raw_intake_staging_plan.json"
SOURCE = RAW_ROOT / "source"
SELECTED = RAW_ROOT / "selected"
EXTRACTED = RAW_ROOT / "detector-refresh-2026-07-03/extracted"
OUT = ROOT / "web/mobile-clustering-prototype/public/real-data"
DATA_JS = ROOT / "web/mobile-clustering-prototype/public/data.js"
LEGACY_DATA_JS = ROOT / "web/mobile-clustering-prototype/data.js"
SOURCE_ZIP = SOURCE / "stav.zip"
PRESERVED_REAL_DATA_PATTERNS = ("catalog_*",)
STATIC_PRODUCT_INDEX = [
    {
        "id": "R037",
        "name": "טבעת ליין יהלומים",
        "aliases": ["R037", "טבעת ליין", "ליין יהלומים", "טבעת פס יהלומים"],
        "type": "טבעת",
        "family": "Line",
        "meta": "מוצר קיים בקטלוג · תמונת ייחוס לקריאה בלבד",
        "image": {"id": "catalog_R037_frontal_01", "src": "/real-data/catalog_R037_frontal_01.jpg"},
    },
    {
        "id": "NEGEV-NECKLACE",
        "name": "שרשרת נגב",
        "aliases": ["Negev Necklace", "negev", "נגב", "תליון נגב"],
        "type": "שרשרת",
        "family": "Negev",
        "meta": "דוגמה לחיפוש לפי שם שההורה זוכר",
    },
]

# Keep this prototype bound to the raw-intake contract: Dalia/Eyal should only
# see groups that still require human review. The staging plan also contains
# historical/terminal groups for auditability (already live, draft created,
# waiting on parents, dead/merged, etc.). Exporting those into the mobile queue
# was the root cause of already-cataloged products being shown as fresh review
# tasks.
RAW_INTAKE_EXITED_GROUP_STATUSES = {
    "not_uploadable",
    "published_active_validated",
    "already_live_approved",
    "dead_or_merged",
    "draft_created",
    "promoted_to_catalog_draft",
    "approved_upload_blocked",
    "needs_parent_fact_mapping",
    "raw_intake_local_images_missing",
    "parent_facts_captured",
    "ready_for_package",
    "package_sent_waiting_parent_approval",
    "waiting_on_parents",
    "draft_exists_needs_reconciliation",
    "airtable_identity_promoted",
    "airtable_identity_bound_existing",
    "early_airtable_row_created",
    "raw_pg_identity_anchor_required",
    "raw_pg_identity_validated_pending_airtable",
    "raw_pg_airtable_identity_row_required",
}
ACTIVE_MOBILE_CANONICAL_STAGES = {
    "detector_matched",
    "identity_classified",
}
STATUS_HE = {
    "needs_parent_fact_mapping": "צריך השלמת פרטים",
    "parent_facts_captured": "יש פרטים — צריך אימות/המשך טיפול",
    "pending_review": "צריך בדיקת שיוך",
    "existing_product_new_images": "תמונות חדשות למוצר קיים",
    "published_active_validated": "מוצר קיים באתר — לצרף תמונות",
    "draft_created": "כבר נוצר מוצר — לבדוק שיוך",
    "package_sent_waiting_parent_approval": "כבר נשלחה חבילת אישור — לא שייך לאימות תמונות",
    "approved_upload_blocked": "אושר להעלאה — ממשיך לשערי העלאה",
}
CATALOGED_PRODUCT_STATUSES = {
    "existing_product_new_images",
    "published_active_validated",
    "draft_created",
}
TYPE_HE = {
    "ring": "טבעת",
    "earring": "עגילים",
    "necklace": "שרשרת",
    "bracelet": "צמיד",
}
METAL_HE = {
    "gold": "זהב",
    "rose_gold": "זהב ורוד",
    "white_gold": "זהב לבן",
    "silver": "כסף",
    "mixed": "מתכות שונות",
}
STONE_HE = {
    "sapphire": "ספיר",
    "ruby": "רובי",
    "pearl": "פנינה",
    "diamond": "יהלום",
    "emerald": "אמרלד",
    "faceted_gemstone": "אבן צבעונית",
    "mixed": "אבנים שונות",
    "moonstone": "מונסטון",
    "none": "ללא אבן",
}
SKIP_IMAGE_MARKERS = (
    "gallery",
    "contact_sheet",
    "contact-sheet",
    "approval",
    "dryrun",
    "ask_sheet",
    "fact_ask",
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
DETECTOR_CANDIDATE_SOURCES = {
    "detector_topk",
    "detector_db_topk",
    "crop_embedding_topk",
    "jewelry_detector_match",
}


def safe_name(source_path: str) -> str:
    return source_path.replace("/", "__")


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def is_product_photo(path: Path) -> bool:
    lower_name = path.name.lower()
    return is_image(path) and not any(marker in lower_name for marker in SKIP_IMAGE_MARKERS)


def sorted_matches(paths: list[Path]) -> list[Path]:
    return sorted({path for path in paths if path.exists()}, key=lambda path: str(path.relative_to(RAW_ROOT) if path.is_relative_to(RAW_ROOT) else path))


def selected_fallbacks(group_id: str, source_path: str) -> list[Path]:
    basename = Path(source_path).name
    pg_dir = SELECTED / group_id
    matches = []
    if pg_dir.exists():
        matches.extend(pg_dir.rglob(basename))
        matches.extend(path for path in pg_dir.rglob("*") if path.name.endswith(basename))
    return sorted_matches([path for path in matches if is_product_photo(path)])


def zip_member_for_source(source_path: str) -> str | None:
    """Find an original source image inside the staged Dropbox ZIP."""
    if not SOURCE_ZIP.exists():
        return None
    basename = Path(source_path).name
    with zipfile.ZipFile(SOURCE_ZIP) as archive:
        names = archive.namelist()
        if source_path in names:
            return source_path
        matches = [name for name in names if name.endswith("/" + source_path) or Path(name).name == basename]
        image_matches = [name for name in matches if is_image(Path(name))]
        if not image_matches:
            return None
        return sorted(image_matches)[0]


def find_source_file(group_id: str, source_path: str) -> tuple[Path | None, str]:
    direct_candidates = [
        SOURCE / source_path,
        EXTRACTED / safe_name(source_path),
        EXTRACTED / Path(source_path).name,
    ]
    for candidate in direct_candidates:
        if candidate.exists() and is_product_photo(candidate):
            return candidate, "staging_asset"

    fallback = selected_fallbacks(group_id, source_path)
    if fallback:
        return fallback[0], "selected_fallback"

    broad_matches = sorted_matches(
        [path for path in EXTRACTED.rglob(Path(source_path).name) if is_product_photo(path)]
        + [path for path in SELECTED.rglob(Path(source_path).name) if is_product_photo(path)]
    )
    if broad_matches:
        source_kind = "selected_fallback" if SELECTED in broad_matches[0].parents else "staging_asset"
        return broad_matches[0], source_kind

    zip_member = zip_member_for_source(source_path)
    if zip_member:
        return Path(zip_member), "source_zip"

    return None, "missing"


def summarize_assets(pg: dict) -> str:
    first = (pg.get("assets") or [{}])[0]
    bits = []
    jt = TYPE_HE.get(first.get("jewelry_type"), first.get("jewelry_type"))
    metal = METAL_HE.get(first.get("metal_color"), first.get("metal_color"))
    stone = STONE_HE.get(first.get("stone_type"), first.get("stone_type"))
    for value in [jt, metal, stone]:
        if value and value != "none":
            bits.append(value)
    return " · ".join(bits) if bits else (pg.get("evidence") or {}).get("description") or "קבוצת תכשיט לבדיקה"


def proposal_type(category: str, status: str | None, pg: dict[str, Any]) -> str:
    if status == "pending_review":
        return "split_likely" if pg.get("member_count", 0) > 2 else "same_new_product_group"
    if category == "new_product_existing_design":
        return "same_design_sibling"
    return "same_new_product_group"


def recommended_action(ptype: str) -> str:
    return {
        "attach_to_existing_product": "לאשר צירוף למוצר קיים",
        "same_design_sibling": "לבדוק הבדל / אותו עיצוב",
        "split_likely": "לפתוח ולוודא קבוצה",
        "same_new_product_group": "אישור מהיר כמוצר חדש",
    }.get(ptype, "לפתוח")


def is_cataloged_existing_product(category: str, status: str | None, product_candidates: list[dict[str, Any]]) -> bool:
    """True when staging already says this product group belongs to a cataloged product.

    These groups should not begin with a generic photo-clustering question. The
    product_groups row/status is the authoritative post-staging state; the
    cluster-level category can lag behind it after a draft/publish/update.
    """
    if not product_candidates:
        return False
    return category == "existing_product_new_images" or status in CATALOGED_PRODUCT_STATUSES


def initial_stage_for_group(category: str, status: str | None, product_candidates: list[dict[str, Any]], photo_count: int) -> str:
    if is_cataloged_existing_product(category, status, product_candidates):
        return "existing_product_selection"
    if product_candidates:
        return "product_identity"
    return "product_identity" if photo_count <= 1 else "cluster_photos"


def initial_stats(data: dict) -> dict:
    return {
        "source": str(STAGING.relative_to(ROOT.parents[1])),
        "considered_product_groups": 0,
        "actionable_product_groups": 0,
        "groups_exported": 0,
        "photos_exported": 0,
        "skipped_status_counts": {},
        "missing_files": [],
        "source_counts": {"staging_asset": 0, "selected_fallback": 0, "source_zip": 0},
        "generated_at": data.get("generated_at"),
    }


def increment(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def is_mobile_actionable(pg: dict[str, Any]) -> bool:
    """Return True only for raw-intake groups still needing reviewer identity work.

    Prefer the canonical reducer fields added by build_raw_intake_staging_plan.py.
    Fall back to legacy raw status only for older staging files.
    """
    source_funnel = pg.get("source_funnel")
    canonical_stage = pg.get("canonical_stage")
    if canonical_stage:
        return source_funnel == "raw_intake" and canonical_stage in ACTIVE_MOBILE_CANONICAL_STAGES
    return pg.get("status") not in RAW_INTAKE_EXITED_GROUP_STATUSES


def group_sort_key(group: dict) -> tuple[str, str]:
    return (group.get("rawCategory") or "", group.get("sourceRef") or group.get("id") or "")


def out_file_name(group_id: str, source_path: str, index: int) -> str:
    source_stem = Path(source_path).stem.replace(" ", "_")
    return f"{group_id}__{index:02d}__{source_stem}.jpg"


def candidate_records(item: dict, pg: dict) -> list[dict]:
    candidates = []
    seen = set()

    def add_candidate(candidate_id: str | None, meta: str, source: str, kind: str = "product", **extra: object) -> None:
        if not candidate_id or candidate_id in seen:
            return
        seen.add(candidate_id)
        candidate_type = "עיצוב קיים" if kind == "design" else "מוצר קיים"
        record: dict[str, object] = {
            "id": candidate_id,
            "label": f"{candidate_id} · {candidate_type}",
            "meta": meta,
            "source": source,
            "kind": kind,
        }
        for key, value in extra.items():
            if value is not None:
                record[key] = value
        candidates.append(record)

    # The staging plan already carries human/SQLite identity context. Do not drop it
    # just because it did not come from the detector-candidate list.
    add_candidate(pg.get("candidate_product_id"), "הצעה מהקיטלוג הקיים", "staging_product_group", "product")
    for catalog_id in item.get("candidate_catalog_ids") or []:
        add_candidate(catalog_id, "הצעה מהקיטלוג הקיים", "staging_cluster", "product")
    add_candidate(pg.get("candidate_design_id") or item.get("candidate_design_id"), "עיצוב קיים לבדיקה", "staging_cluster", "design")

    raw_candidates = pg.get("detector_candidates") or item.get("detector_candidates") or []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source") or (raw.get("detectorEvidence") or {}).get("source") or raw.get("provenance")
        if source not in DETECTOR_CANDIDATE_SOURCES:
            continue
        candidate_id = raw.get("product_id") or raw.get("catalog_id") or raw.get("id")
        kind = "design" if "design" in str(candidate_id).lower() or candidate_id == pg.get("candidate_design_id") else "product"
        add_candidate(
            candidate_id,
            "הצעה ויזואלית לבדיקה",
            source,
            kind,
            rank=raw.get("rank"),
            score=raw.get("score"),
            margin=raw.get("margin"),
        )
    return candidates


def build_group(item: dict, pg: dict, category: str, active_index: int, stats: dict) -> dict | None:
    photos = []
    copied_sources = set()
    for asset_index, asset in enumerate(pg.get("assets") or [], start=1):
        source_path = asset.get("source_path")
        if not source_path:
            continue
        src, source_kind = find_source_file(pg["group_id"], source_path)
        if not src:
            stats["missing_files"].append({"group": pg["group_id"], "sourcePath": source_path})
            continue
        if src in copied_sources:
            continue
        copied_sources.add(src)
        increment(stats["source_counts"], source_kind)
        out_name = out_file_name(pg["group_id"], source_path, asset_index)
        if source_kind == "source_zip":
            zip_member = str(src)
            with zipfile.ZipFile(SOURCE_ZIP) as archive:
                with archive.open(zip_member) as source_fp, (OUT / out_name).open("wb") as out_fp:
                    shutil.copyfileobj(source_fp, out_fp)
        else:
            shutil.copy2(src, OUT / out_name)
        photos.append({
            "id": asset.get("asset_id") or f"{pg['group_id']}_photo_{asset_index:02d}",
            "src": f"/real-data/{out_name}",
            "sourcePath": source_path,
            "sourceKind": source_kind,
            "jewelryType": asset.get("jewelry_type"),
            "metalColor": asset.get("metal_color"),
            "stoneType": asset.get("stone_type"),
            "stoneColor": asset.get("stone_color"),
        })

    if not photos:
        increment(stats["skipped_status_counts"], "actionable_missing_images")
        return None

    status = pg.get("status")
    candidates = candidate_records(item, pg)
    product_candidates = [candidate for candidate in candidates if candidate.get("kind") != "design"]
    design_candidates = [candidate for candidate in candidates if candidate.get("kind") == "design"]
    cataloged_existing = is_cataloged_existing_product(category, status, product_candidates)
    ptype = "attach_to_existing_product" if cataloged_existing else proposal_type(category, status, pg)
    initial_stage = initial_stage_for_group(category, status, product_candidates, len(photos))
    group = {
        "id": f"active-{active_index:03d}",
        "title": f"קבוצת raw-intake {active_index}",
        "subtitle": summarize_assets(pg),
        "type": ptype,
        "confidence": "medium" if status != "pending_review" else "low",
        "photos": photos,
        "evidence": f"{len(photos)} תמונות זמינות · {STATUS_HE.get(status, status)}",
        "recommended": recommended_action(ptype),
        "initialStage": initial_stage,
        "reviewIntent": "attach_existing_product_images" if cataloged_existing else "classify_product_group",
        "candidates": product_candidates,
        "designCandidates": design_candidates,
        "rawStatus": status,
        "canonicalStage": pg.get("canonical_stage"),
        "sourceFunnel": pg.get("source_funnel"),
        "identityLane": pg.get("identity_lane"),
        "owner": pg.get("owner"),
        "blocker": pg.get("blocker"),
        "nextAction": pg.get("next_action"),
        "rawCategory": category,
        "sourceRef": pg["group_id"],
    }
    return group


def main() -> None:
    data = json.loads(STAGING.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    preserved_files = {}
    for pattern in PRESERVED_REAL_DATA_PATTERNS:
        for old in OUT.glob(pattern):
            if old.is_file():
                preserved_files[old.name] = old.read_bytes()
    for old in OUT.glob("*"):
        if old.is_file():
            old.unlink()
    for name, content in preserved_files.items():
        (OUT / name).write_bytes(content)

    groups = []
    stats = initial_stats(data)
    for category, items in data.get("categories", {}).items():
        for item in items:
            for pg in item.get("product_groups", []):
                status = pg.get("status")
                stats["considered_product_groups"] += 1
                if not is_mobile_actionable(pg):
                    skip_key = pg.get("canonical_stage") or status or "unknown"
                    increment(stats["skipped_status_counts"], skip_key)
                    continue
                stats["actionable_product_groups"] += 1
                group = build_group(item, pg, category, len(groups) + 1, stats)
                if group:
                    groups.append(group)

    groups.sort(key=group_sort_key)
    for index, group in enumerate(groups, start=1):
        group["id"] = f"active-{index:03d}"
        group["title"] = f"תמונות למוצר קיים {index}" if group.get("reviewIntent") == "attach_existing_product_images" else f"פריט לא מקוטלג {index}"
    stats["groups_exported"] = len(groups)
    stats["photos_exported"] = sum(len(g["photos"]) for g in groups)
    stats["missing_files"] = sorted(stats["missing_files"], key=lambda item: (item["group"], item["sourcePath"]))
    payload = {
        "datasetVersion": "real-raw-intake-staging-2026-07-07-canonical-reducer-v1",
        "source": "dropbox-stav-main-2026-06-07 raw-intake staging plan from SQLite clusters/product groups",
        "stats": stats,
        "groups": groups,
    }
    data_js_text = (
        "window.STAV_DATASET_VERSION = " + json.dumps(payload["datasetVersion"], ensure_ascii=False) + ";\n"
        + "window.STAV_DATASET_SOURCE = " + json.dumps(payload["source"], ensure_ascii=False) + ";\n"
        + "window.STAV_REAL_GROUPS = " + json.dumps(groups, ensure_ascii=False, indent=2) + ";\n"
        + "window.STAV_REAL_DATASET_STATS = " + json.dumps(stats, ensure_ascii=False, indent=2) + ";\n"
        + "window.STAV_PRODUCT_INDEX = " + json.dumps(STATIC_PRODUCT_INDEX, ensure_ascii=False, indent=2) + ";\n"
    )
    DATA_JS.write_text(data_js_text)
    LEGACY_DATA_JS.write_text(data_js_text)
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
