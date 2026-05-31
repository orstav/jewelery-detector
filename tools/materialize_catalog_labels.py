#!/usr/bin/env python3
"""Apply catalog attribution labels to the normalized catalog manifest."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


DEFAULT_BASE = Path(__file__).resolve().parents[1] / "data" / "catalog_normalized_clean_v1"


def split_ids(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("|", ",").split(",") if item.strip()]


def join_ids(values: list[str]) -> str:
    return "|".join(values)


def infer_media_role(row: dict) -> str:
    roles = set(split_ids(row.get("image_roles", "")))
    product_ids = split_ids(row.get("product_ids", ""))
    if len(product_ids) > 1:
        return "shared_supporting"
    if roles.intersection({"model_or_lifestyle", "detail_or_crop", "unknown"}):
        return "supporting"
    return "identity"


def derived_fields(media_role: str) -> dict[str, str]:
    shared = media_role == "shared_supporting"
    supporting = media_role in {"supporting", "shared_supporting"}
    return {
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


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def materialize(manifest_path: Path, labels_path: Path) -> tuple[list[dict], dict]:
    manifest = read_csv(manifest_path)
    labels = {row["asset_id"]: row for row in read_csv(labels_path)}

    rows = []
    warnings = []
    for row in manifest:
        asset_id = row["asset_id"]
        label = labels.get(asset_id)
        current_product_ids = split_ids(row.get("product_ids", ""))
        if label:
            final_product_ids = split_ids(label["correct_product_ids"])
            media_role = label["media_role"]
            label_source = label["decision_source"]
            notes = label.get("notes", "")
            derived = {
                "identity_eligible": label.get("identity_eligible", ""),
                "supports_multiple_products": label.get("supports_multiple_products", ""),
                "catalog_media_eligible": label.get("catalog_media_eligible", ""),
                "clustering_policy": label.get("clustering_policy", ""),
            }
        else:
            final_product_ids = current_product_ids
            media_role = infer_media_role(row)
            label_source = "inferred"
            notes = ""
            derived = derived_fields(media_role)

        if media_role == "identity" and len(final_product_ids) != 1:
            warnings.append({"asset_id": asset_id, "warning": "identity_requires_exactly_one_product"})
        if media_role == "shared_supporting" and len(final_product_ids) < 2:
            warnings.append({"asset_id": asset_id, "warning": "shared_supporting_has_fewer_than_two_products"})
        if not final_product_ids and media_role not in {"exclude", "needs_followup"}:
            warnings.append({"asset_id": asset_id, "warning": "eligible_asset_has_no_final_product_ids"})

        rows.append(
            {
                **row,
                "current_product_ids": join_ids(current_product_ids),
                "final_product_ids": join_ids(final_product_ids),
                "media_role": media_role,
                "identity_eligible": derived["identity_eligible"],
                "supports_multiple_products": derived["supports_multiple_products"],
                "catalog_media_eligible": derived["catalog_media_eligible"],
                "clustering_policy": derived["clustering_policy"],
                "attribution_source": label_source,
                "attribution_notes": notes,
            }
        )

    summary = {
        "manifest_assets": len(manifest),
        "labeled_assets": len(labels),
        "materialized_assets": len(rows),
        "media_roles": Counter(row["media_role"] for row in rows),
        "attribution_sources": Counter(row["attribution_source"] for row in rows),
        "identity_eligible_assets": sum(1 for row in rows if row["identity_eligible"] == "true"),
        "catalog_media_eligible_assets": sum(1 for row in rows if row["catalog_media_eligible"] == "true"),
        "warnings": warnings,
    }
    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest or args.base / "manifest.csv"
    labels_path = args.labels or args.base / "attribution_labels.csv"
    out_csv = args.out_csv or args.base / "final_labeled_manifest.csv"
    out_json = args.out_json or args.base / "final_labeled_manifest.json"
    summary_path = args.summary or args.base / "final_labeled_manifest_summary.json"

    rows, summary = materialize(manifest_path, labels_path)
    fieldnames = list(rows[0].keys()) if rows else []
    write_csv(out_csv, rows, fieldnames)
    out_json.write_text(json.dumps({"assets": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Materialized assets: {summary['materialized_assets']}")
    print(f"Identity eligible: {summary['identity_eligible_assets']}")
    print(f"Catalog media eligible: {summary['catalog_media_eligible_assets']}")
    print(f"Warnings: {len(summary['warnings'])}")
    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_json}")
    print(f"Wrote: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
