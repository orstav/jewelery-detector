#!/usr/bin/env python3
"""Build a real-data fixture for the Vercel mobile clustering prototype.

Read-only over raw-intake staging artifacts. Copies a small image sample into
web/mobile-clustering-prototype/public/real-data and writes data.js consumed by
app.js. No Shopify/Airtable/Drive/detector DB writes.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT.parents[1] / "workbench/package-prep/raw-intake/dropbox-stav-main-2026-06-07"
STAGING = RAW_ROOT / "staging/raw_intake_staging_plan.json"
SELECTED = RAW_ROOT / "selected"
EXTRACTED = RAW_ROOT / "detector-refresh-2026-07-03/extracted"
OUT = ROOT / "web/mobile-clustering-prototype/real-data"
DATA_JS = ROOT / "web/mobile-clustering-prototype/data.js"

ALLOWED_STATUSES = {
    "needs_parent_fact_mapping",
    "parent_facts_captured",
    "pending_review",
}
STATUS_HE = {
    "needs_parent_fact_mapping": "לא מקוטלג · חסרים פרטים",
    "parent_facts_captured": "לא מקוטלג · יש חלק מהפרטים",
    "pending_review": "לא מקוטלג · לבדיקה",
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
    "pearl": "פנינה",
    "diamond": "יהלום",
    "emerald": "אמרלד",
    "faceted_gemstone": "אבן צבעונית",
    "mixed": "אבנים שונות",
    "moonstone": "מונסטון",
    "none": "ללא אבן",
}


def safe_name(source_path: str) -> str:
    return source_path.replace("/", "__")


def find_source_file(source_path: str) -> Path | None:
    candidate = EXTRACTED / safe_name(source_path)
    if candidate.exists():
        return candidate
    basename = Path(source_path).name
    matches = list(SELECTED.rglob(basename)) + list(EXTRACTED.rglob(basename))
    jpgs = [m for m in matches if m.suffix.lower() in {".jpg", ".jpeg"}]
    return jpgs[0] if jpgs else (matches[0] if matches else None)


def summarize_assets(pg: dict) -> str:
    first = (pg.get("assets") or [{}])[0]
    bits = []
    jt = TYPE_HE.get(first.get("jewelry_type"), first.get("jewelry_type"))
    metal = METAL_HE.get(first.get("metal_color"), first.get("metal_color"))
    stone = STONE_HE.get(first.get("stone_type"), first.get("stone_type"))
    for value in [jt, metal, stone]:
        if value and value != "none":
            bits.append(value)
    return " · ".join(bits) if bits else (pg.get("evidence") or {}).get("description") or "תכשיט לא מקוטלג"


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


def main() -> None:
    data = json.loads(STAGING.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*"):
        if old.is_file():
            old.unlink()

    groups = []
    stats = {"considered_product_groups": 0, "groups_exported": 0, "photos_exported": 0, "missing_files": []}
    for category, items in data.get("categories", {}).items():
        for item in items:
            for pg in item.get("product_groups", []):
                status = pg.get("status")
                if status not in ALLOWED_STATUSES:
                    continue
                stats["considered_product_groups"] += 1
                photos = []
                for asset in pg.get("assets") or []:
                    src = find_source_file(asset["source_path"])
                    if not src:
                        stats["missing_files"].append(asset["source_path"])
                        continue
                    out_name = f"{pg['group_id']}__{Path(asset['source_path']).stem}.jpg".replace(" ", "_")
                    out_path = OUT / out_name
                    shutil.copy2(src, out_path)
                    photos.append({
                        "id": asset["asset_id"],
                        "src": f"/real-data/{out_name}",
                        "sourcePath": asset["source_path"],
                        "jewelryType": asset.get("jewelry_type"),
                        "metalColor": asset.get("metal_color"),
                        "stoneType": asset.get("stone_type"),
                        "stoneColor": asset.get("stone_color"),
                    })
                if not photos:
                    continue
                ptype = proposal_type(category, status, pg)
                candidate = pg.get("candidate_product_id") or item.get("candidate_catalog_id")
                candidates = []
                if candidate:
                    candidates.append({"id": candidate, "label": f"{candidate} · מוצר/עיצוב דומה", "meta": "מועמד מהגלאי — לא החלטה"})
                groups.append({
                    "id": pg["group_id"],
                    "title": f"{STATUS_HE[status]} · {summarize_assets(pg)}",
                    "type": ptype,
                    "confidence": "medium" if status != "pending_review" else "low",
                    "photos": photos,
                    "evidence": f"{len(photos)} תמונות אמיתיות מדרופבוקס · {STATUS_HE[status]}",
                    "recommended": recommended_action(ptype),
                    "candidates": candidates,
                    "rawStatus": status,
                    "rawCategory": category,
                    "sourceRef": pg["group_id"],
                })
                if len(groups) >= 10:
                    break
            if len(groups) >= 10:
                break
        if len(groups) >= 10:
            break

    if len(groups) < 10:
        for pg_dir in sorted(SELECTED.glob("pg_*")):
            if not pg_dir.is_dir():
                continue
            if any(group.get("sourceRef") == pg_dir.name for group in groups):
                continue
            image_paths = []
            for image_path in sorted(pg_dir.glob("*.jpg")):
                lower_name = image_path.name.lower()
                if any(skip in lower_name for skip in ["sheet", "gallery", "approval", "dryrun"]):
                    continue
                image_paths.append(image_path)
            if len(image_paths) < 2:
                continue
            photos = []
            for idx, src in enumerate(image_paths[:3], start=1):
                out_name = f"{pg_dir.name}__selected_{idx:02d}.jpg"
                out_path = OUT / out_name
                shutil.copy2(src, out_path)
                photos.append({
                    "id": f"{pg_dir.name}_photo_{idx:02d}",
                    "src": f"/real-data/{out_name}",
                    "sourcePath": str(src.relative_to(RAW_ROOT)),
                })
            groups.append({
                "id": pg_dir.name,
                "title": f"לא מקוטלג · קבוצה מתיקיית עבודה · {pg_dir.name}",
                "type": "same_new_product_group",
                "confidence": "low",
                "photos": photos,
                "evidence": f"{len(photos)} תמונות אמיתיות מתיקיית raw-intake selected",
                "recommended": "לפתוח ולוודא קבוצה",
                "candidates": [],
                "rawStatus": "selected_unresolved_sample",
                "rawCategory": "selected_filesystem_fallback",
                "sourceRef": pg_dir.name,
            })
            if len(groups) >= 10:
                break

    stats["groups_exported"] = len(groups)
    stats["photos_exported"] = sum(len(g["photos"]) for g in groups)
    payload = {
        "datasetVersion": "real-raw-intake-2026-07-06-v1",
        "source": "dropbox-stav-main-2026-06-07 raw-intake staging, read-only sample",
        "stats": stats,
        "groups": groups,
    }
    DATA_JS.write_text(
        "window.STAV_DATASET_VERSION = " + json.dumps(payload["datasetVersion"], ensure_ascii=False) + ";\n"
        + "window.STAV_REAL_GROUPS = " + json.dumps(groups, ensure_ascii=False, indent=2) + ";\n"
        + "window.STAV_REAL_DATASET_STATS = " + json.dumps(stats, ensure_ascii=False, indent=2) + ";\n"
    )
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
