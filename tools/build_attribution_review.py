#!/usr/bin/env python3
"""Build a small static review app for catalog attribution QA."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast

JsonDict = dict[str, Any]

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = WORKSPACE / "data" / "catalog_normalized_clean_v1" / "catalog_visual_assets.json"
DEFAULT_OUT = WORKSPACE / "review_tools" / "attribution_review"
DEFAULT_LABELS_OUT = WORKSPACE / "data" / "catalog_normalized_clean_v1" / "attribution_labels_auto.csv"


ATTRIBUTION_FLAGS = {
    "multiple_product_ids",
    "shared_across_product_folders",
    "multiple_categories",
    "folder_filename_id_mismatch",
    "missing_product_id",
}


def rel_url(path: str) -> str:
    try:
        return "/" + str(Path(path).resolve().relative_to(WORKSPACE)).replace(" ", "%20")
    except ValueError:
        return Path(path).as_uri()


def basename(path: str) -> str:
    return Path(path).name


def issue_priority(asset: JsonDict) -> tuple[int, str]:
    flags = set(asset.get("flags", []))
    if "multiple_categories" in flags:
        return (0, "category_conflict")
    if "multiple_product_ids" in flags:
        return (1, "multiple_product_ids")
    if "shared_across_product_folders" in flags:
        return (2, "shared_folder_asset")
    if "folder_filename_id_mismatch" in flags:
        return (3, "folder_filename_mismatch")
    if "missing_product_id" in flags:
        return (4, "missing_product_id")
    if "unknown" in asset.get("image_roles", []):
        return (5, "unknown_role_context")
    return (6, "other")


def product_prefixes(product_ids: list[str]) -> set[str]:
    return {product_id[:1] for product_id in product_ids if product_id}


def derived_fields(media_role: str) -> dict[str, str]:
    shared = media_role == "shared_supporting"
    supporting = media_role in {"supporting", "shared_supporting"}
    return {
        "media_role": media_role,
        "identity_eligible": "true" if media_role == "identity" else "false",
        "supports_multiple_products": "true" if shared else "false",
        "catalog_media_eligible": "true" if media_role not in {"", "exclude", "wrong_product"} else "false",
        "clustering_policy": (
            "can_link_product_identity"
            if media_role == "identity"
            else "attach_after_identity_clustering"
            if supporting
            else ""
        ),
    }


def auto_label_for(asset: JsonDict, issue: str) -> JsonDict | None:
    product_ids = asset.get("product_ids", [])
    prefixes = product_prefixes(product_ids)
    if issue == "category_conflict" or len(prefixes) > 1:
        return {
            "asset_id": asset["asset_id"],
            "decision_source": "auto",
            "correct_product_ids": ",".join(product_ids),
            "notes": "Auto-labeled from domain rule: model/set image contains jewelry from multiple product categories.",
            **derived_fields("shared_supporting"),
        }
    return None


def build_queue(input_path: Path) -> JsonDict:
    payload = cast("JsonDict", json.loads(input_path.read_text(encoding="utf-8")))
    assets = payload["visual_assets"]

    by_product: dict[str, list[JsonDict]] = defaultdict(list)
    for asset in assets:
        for product_id in asset.get("product_ids", []):
            by_product[product_id].append(asset)

    rows: list[JsonDict] = []
    auto_labels: list[JsonDict] = []
    for asset in assets:
        flags = set(asset.get("flags", []))
        product_ids = asset.get("product_ids", [])
        roles = set(asset.get("image_roles", []))
        include = bool(flags & ATTRIBUTION_FLAGS)
        include = include or (len(product_ids) > 1)
        include = include or ("unknown" in roles and (len(product_ids) > 1 or "shared_across_product_folders" in flags))
        if not include:
            continue

        _, issue = issue_priority(asset)
        auto_label = auto_label_for(asset, issue)
        if auto_label:
            auto_labels.append(auto_label)
            continue
        related_products: list[JsonDict] = []
        for product_id in product_ids:
            related_products.append(
                {
                    "product_id": product_id,
                    "asset_count": len(by_product[product_id]),
                    "folders": sorted({folder for a in by_product[product_id] for folder in a.get("product_folders", [])}),
                    "roles": sorted({role for a in by_product[product_id] for role in a.get("image_roles", [])}),
                }
            )

        rows.append(
            {
                "asset_id": asset["asset_id"],
                "display_title": " / ".join(product_ids) or basename(asset["preferred_path"]),
                "filename": basename(asset["preferred_path"]),
                "rel_paths": [occurrence.get("rel_path", "") for occurrence in asset.get("occurrences", [])],
                "occurrences": [
                    {
                        "filename": occurrence.get("filename", basename(occurrence.get("path", ""))),
                        "product_ids": occurrence.get("product_ids", []),
                        "folder": occurrence.get("product_folder", ""),
                        "rel_path": occurrence.get("rel_path", ""),
                        "kind": occurrence.get("export_kind", ""),
                        "role": occurrence.get("image_role", ""),
                    }
                    for occurrence in asset.get("occurrences", [])
                ],
                "issue": issue,
                "category": asset.get("category", ""),
                "product_ids": product_ids,
                "folder_product_ids": asset.get("folder_product_ids", []),
                "filename_product_ids": asset.get("filename_product_ids", []),
                "product_folders": asset.get("product_folders", []),
                "image_roles": asset.get("image_roles", []),
                "export_kinds": asset.get("export_kinds", []),
                "flags": asset.get("flags", []),
                "occurrence_count": asset.get("occurrence_count", 0),
                "image_url": rel_url(asset["preferred_path"]),
                "quality_url": rel_url(asset.get("quality_path") or asset["preferred_path"]),
                "preferred_path": asset["preferred_path"],
                "shot_keys": asset.get("shot_keys", []),
                "related_products": related_products,
                "suggested_decision": suggested_decision(asset),
            }
        )

    rows.sort(key=lambda row: (issue_priority(row)[0], row["category"], row["asset_id"]))

    stats: JsonDict = {
        "total_assets": len(assets),
        "review_assets": len(rows),
        "auto_labeled_assets": len(auto_labels),
        "manual_review_assets": len(rows),
        "issues": Counter(row["issue"] for row in rows),
        "flags": Counter(flag for row in rows for flag in row["flags"]),
    }
    return {"stats": stats, "items": rows, "auto_labels": auto_labels}


def suggested_decision(asset: JsonDict) -> str:
    flags = set(asset.get("flags", []))
    roles = set(asset.get("image_roles", []))
    product_ids = asset.get("product_ids", [])
    categories = {pid[:1] for pid in product_ids if pid}
    if len(categories) > 1 or "multiple_categories" in flags:
        return "supporting_or_set"
    if len(product_ids) > 1 and "model_or_lifestyle" in roles:
        return "supporting_or_set"
    if len(product_ids) > 1:
        return "choose_primary_or_variant"
    if "missing_product_id" in flags:
        return "assign_or_exclude"
    return "verify"


def write_data_js(out_dir: Path, data: JsonDict) -> None:
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    (out_dir / "review-data.js").write_text(f"window.REVIEW_DATA = {encoded};\n", encoding="utf-8")


def write_auto_labels_csv(path: Path, data: JsonDict) -> None:
    labels = data.get("auto_labels", [])
    fieldnames = [
        "asset_id",
        "decision_source",
        "correct_product_ids",
        "media_role",
        "identity_eligible",
        "supports_multiple_products",
        "catalog_media_eligible",
        "clustering_policy",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(labels)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--labels-out", type=Path, default=DEFAULT_LABELS_OUT)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    data = build_queue(args.input)
    write_data_js(args.out, data)
    write_auto_labels_csv(args.labels_out, data)
    print(f"Review assets: {data['stats']['review_assets']}")
    print(f"Auto-labeled assets: {data['stats']['auto_labeled_assets']}")
    print(f"Manual review assets: {data['stats']['manual_review_assets']}")
    print(f"Wrote: {args.out / 'review-data.js'}")
    print(f"Wrote: {args.labels_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
