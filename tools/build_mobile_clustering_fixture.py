#!/usr/bin/env python3
"""Build a real-data fixture for the Stav mobile clustering prototype.

Read-only over raw-intake staging artifacts. Copies local image files into
web/mobile-clustering-prototype/real-data and writes data.js consumed by app.js.
No Shopify/Airtable/Drive/detector DB writes.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT.parents[1] / "workbench/package-prep/raw-intake/dropbox-stav-main-2026-06-07"
STAGING = RAW_ROOT / "staging/raw_intake_staging_plan.json"
SOURCE = RAW_ROOT / "source"
SELECTED = RAW_ROOT / "selected"
EXTRACTED = RAW_ROOT / "detector-refresh-2026-07-03/extracted"
OUT = ROOT / "web/mobile-clustering-prototype/real-data"
DATA_JS = ROOT / "web/mobile-clustering-prototype/data.js"
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

ACTIONABLE_STATUSES = {
    "needs_parent_fact_mapping",
    "parent_facts_captured",
    "pending_review",
}
STATUS_HE = {
    "needs_parent_fact_mapping": "צריך השלמת פרטים",
    "parent_facts_captured": "יש פרטים — צריך אימות/המשך טיפול",
    "pending_review": "צריך בדיקת שיוך",
    "package_sent_waiting_parent_approval": "כבר נשלחה חבילת אישור — לא שייך לאימות תמונות",
    "approved_upload_blocked": "אושר להעלאה — ממשיך לשערי העלאה",
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


def proposal_type(category: str, status: str, pg: dict) -> str:
    if status == "pending_review":
        return "split_likely" if pg.get("member_count", 0) > 2 else "same_new_product_group"
    if category == "new_product_existing_design":
        return "same_design_sibling"
    return "same_new_product_group"


def recommended_action(ptype: str) -> str:
    return {
        "same_design_sibling": "לבדוק הבדל / אותו עיצוב",
        "split_likely": "לפתוח ולוודא קבוצה",
        "same_new_product_group": "אישור מהיר כמוצר חדש",
    }.get(ptype, "לפתוח")


def initial_stats(data: dict) -> dict:
    return {
        "source": str(STAGING.relative_to(ROOT.parents[1])),
        "considered_product_groups": 0,
        "actionable_product_groups": 0,
        "groups_exported": 0,
        "photos_exported": 0,
        "skipped_status_counts": {},
        "missing_files": [],
        "source_counts": {"staging_asset": 0, "selected_fallback": 0},
        "generated_at": data.get("generated_at"),
    }


def increment(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def group_sort_key(group: dict) -> tuple[str, str]:
    return (group.get("rawCategory") or "", group.get("sourceRef") or group.get("id") or "")


def out_file_name(group_id: str, source_path: str, index: int) -> str:
    source_stem = Path(source_path).stem.replace(" ", "_")
    return f"{group_id}__{index:02d}__{source_stem}.jpg"


def candidate_records(item: dict, pg: dict) -> list[dict]:
    candidates = []
    seen = set()
    for candidate_id in [
        pg.get("candidate_product_id"),
        item.get("candidate_catalog_id"),
        pg.get("candidate_design_id"),
        item.get("candidate_design_id"),
    ] + list(item.get("candidate_catalog_ids") or []):
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        candidate_type = "עיצוב דומה" if "design" in str(candidate_id).lower() or candidate_id == pg.get("candidate_design_id") else "מוצר דומה"
        candidates.append({
            "id": candidate_id,
            "label": f"{candidate_id} · {candidate_type}",
            "meta": "מועמד מהגלאי — לא החלטה",
        })
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
    ptype = proposal_type(category, status, pg)
    return {
        "id": f"active-{active_index:03d}",
        "title": f"קבוצת raw-intake {active_index}",
        "subtitle": summarize_assets(pg),
        "type": ptype,
        "confidence": "medium" if status != "pending_review" else "low",
        "photos": photos,
        "evidence": f"{len(photos)} תמונות זמינות · {STATUS_HE.get(status, status)}",
        "recommended": recommended_action(ptype),
        "candidates": candidate_records(item, pg),
        "rawStatus": status,
        "rawCategory": category,
        "sourceRef": pg["group_id"],
        "candidateProductId": pg.get("candidate_product_id") or item.get("candidate_catalog_id"),
        "candidateDesignId": pg.get("candidate_design_id") or item.get("candidate_design_id"),
    }


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
                if status not in ACTIONABLE_STATUSES:
                    increment(stats["skipped_status_counts"], status or "unknown")
                    continue
                stats["actionable_product_groups"] += 1
                group = build_group(item, pg, category, len(groups) + 1, stats)
                if group:
                    groups.append(group)

    groups.sort(key=group_sort_key)
    for index, group in enumerate(groups, start=1):
        group["id"] = f"active-{index:03d}"
        group["title"] = f"פריט לא מקוטלג {index}"
    stats["groups_exported"] = len(groups)
    stats["photos_exported"] = sum(len(g["photos"]) for g in groups)
    stats["missing_files"] = sorted(stats["missing_files"], key=lambda item: (item["group"], item["sourcePath"]))
    payload = {
        "datasetVersion": "real-raw-intake-2026-07-06-v2",
        "source": "dropbox-stav-main-2026-06-07 raw-intake staging, read-only active fixture",
        "stats": stats,
        "groups": groups,
    }
    DATA_JS.write_text(
        "window.STAV_DATASET_VERSION = " + json.dumps(payload["datasetVersion"], ensure_ascii=False) + ";\n"
        + "window.STAV_DATASET_SOURCE = " + json.dumps(payload["source"], ensure_ascii=False) + ";\n"
        + "window.STAV_REAL_GROUPS = " + json.dumps(groups, ensure_ascii=False, indent=2) + ";\n"
        + "window.STAV_REAL_DATASET_STATS = " + json.dumps(stats, ensure_ascii=False, indent=2) + ";\n"
        + "window.STAV_PRODUCT_INDEX = " + json.dumps(STATIC_PRODUCT_INDEX, ensure_ascii=False, indent=2) + ";\n"
    )
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
