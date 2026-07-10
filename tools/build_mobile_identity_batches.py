#!/usr/bin/env python3
"""Build all Stav mobile identity-review batches and a 100% source coverage registry.

Inputs are read-only: migrated Stav SQLite and the raw source ZIP.
Outputs are static prototype assets only. No Airtable/Drive/Shopify/WhatsApp writes.

Policy:
- `web` assets with unresolved identity statuses become Dalia review cards.
- already closed/downstream `web` assets remain accounted but are not shown again.
- `print`, `png`, and `fix` assets are support/version assets; they are routed to
  deterministic mapping lanes instead of being presented as separate products.
- every source asset must be assigned exactly one coverage lane.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get(
    "STAV_WORKSPACE",
    "/home/server/.hermes/profiles/hermes-hal-9000/workspace/openclaw-hal-import/workspace",
))
SQLITE = Path(os.environ.get("STAV_SQLITE", WORKSPACE / "state/stav/stav.sqlite"))
RAW_ROOT = Path(os.environ.get(
    "STAV_RAW_ROOT",
    WORKSPACE / "workbench/package-prep/raw-intake/dropbox-stav-main-2026-06-07",
))
SOURCE_ZIP = Path(os.environ.get("STAV_SOURCE_ZIP", RAW_ROOT / "source/stav.zip"))
OUT_ROOT = ROOT / "web/mobile-clustering-prototype/public"
REAL_DATA = OUT_ROOT / "real-data"
BATCH_ROOT = OUT_ROOT / "batches"
DATA_JS = OUT_ROOT / "data.js"
LEGACY_DATA_JS = ROOT / "web/mobile-clustering-prototype/data.js"
INVENTORY = BATCH_ROOT / "index.json"
GLOBAL_COVERAGE = BATCH_ROOT / "coverage.json"
DEFAULT_BATCH = "dropbox-2025-03-19-web"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

DALIA_REVIEW_STATUSES = {
    "dropbox_possible_duplicate_visual_confirm",
    "dropbox_likely_existing_visual_confirm",
    "dropbox_new_candidate_needs_review",
    "pending_review",
    "identity_ambiguous",
    "unmapped",
}
TERMINAL_STATUSES = {
    "published_active_validated",
    "dead_or_merged",
}
DOWNSTREAM_STATUSES = {
    "ready_for_package",
    "parent_facts_captured",
    "draft_created",
    "dropbox_proven_existing_drive_and_shopify",
    "dropbox_proven_existing_drive_check_shopify",
}
SUPPORT_ROLES = {"print", "png", "fix"}


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value or "")]


def batch_parts(source_path: str) -> tuple[str, str]:
    parts = (source_path or "").split("/")
    date = parts[0] if parts else "unknown"
    role = parts[1].lower() if len(parts) > 1 else "unknown"
    return date, role


def batch_id(date: str, role: str = "web") -> str:
    return f"dropbox-{date}-{role}"


def output_name(source_path: str) -> str:
    p = Path(source_path)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem)
    return f"src_{safe}{p.suffix.lower()}"


def support_match_key(source_path: str) -> str:
    stem = Path(source_path).stem.lower()
    stem = re.sub(r"\b(print|web|png|fix|res|1500|3000|high|low)\b", "", stem)
    stem = re.sub(r"[^a-z0-9]+", "", stem)
    return stem


def open_db() -> sqlite3.Connection:
    if not SQLITE.exists():
        raise SystemExit(f"SQLite not found: {SQLITE}")
    con = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(
        """
        select a.asset_id, a.source_path, a.source_date, a.role_path,
               a.asset_status, a.width, a.height, a.sha256, a.is_fix,
               pga.group_id, pg.status as group_status,
               pg.candidate_product_id, pg.candidate_design_id,
               pg.cluster_id, pg.member_count
        from assets a
        left join product_group_assets pga on pga.asset_id = a.asset_id
        left join product_groups pg on pg.group_id = pga.group_id
        order by a.source_path, a.asset_id
        """
    )]


def reset_outputs() -> None:
    REAL_DATA.mkdir(parents=True, exist_ok=True)
    BATCH_ROOT.mkdir(parents=True, exist_ok=True)
    preserved = {}
    for path in REAL_DATA.glob("catalog_*"):
        if path.is_file():
            preserved[path.name] = path.read_bytes()
    detector = REAL_DATA / "mobile_detector_candidates.json"
    if detector.exists():
        preserved[detector.name] = detector.read_bytes()
    for directory in (REAL_DATA, BATCH_ROOT):
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
                elif directory == BATCH_ROOT or path.name not in preserved:
                    path.unlink()
    for name, payload in preserved.items():
        (REAL_DATA / name).write_bytes(payload)


def load_legacy_helpers():
    # Reuse the existing catalog-index builder. Its outputs are static read-only
    # thumbnails; the mobile workflow never writes to catalog systems.
    import importlib.util
    path = ROOT / "tools/build_mobile_identity_fixture.py"
    spec = importlib.util.spec_from_file_location("mobile_identity_legacy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.OUT = REAL_DATA
    module.SQLITE = SQLITE
    module.WORKSPACE = WORKSPACE
    module.READINESS_REPORT = WORKSPACE / "workbench/readiness-validator/readiness-report-v1.json"
    module.DETECTOR_CANDIDATES = REAL_DATA / "mobile_detector_candidates.json"
    return module


def zip_index() -> tuple[zipfile.ZipFile, dict[str, str]]:
    if not SOURCE_ZIP.exists():
        raise SystemExit(f"Source ZIP not found: {SOURCE_ZIP}")
    archive = zipfile.ZipFile(SOURCE_ZIP)
    exact: dict[str, str] = {}
    for name in archive.namelist():
        if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        clean = name.lstrip("/")
        exact.setdefault(clean, name)
        parts = clean.split("/")
        for index in range(len(parts)):
            suffix = "/".join(parts[index:])
            exact.setdefault(suffix, name)
    return archive, exact


def asset_lane(row: dict[str, Any], web_keys: set[tuple[str, str]]) -> tuple[str, str]:
    date, role = batch_parts(row.get("source_path") or "")
    status = row.get("group_status") or "unmapped"
    if role in SUPPORT_ROLES:
        key = support_match_key(row.get("source_path") or "")
        if key and (date, key) in web_keys:
            return "support_linked_to_web", "support/derivative asset linked by date + normalized filename"
        return "support_mapping_pending", "support/derivative asset needs deterministic version mapping"
    if role != "web":
        return "non_web_source_routed", f"source role {role} routed outside Dalia identity review"
    if status in TERMINAL_STATUSES:
        return "terminal_closed", status
    if status in DOWNSTREAM_STATUSES:
        return "downstream_existing_workflow", status
    if status in DALIA_REVIEW_STATUSES or not row.get("group_id"):
        return "dalia_identity_review", status
    return "system_review_pending", status


def assumption_for(status: str, count: int) -> str:
    if count > 1:
        return "HAL קיבץ את התמונות יחד. קודם מאשרים אם כולן אותו תכשיט; אם לא, מפצלים בלי לאבד תמונה."
    if status == "dropbox_likely_existing_visual_confirm":
        return "ייתכן שזה מוצר שכבר קיים. הגלאי מציע כיוון בלבד ודליה מאשרת לפי התמונות."
    if status == "dropbox_possible_duplicate_visual_confirm":
        return "ייתכן שזו תמונה של מוצר קיים או תמונה דומה. צריך לבחור לפי זהות התכשיט."
    if status == "dropbox_new_candidate_needs_review":
        return "ייתכן שזה מוצר חדש. צריך לבדוק מול הקטלוג לפני יצירת זהות חדשה."
    return "התמונה דורשת החלטת זהות: מוצר קיים, מוצר חדש או לא בטוחה."


def build_all() -> dict[str, Any]:
    reset_outputs()
    con = open_db()
    rows = load_rows(con)
    helpers = load_legacy_helpers()
    product_index = helpers.build_product_index(con)
    product_by_id = {str(item["id"]): item for item in product_index}
    detector_export = helpers.load_detector_candidate_export()
    archive, members = zip_index()

    web_keys = {
        (batch_parts(row.get("source_path") or "")[0], support_match_key(row.get("source_path") or ""))
        for row in rows if batch_parts(row.get("source_path") or "")[1] == "web"
    }
    coverage_rows = []
    web_rows_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        date, role = batch_parts(row.get("source_path") or "")
        lane, reason = asset_lane(row, web_keys)
        coverage_rows.append({
            "asset_id": row["asset_id"], "source_path": row.get("source_path"),
            "source_date": date, "role": role, "product_group_id": row.get("group_id"),
            "group_status": row.get("group_status"), "lane": lane, "reason": reason,
        })
        if role == "web":
            web_rows_by_date[date].append(row)

    by_asset_lane = {row["asset_id"]: row["lane"] for row in coverage_rows}
    batches: dict[str, dict[str, Any]] = {}
    batch_index = []
    copied_assets: set[str] = set()

    for date, date_rows in sorted(web_rows_by_date.items()):
        bid = batch_id(date)
        batch_dir = BATCH_ROOT / bid
        batch_dir.mkdir(parents=True, exist_ok=True)
        source_assets = []
        review_rows = [row for row in date_rows if by_asset_lane[row["asset_id"]] == "dalia_identity_review"]
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in review_rows:
            grouped[row.get("group_id") or f"asset:{row['asset_id']}"] .append(row)
        review_cards = []
        blocked_assets = []
        batch_detector_card = 0
        for group_key, group_rows in sorted(grouped.items(), key=lambda item: natural_key(min(row["source_path"] for row in item[1]))):
            photos = []
            for row in sorted(group_rows, key=lambda item: natural_key(item["source_path"])):
                member = members.get((row.get("source_path") or "").lstrip("/"))
                if not member:
                    blocked_assets.append({"asset_id": row["asset_id"], "source_path": row.get("source_path"), "reason": "source_image_missing_from_zip"})
                    continue
                filename = output_name(row["source_path"])
                destination = batch_dir / filename
                if not destination.exists():
                    with archive.open(member) as src, destination.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                copied_assets.add(row["asset_id"])
                photos.append({
                    "id": row["asset_id"], "src": f"/batches/{bid}/{filename}",
                    "sourcePath": row["source_path"], "sourceKind": "source_zip",
                    "role": "identity_review_photo",
                })
            if not photos:
                continue
            batch_detector_card += 1
            lookup_card_id = f"card-{batch_detector_card:03d}"
            candidates = []
            if bid == DEFAULT_BATCH:
                candidates = helpers.attach_detector_candidates(lookup_card_id, detector_export, product_by_id)
            status = group_rows[0].get("group_status") or "unmapped"
            review_cards.append({
                "id": f"{bid}-card-{batch_detector_card:03d}",
                "sourceRef": group_rows[0].get("group_id") or group_rows[0]["asset_id"],
                "title": f"{date} · פריט {batch_detector_card}",
                "subtitle": "תכשיט לבדיקה",
                "initialStage": "cluster_photos" if len(photos) > 1 else "product_identity",
                "reviewIntent": "identity_decision",
                "halAssumption": assumption_for(status, len(photos)),
                "rawStatus": status,
                "detectorStatus": "candidates" if candidates else "no_candidates",
                "detectorSource": "detector_db_embedding_topk" if candidates else None,
                "photos": photos,
                "candidates": candidates,
                "existingCandidates": candidates,
                "existingCandidate": candidates[0] if candidates else None,
            })

        for row in sorted(date_rows, key=lambda item: natural_key(item["source_path"])):
            source_assets.append({
                "asset_id": row["asset_id"], "source_path": row["source_path"],
                "role_path": row.get("role_path"), "asset_status": row.get("asset_status"),
                "width": row.get("width"), "height": row.get("height"), "sha256": row.get("sha256"),
                "product_group_id": row.get("group_id"), "group_status": row.get("group_status"),
                "coverage_lane": by_asset_lane[row["asset_id"]],
            })
        lane_counts = Counter(by_asset_lane[row["asset_id"]] for row in date_rows)
        reviewable_ids = {photo["id"] for card in review_cards for photo in card["photos"]}
        blocked_ids = {item["asset_id"] for item in blocked_assets}
        auto_accounted = [
            {"asset_id": row["asset_id"], "source_path": row["source_path"], "lane": by_asset_lane[row["asset_id"]], "reason": row.get("group_status") or by_asset_lane[row["asset_id"]]}
            for row in date_rows if row["asset_id"] not in reviewable_ids and row["asset_id"] not in blocked_ids
        ]
        manifest = {
            "batch_id": bid, "label": date, "source_folder": f"{date}/web",
            "source": str(SQLITE), "source_zip": str(SOURCE_ZIP),
            "source_assets": source_assets, "review_cards": review_cards,
            "auto_accounted_assets": auto_accounted, "blocked_assets": blocked_assets,
            "coverage": {
                "expected": len(source_assets), "seen": len(source_assets),
                "reviewable": len(reviewable_ids), "review_cards": len(review_cards),
                "auto_accounted": len(auto_accounted), "blocked": len(blocked_assets),
                "lane_counts": dict(lane_counts),
            },
            "detector": {
                "source": "detector_db_embedding_topk" if bid == DEFAULT_BATCH else None,
                "cards_with_candidates": sum(bool(card["candidates"]) for card in review_cards),
                "candidate_total": sum(len(card["candidates"]) for card in review_cards),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "no_live_writes": True,
        }
        (batch_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        batches[bid] = manifest
        batch_index.append({
            "batch_id": bid, "label": date, "source_folder": f"{date}/web",
            "source_assets": len(source_assets), "reviewable_assets": len(reviewable_ids),
            "review_cards": len(review_cards), "auto_accounted": len(auto_accounted),
            "blocked": len(blocked_assets), "complete": len(review_cards) == 0 and not blocked_assets,
            "url": f"/?batch={bid}",
        })

    # Operational priority: finish the already-tested reference batch first,
    # then work newest shoots backwards. This keeps current commerce work fresh
    # while the complete backlog remains explicit and finite.
    batch_index = sorted(
        batch_index,
        key=lambda item: (item["batch_id"] == DEFAULT_BATCH, item["label"]),
        reverse=True,
    )
    lane_counts = Counter(row["lane"] for row in coverage_rows)
    asset_ids = [row["asset_id"] for row in coverage_rows]
    if len(asset_ids) != len(set(asset_ids)):
        raise SystemExit("asset coverage contains duplicate asset ids")
    if len(asset_ids) != len(rows):
        raise SystemExit(f"coverage mismatch: {len(asset_ids)} lanes for {len(rows)} source rows")
    reviewable_total = sum(item["reviewable_assets"] for item in batch_index)
    global_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(SQLITE), "source_zip": str(SOURCE_ZIP),
        "total_assets": len(rows), "accounted_assets": len(coverage_rows),
        "coverage_valid": len(rows) == len(coverage_rows),
        "lane_counts": dict(lane_counts), "reviewable_web_assets": reviewable_total,
        "review_batches": len([item for item in batch_index if item["reviewable_assets"]]),
        "batches": batch_index, "assets": coverage_rows,
        "no_live_writes": True,
    }
    INVENTORY.write_text(json.dumps({"generated_at": global_report["generated_at"], "default_batch": DEFAULT_BATCH, "batches": batch_index}, ensure_ascii=False, indent=2), encoding="utf-8")
    GLOBAL_COVERAGE.write_text(json.dumps(global_report, ensure_ascii=False, indent=2), encoding="utf-8")

    if DEFAULT_BATCH not in batches:
        default = next((item["batch_id"] for item in batch_index if item["reviewable_assets"]), batch_index[0]["batch_id"])
    else:
        default = DEFAULT_BATCH
    default_manifest = batches[default]
    (REAL_DATA / "manifest.json").write_text(json.dumps(default_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    data_payload = {
        "dataset_version": f"all-batches-{global_report['generated_at']}",
        "default_batch": default,
        "batch_index": batch_index,
        "batches": batches,
        "product_index": product_index,
        "global_coverage": {key: value for key, value in global_report.items() if key != "assets"},
    }
    js = "\n".join([
        "window.STAV_DATASET_VERSION = " + json.dumps(data_payload["dataset_version"], ensure_ascii=False) + ";",
        "window.STAV_DEFAULT_BATCH = " + json.dumps(default, ensure_ascii=False) + ";",
        "window.STAV_BATCH_INDEX = " + json.dumps(batch_index, ensure_ascii=False, indent=2) + ";",
        "window.STAV_BATCHES = " + json.dumps(batches, ensure_ascii=False, indent=2) + ";",
        "window.STAV_GLOBAL_COVERAGE = " + json.dumps(data_payload["global_coverage"], ensure_ascii=False, indent=2) + ";",
        "window.STAV_PRODUCT_INDEX = " + json.dumps(product_index, ensure_ascii=False, indent=2) + ";",
        "window.STAV_REAL_GROUPS = window.STAV_BATCHES[window.STAV_DEFAULT_BATCH].review_cards;",
        "window.STAV_SOURCE_ASSETS = window.STAV_BATCHES[window.STAV_DEFAULT_BATCH].source_assets.filter((asset) => asset.coverage_lane === 'dalia_identity_review');",
        "window.STAV_REAL_DATASET_STATS = {...window.STAV_BATCHES[window.STAV_DEFAULT_BATCH].coverage, batch_id: window.STAV_DEFAULT_BATCH};",
        "",
    ])
    DATA_JS.write_text(js, encoding="utf-8")
    LEGACY_DATA_JS.write_text(js, encoding="utf-8")
    archive.close()
    return {
        "total_assets": len(rows), "accounted_assets": len(coverage_rows),
        "lane_counts": dict(lane_counts), "review_batches": global_report["review_batches"],
        "reviewable_web_assets": reviewable_total, "copied_review_assets": len(copied_assets),
        "default_batch": default, "output": str(INVENTORY), "no_live_writes": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary-json", help="optional summary output path")
    args = parser.parse_args()
    summary = build_all()
    if args.summary_json:
        path = Path(args.summary_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
