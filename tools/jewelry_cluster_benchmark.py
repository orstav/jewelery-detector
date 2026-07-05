#!/usr/bin/env python3
"""Normalize jewelry image batches into reviewable visual assets.

This is intentionally not a product clusterer yet. It builds the layer below
clustering: image occurrences grouped into logical visual assets with a preferred
file selected for later clustering/review.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import http.server
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

JsonDict = dict[str, Any]
SortKey = tuple[Any, ...]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}
SOURCE_PRIORITY = {"fixed": 0, "unfixed": 1, "reference": 2}
KIND_REVIEW_PRIORITY = {"web": 0, "png": 1, "print": 2, "other": 3}
KIND_QUALITY_PRIORITY = {"print": 0, "png": 1, "web": 2, "other": 3}
SHOT_KEY_RE = re.compile(
    r"^(?P<date>\d{8})-(?:high res|web res 1500|png 1500)(?:-(?P<num>\d+))?\.(?:jpg|jpeg|png)$",
    re.IGNORECASE,
)
CATALOG_PRODUCT_ID_RE = re.compile(r"(?<![A-Z0-9])[REN]\d{3}(?!\d)", re.IGNORECASE)
CATALOG_MANUAL_PRODUCT_ID_EXPANSIONS = {
    "R012-R014": ["R013"],
}
CATALOG_CATEGORY_ALIASES = {
    "rings": "טבעות",
    "ring": "טבעות",
    "טבעות": "טבעות",
    "earrings": "עגילים",
    "earring": "עגילים",
    "עגילים": "עגילים",
    "necklaces": "שרשראות",
    "necklace": "שרשראות",
    "שרשראות": "שרשראות",
}
CATALOG_CATEGORY_CODE = {"טבעות": "R", "עגילים": "E", "שרשראות": "N"}


@dataclass
class Occurrence:
    occurrence_id: str
    source: str
    path: str
    rel_path: str
    filename: str
    extension: str
    kind: str
    reference_cluster_id: str
    is_before_fix: bool
    size_bytes: int
    width: int | None
    height: int | None
    sha256: str
    ahash: str
    dhash: str
    shot_key: str


@dataclass
class VisualAsset:
    asset_id: str
    preferred_path: str
    quality_path: str
    reference_cluster_ids: list[str]
    sources: list[str]
    kinds: list[str]
    shot_keys: list[str]
    confidence: float
    flags: list[str]
    occurrence_count: int


class UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        a = self.find(left)
        b = self.find(right)
        if a != b:
            self.parent[b] = a


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_name_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pillow_resample_lanczos() -> Any:
    from PIL import Image

    image_module = cast("Any", Image)
    if hasattr(image_module, "Resampling"):
        return image_module.Resampling.LANCZOS
    return image_module.LANCZOS


def register_pillow_image_plugins() -> None:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return
    register_heif_opener()


def open_pillow_image(path: Path) -> Any:
    from PIL import Image

    register_pillow_image_plugins()
    return Image.open(path)


def image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        with open_pillow_image(path) as image:
            width, height = cast("tuple[int, int]", image.size)
            return int(width), int(height)
    except Exception:
        return None, None


def thumbnail_pixels(path: Path, width: int, height: int) -> list[tuple[int, int, int]]:
    from PIL import ImageOps

    with open_pillow_image(path) as image:
        thumbnail = ImageOps.exif_transpose(image).convert("RGB").resize(
            (width, height),
            pillow_resample_lanczos(),
        )
    return list(thumbnail.getdata())


def average_hash(path: Path, tmpdir: Path) -> str:
    _ = tmpdir
    try:
        pixels = thumbnail_pixels(path, 8, 8)
    except Exception:
        return ""
    grays = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    avg = sum(grays) / len(grays)
    bits = ["1" if gray >= avg else "0" for gray in grays]
    return f"{int(''.join(bits), 2):016x}"


def difference_hash(path: Path, tmpdir: Path) -> str:
    _ = tmpdir
    width = 9
    height = 8
    try:
        pixels = thumbnail_pixels(path, width, height)
    except Exception:
        return ""
    grays = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    bits: list[str] = []
    for y in range(height):
        for x in range(width - 1):
            bits.append("1" if grays[y * width + x] > grays[y * width + x + 1] else "0")
    return f"{int(''.join(bits), 2):016x}"


def hamming_hex(left: str, right: str) -> int | None:
    if not left or not right:
        return None
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def parse_shot_key(filename: str) -> str:
    match = SHOT_KEY_RE.match(filename)
    if not match:
        return ""
    number = int(match.group("num") or "0")
    return f"{match.group('date')}-{number:03d}"


def detect_kind(relative_path: Path) -> str:
    parts = [part.lower() for part in relative_path.parts]
    for kind in ("web", "png", "print"):
        if kind in parts:
            return kind
    return "other"


def is_before_fix(relative_path: Path) -> bool:
    lowered = str(relative_path).lower()
    return "before fix" in lowered or "befoer fix" in lowered


def iter_image_files(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    return sorted(path for path in base.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def catalog_product_ids(text: str) -> list[str]:
    product_ids = {match.group(0).upper() for match in CATALOG_PRODUCT_ID_RE.finditer(text)}
    normalized = text.upper()
    for marker, extra_ids in CATALOG_MANUAL_PRODUCT_ID_EXPANSIONS.items():
        if marker in normalized:
            product_ids.update(extra_ids)
    return sorted(product_ids)


def catalog_image_role(path: Path) -> str:
    lowered = " ".join(part.lower() for part in path.parts)
    if "model" in lowered or "lifestyle" in lowered or "אווירה" in lowered:
        return "model_or_lifestyle"
    if "front" in lowered or "frontal" in lowered:
        return "front"
    if "back" in lowered:
        return "back"
    if "side" in lowered:
        return "side"
    if "angled" in lowered or "angle" in lowered:
        return "angled"
    if "detail" in lowered or "crop" in lowered:
        return "detail_or_crop"
    return "unknown"


def catalog_export_kind(path: Path) -> str:
    lowered_parts = [part.lower().strip() for part in path.parts]
    suffix = path.suffix.lower()
    if suffix == ".png" or any(part == "png" for part in lowered_parts):
        return "png"
    if any(part == "print" or part.startswith("print") for part in lowered_parts) or "high res" in path.name.lower():
        return "print"
    if any(part == "web" or part.startswith("web ") for part in lowered_parts) or "web res" in path.name.lower():
        return "web"
    if "crop" in lowered_parts:
        return "crop"
    return "other"


def catalog_shot_key(path: Path) -> str:
    stem = path.stem.lower()
    stem = re.sub(r"^(עותק של|copy of)\s+", "", stem).strip()
    stem = CATALOG_PRODUCT_ID_RE.sub("", stem)
    replacements = [
        r"web[- ]?res[- ]?1500",
        r"png[- ]?1500",
        r"high[- ]?res",
        r"\bprint\b",
        r"_print\b",
        r"\barchive\b",
    ]
    for pattern in replacements:
        stem = re.sub(pattern, "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^a-z0-9א-ת]+", "-", stem)
    stem = re.sub(r"-+", "-", stem).strip("-")
    return stem or path.stem.lower()


def catalog_normalized_category(raw: str) -> str | None:
    return CATALOG_CATEGORY_ALIASES.get(raw)


def iter_catalog_image_files(root: Path, categories: set[str]) -> Iterable[tuple[str, Path, Path]]:
    for category_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        category = catalog_normalized_category(category_dir.name)
        if category not in categories:
            continue
        for path in iter_image_files(category_dir):
            yield category, category_dir, path


def catalog_product_folder(category_dir: Path, image_path: Path) -> str:
    rel = image_path.relative_to(category_dir)
    if len(rel.parts) <= 1:
        return ""
    return rel.parts[0]


def scan_source(source: str, base: Path, tmpdir: Path, start_index: int) -> list[Occurrence]:
    occurrences: list[Occurrence] = []
    for index, path in enumerate(iter_image_files(base), start=start_index):
        rel = path.relative_to(base)
        reference_cluster_id = (rel.parts[0] if len(rel.parts) > 1 else "") if source == "reference" else ""
        width, height = image_size(path)
        occurrences.append(
            Occurrence(
                occurrence_id=f"O{index:05d}",
                source=source,
                path=str(path),
                rel_path=str(rel),
                filename=path.name,
                extension=path.suffix.lower(),
                kind=detect_kind(rel),
                reference_cluster_id=reference_cluster_id,
                is_before_fix=is_before_fix(rel),
                size_bytes=path.stat().st_size,
                width=width,
                height=height,
                sha256=sha256(path),
                ahash=average_hash(path, tmpdir),
                dhash=difference_hash(path, tmpdir),
                shot_key=parse_shot_key(path.name),
            )
        )
    return occurrences


def scan_catalog(root: Path, categories: set[str], tmpdir: Path) -> list[JsonDict]:
    occurrences = []
    index = 1
    for category, category_dir, path in iter_catalog_image_files(root, categories):
        rel = path.relative_to(root)
        product_folder = catalog_product_folder(category_dir, path)
        if not product_folder:
            continue
        folder_ids = catalog_product_ids(product_folder)
        filename_ids = catalog_product_ids(path.name)
        product_ids = filename_ids or folder_ids
        width, height = image_size(path)
        occurrences.append(
            {
                "occurrence_id": f"CO{index:05d}",
                "category": category,
                "category_code": CATALOG_CATEGORY_CODE.get(category, ""),
                "path": str(path),
                "rel_path": str(rel),
                "product_folder": product_folder,
                "product_folder_path": str(Path(category) / product_folder) if product_folder else category,
                "folder_product_ids": folder_ids,
                "filename_product_ids": filename_ids,
                "product_ids": product_ids,
                "reference_cluster_id": f"{category}/{product_folder}" if product_folder else category,
                "filename": path.name,
                "extension": path.suffix.lower(),
                "export_kind": catalog_export_kind(path),
                "image_role": catalog_image_role(rel),
                "shot_key": catalog_shot_key(path),
                "size_bytes": path.stat().st_size,
                "width": width,
                "height": height,
                "sha256": sha256(path),
                "ahash": average_hash(path, tmpdir),
                "dhash": difference_hash(path, tmpdir),
            }
        )
        index += 1
    return occurrences


def catalog_asset_sort_key(group: list[JsonDict]) -> SortKey:
    preferred = choose_catalog_preferred(group, KIND_REVIEW_PRIORITY)
    return (preferred["category"], preferred["product_folder"], preferred["shot_key"], preferred["filename"])


def choose_catalog_preferred(group: list[JsonDict], kind_priority: dict[str, int]) -> JsonDict:
    return sorted(
        group,
        key=lambda occurrence: (
            kind_priority.get(occurrence["export_kind"], 99),
            occurrence["image_role"] == "model_or_lifestyle",
            occurrence["size_bytes"],
            occurrence["rel_path"],
        ),
    )[0]


def normalize_catalog_assets(occurrences: list[JsonDict]) -> tuple[list[JsonDict], dict[str, str]]:
    uf = UnionFind([occurrence["occurrence_id"] for occurrence in occurrences])
    by_sha: dict[str, list[JsonDict]] = defaultdict(list)
    by_product_shot: dict[tuple[str, tuple[str, ...], str], list[JsonDict]] = defaultdict(list)
    for occurrence in occurrences:
        by_sha[occurrence["sha256"]].append(occurrence)
        if occurrence["product_ids"] and occurrence["shot_key"]:
            by_product_shot[
                (occurrence["category"], tuple(sorted(occurrence["product_ids"])), occurrence["shot_key"])
            ].append(occurrence)
    for group in by_sha.values():
        for occurrence in group[1:]:
            uf.union(group[0]["occurrence_id"], occurrence["occurrence_id"])
    for group in by_product_shot.values():
        # Export variants of the same named shot: web/png/print.
        for occurrence in group[1:]:
            uf.union(group[0]["occurrence_id"], occurrence["occurrence_id"])

    by_root: dict[str, list[JsonDict]] = defaultdict(list)
    for occurrence in occurrences:
        by_root[uf.find(occurrence["occurrence_id"])].append(occurrence)

    assets = []
    occurrence_to_asset = {}
    for index, group in enumerate(sorted(by_root.values(), key=catalog_asset_sort_key), start=1):
        asset_id = f"CA{index:05d}"
        for occurrence in group:
            occurrence_to_asset[occurrence["occurrence_id"]] = asset_id
        preferred = choose_catalog_preferred(group, KIND_REVIEW_PRIORITY)
        quality = choose_catalog_preferred(group, KIND_QUALITY_PRIORITY)
        product_ids = sorted({pid for occurrence in group for pid in occurrence["product_ids"]})
        folder_ids = sorted({pid for occurrence in group for pid in occurrence["folder_product_ids"]})
        filename_ids = sorted({pid for occurrence in group for pid in occurrence["filename_product_ids"]})
        reference_clusters = sorted({occurrence["reference_cluster_id"] for occurrence in group})
        categories = sorted({occurrence["category"] for occurrence in group})
        product_folders = sorted({occurrence["product_folder"] for occurrence in group if occurrence["product_folder"]})
        export_kinds = sorted({occurrence["export_kind"] for occurrence in group})
        roles = sorted({occurrence["image_role"] for occurrence in group})
        shot_keys = sorted({occurrence["shot_key"] for occurrence in group if occurrence["shot_key"]})
        flags = []
        if len(categories) > 1:
            flags.append("multiple_categories")
        if len(reference_clusters) > 1:
            flags.append("shared_across_product_folders")
        if len(filename_ids) > 1 or (not filename_ids and len(product_ids) > 1):
            flags.append("multiple_product_ids")
        if folder_ids and filename_ids and set(folder_ids).isdisjoint(filename_ids):
            flags.append("folder_filename_id_mismatch")
        if not product_ids:
            flags.append("missing_product_id")
        if len(group) == 1:
            flags.append("single_occurrence")
        assets.append(
            {
                "asset_id": asset_id,
                "category": preferred["category"],
                "preferred_path": preferred["path"],
                "quality_path": quality["path"],
                "product_ids": product_ids,
                "folder_product_ids": folder_ids,
                "filename_product_ids": filename_ids,
                "reference_cluster_ids": reference_clusters,
                "product_folders": product_folders,
                "export_kinds": export_kinds,
                "image_roles": roles,
                "shot_keys": shot_keys,
                "flags": flags,
                "occurrence_count": len(group),
                "occurrences": sorted(group, key=lambda item: item["rel_path"]),
            }
        )
    return assets, occurrence_to_asset


def catalog_asset_is_ambiguous(asset: JsonDict) -> bool:
    ambiguous_multi_id = "multiple_product_ids" in asset["flags"] and not asset["filename_product_ids"]
    return (
        "missing_product_id" in asset["flags"]
        or "folder_filename_id_mismatch" in asset["flags"]
        or ambiguous_multi_id
    )


def inherited_reference_labels(occurrences: list[Occurrence]) -> dict[str, set[str]]:
    by_sha: dict[str, set[str]] = defaultdict(set)
    for occurrence in occurrences:
        if occurrence.reference_cluster_id:
            by_sha[occurrence.sha256].add(occurrence.reference_cluster_id)
    return by_sha


def occurrence_reference_labels(occurrence: Occurrence, sha_labels: dict[str, set[str]]) -> set[str]:
    labels = set(sha_labels.get(occurrence.sha256, set()))
    if occurrence.reference_cluster_id:
        labels.add(occurrence.reference_cluster_id)
    return labels


def same_visual_candidate(left: Occurrence, right: Occurrence, sha_labels: dict[str, set[str]]) -> bool:
    if left.sha256 == right.sha256:
        return True
    d_distance = hamming_hex(left.dhash, right.dhash)
    a_distance = hamming_hex(left.ahash, right.ahash)
    if d_distance is None or a_distance is None:
        return False

    left_labels = occurrence_reference_labels(left, sha_labels)
    right_labels = occurrence_reference_labels(right, sha_labels)
    has_conflicting_labels = bool(left_labels and right_labels and left_labels.isdisjoint(right_labels))
    if has_conflicting_labels:
        return False
    if left_labels and left_labels == right_labels and left.shot_key and left.shot_key == right.shot_key:
        return True

    # Same source + same shot key usually means web/print/png versions of the same
    # export. Keep the visual hash threshold conservative.
    if left.source == right.source and left.shot_key and left.shot_key == right.shot_key:
        return d_distance <= 12 and a_distance <= 16

    # Reference copies are already connected by exact hash. For non-exact
    # cross-source edits, require very strong visual agreement and no reference
    # conflict.
    if left.shot_key and left.shot_key == right.shot_key:
        return d_distance <= 5 and a_distance <= 8

    return False


def edit_duplicate_distance(left: Occurrence, right: Occurrence) -> int | None:
    d_distance = hamming_hex(left.dhash, right.dhash)
    a_distance = hamming_hex(left.ahash, right.ahash)
    if d_distance is None or a_distance is None:
        return None
    return d_distance + a_distance


def edit_duplicate_sort_key(left: Occurrence, right: Occurrence) -> SortKey:
    distance = edit_duplicate_distance(left, right)
    same_kind = left.kind == right.kind
    same_size = left.width == right.width and left.height == right.height
    return (
        distance if distance is not None else 999,
        not same_kind,
        not same_size,
        abs((left.size_bytes or 0) - (right.size_bytes or 0)),
        right.rel_path,
    )


def edited_duplicate_pairs(occurrences: list[Occurrence], max_distance: int) -> list[JsonDict]:
    fixed = [occurrence for occurrence in occurrences if occurrence.source == "fixed"]
    others = [occurrence for occurrence in occurrences if occurrence.source == "unfixed"]
    if not fixed or not others:
        return []

    best_for_fixed: dict[str, tuple[Occurrence, int]] = {}
    for left in fixed:
        candidates = []
        for right in others:
            if left.kind != right.kind:
                continue
            if left.width != right.width or left.height != right.height:
                continue
            distance = edit_duplicate_distance(left, right)
            if distance is None or distance > max_distance:
                continue
            candidates.append((right, distance))
        if candidates:
            best_for_fixed[left.occurrence_id] = sorted(candidates, key=lambda item: edit_duplicate_sort_key(left, item[0]))[0]

    best_for_other: dict[str, tuple[Occurrence, int]] = {}
    for right in others:
        candidates = []
        for left in fixed:
            if left.kind != right.kind:
                continue
            if left.width != right.width or left.height != right.height:
                continue
            distance = edit_duplicate_distance(left, right)
            if distance is None or distance > max_distance:
                continue
            candidates.append((left, distance))
        if candidates:
            best_for_other[right.occurrence_id] = sorted(candidates, key=lambda item: edit_duplicate_sort_key(right, item[0]))[0]

    pairs = []
    for left in fixed:
        match = best_for_fixed.get(left.occurrence_id)
        if not match:
            continue
        right, distance = match
        reverse = best_for_other.get(right.occurrence_id)
        if not reverse or reverse[0].occurrence_id != left.occurrence_id:
            continue
        pairs.append(
            {
                "source_occurrence_id": left.occurrence_id,
                "target_occurrence_id": right.occurrence_id,
                "source_path": left.path,
                "target_path": right.path,
                "source_rel_path": left.rel_path,
                "target_rel_path": right.rel_path,
                "source_source": left.source,
                "target_source": right.source,
                "kind": left.kind,
                "distance": distance,
                "source_shot_key": left.shot_key,
                "target_shot_key": right.shot_key,
            }
        )
    return sorted(pairs, key=lambda pair: (pair["distance"], pair["source_rel_path"], pair["target_rel_path"]))


def normalize_assets(
    occurrences: list[Occurrence],
    edit_dedup_distance: int | None = None,
) -> tuple[list[JsonDict], dict[str, str], list[JsonDict]]:
    sha_labels = inherited_reference_labels(occurrences)
    uf = UnionFind([occurrence.occurrence_id for occurrence in occurrences])
    {occurrence.occurrence_id: occurrence for occurrence in occurrences}

    by_sha: dict[str, list[Occurrence]] = defaultdict(list)
    by_shot_source: dict[tuple[str, str], list[Occurrence]] = defaultdict(list)
    by_shot: dict[str, list[Occurrence]] = defaultdict(list)

    for occurrence in occurrences:
        by_sha[occurrence.sha256].append(occurrence)
        if occurrence.shot_key:
            by_shot_source[(occurrence.source, occurrence.shot_key)].append(occurrence)
            by_shot[occurrence.shot_key].append(occurrence)

    for group in by_sha.values():
        for occurrence in group[1:]:
            uf.union(group[0].occurrence_id, occurrence.occurrence_id)

    for group in by_shot_source.values():
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                if same_visual_candidate(left, right, sha_labels):
                    uf.union(left.occurrence_id, right.occurrence_id)

    for group in by_shot.values():
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                if left.source == right.source:
                    continue
                if same_visual_candidate(left, right, sha_labels):
                    uf.union(left.occurrence_id, right.occurrence_id)

    edit_pairs: list[JsonDict] = []
    if edit_dedup_distance is not None:
        edit_pairs = edited_duplicate_pairs(occurrences, edit_dedup_distance)
        for pair in edit_pairs:
            uf.union(pair["source_occurrence_id"], pair["target_occurrence_id"])

    by_root: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_root[uf.find(occurrence.occurrence_id)].append(occurrence)

    assets: list[JsonDict] = []
    occurrence_to_asset: dict[str, str] = {}
    for index, group in enumerate(sorted(by_root.values(), key=asset_sort_key), start=1):
        asset_id = f"A{index:04d}"
        for occurrence in group:
            occurrence_to_asset[occurrence.occurrence_id] = asset_id
        labels = sorted(set().union(*(occurrence_reference_labels(occurrence, sha_labels) for occurrence in group)))
        preferred = choose_preferred(group, KIND_REVIEW_PRIORITY)
        quality = choose_preferred(group, KIND_QUALITY_PRIORITY)
        sources = sorted({occurrence.source for occurrence in group})
        kinds = sorted({occurrence.kind for occurrence in group})
        shot_keys = sorted({occurrence.shot_key for occurrence in group if occurrence.shot_key})
        flags = []
        if len(labels) > 1:
            flags.append("reference_conflict")
        if any(occurrence.is_before_fix for occurrence in group):
            flags.append("contains_before_fix")
        if "fixed" in sources and ("unfixed" in sources or "reference" in sources):
            flags.append("has_fixed_preferred_candidate")
        if len({occurrence.filename for occurrence in group}) == 1 and len({occurrence.sha256 for occurrence in group}) > 1:
            flags.append("same_filename_different_hash")
        if len(group) == 1:
            flags.append("single_occurrence")
        confidence = asset_confidence(group, labels)
        if confidence < 0.8 or "reference_conflict" in flags:
            flags.append("needs_review")
        assets.append(
            {
                "asset_id": asset_id,
                "preferred_occurrence_id": preferred.occurrence_id,
                "preferred_path": preferred.path,
                "quality_occurrence_id": quality.occurrence_id,
                "quality_path": quality.path,
                "reference_cluster_ids": labels,
                "sources": sources,
                "kinds": kinds,
                "shot_keys": shot_keys,
                "confidence": round(confidence, 3),
                "flags": flags,
                "occurrences": [asdict(occurrence) for occurrence in sorted(group, key=occurrence_sort_key)],
            }
        )
    return assets, occurrence_to_asset, edit_pairs


def occurrence_sort_key(occurrence: Occurrence) -> SortKey:
    return (
        SOURCE_PRIORITY.get(occurrence.source, 99),
        KIND_REVIEW_PRIORITY.get(occurrence.kind, 99),
        occurrence.is_before_fix,
        occurrence.rel_path,
    )


def asset_sort_key(group: list[Occurrence]) -> SortKey:
    preferred = choose_preferred(group, KIND_REVIEW_PRIORITY)
    return (preferred.shot_key or "zzzz", preferred.filename, preferred.path)


def choose_preferred(group: list[Occurrence], kind_priority: dict[str, int]) -> Occurrence:
    return sorted(
        group,
        key=lambda occurrence: (
            SOURCE_PRIORITY.get(occurrence.source, 99),
            kind_priority.get(occurrence.kind, 99),
            occurrence.is_before_fix,
            occurrence.size_bytes,
            occurrence.rel_path,
        ),
    )[0]


def asset_confidence(group: list[Occurrence], labels: list[str]) -> float:
    if len(labels) > 1:
        return 0.25
    if len(group) == 1:
        return 0.7
    shot_keys = {occurrence.shot_key for occurrence in group if occurrence.shot_key}
    if len(labels) == 1 and len(shot_keys) == 1:
        return 0.9
    max_dhash = 0
    max_ahash = 0
    for i, left in enumerate(group):
        for right in group[i + 1 :]:
            d_distance = hamming_hex(left.dhash, right.dhash) or 0
            a_distance = hamming_hex(left.ahash, right.ahash) or 0
            max_dhash = max(max_dhash, d_distance)
            max_ahash = max(max_ahash, a_distance)
    if max_dhash <= 5 and max_ahash <= 8:
        return 0.95
    if max_dhash <= 12 and max_ahash <= 16:
        return 0.85
    return 0.65


def write_inventory_csv(path: Path, occurrences: list[Occurrence]) -> None:
    fields = list(asdict(occurrences[0]).keys()) if occurrences else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for occurrence in occurrences:
            writer.writerow(asdict(occurrence))


def write_manifest_csv(path: Path, assets: list[JsonDict]) -> None:
    fields = [
        "asset_id",
        "preferred_path",
        "quality_path",
        "reference_cluster_ids",
        "sources",
        "kinds",
        "shot_keys",
        "confidence",
        "flags",
        "occurrence_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for asset in assets:
            writer.writerow(
                {
                    "asset_id": asset["asset_id"],
                    "preferred_path": asset["preferred_path"],
                    "quality_path": asset["quality_path"],
                    "reference_cluster_ids": "|".join(asset["reference_cluster_ids"]),
                    "sources": "|".join(asset["sources"]),
                    "kinds": "|".join(asset["kinds"]),
                    "shot_keys": "|".join(asset["shot_keys"]),
                    "confidence": asset["confidence"],
                    "flags": "|".join(asset["flags"]),
                    "occurrence_count": len(asset["occurrences"]),
                }
            )


def write_catalog_manifest_csv(path: Path, assets: list[JsonDict]) -> None:
    fields = [
        "asset_id",
        "category",
        "preferred_path",
        "quality_path",
        "product_ids",
        "reference_cluster_ids",
        "product_folders",
        "export_kinds",
        "image_roles",
        "shot_keys",
        "flags",
        "occurrence_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for asset in assets:
            writer.writerow(
                {
                    "asset_id": asset["asset_id"],
                    "category": asset["category"],
                    "preferred_path": asset["preferred_path"],
                    "quality_path": asset["quality_path"],
                    "product_ids": "|".join(asset["product_ids"]),
                    "reference_cluster_ids": "|".join(asset["reference_cluster_ids"]),
                    "product_folders": "|".join(asset["product_folders"]),
                    "export_kinds": "|".join(asset["export_kinds"]),
                    "image_roles": "|".join(asset["image_roles"]),
                    "shot_keys": "|".join(asset["shot_keys"]),
                    "flags": "|".join(asset["flags"]),
                    "occurrence_count": asset["occurrence_count"],
                }
            )


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def catalog_report_markdown(occurrences: list[JsonDict], assets: list[JsonDict]) -> str:
    category_counts = Counter(occurrence["category"] for occurrence in occurrences)
    asset_category_counts = Counter(asset["category"] for asset in assets)
    export_counts = Counter(occurrence["export_kind"] for occurrence in occurrences)
    role_counts = Counter(occurrence["image_role"] for occurrence in occurrences)
    flag_counts = Counter(flag for asset in assets for flag in asset["flags"])
    product_ids = sorted({pid for asset in assets for pid in asset["product_ids"]})
    reference_clusters = sorted({ref for asset in assets for ref in asset["reference_cluster_ids"]})
    lines = [
        "# Catalog Normalization Report",
        "",
        "## Summary",
        "",
        f"- Image occurrences: {len(occurrences)}",
        f"- Visual assets: {len(assets)}",
        f"- Product IDs represented: {len(product_ids)}",
        f"- Product folders represented: {len(reference_clusters)}",
        "",
        "## Occurrences By Category",
        "",
    ]
    for category, count in sorted(category_counts.items()):
        lines.append(f"- {category}: {count}")
    lines.extend(["", "## Visual Assets By Category", ""])
    for category, count in sorted(asset_category_counts.items()):
        lines.append(f"- {category}: {count}")
    lines.extend(["", "## Export Kinds", ""])
    for kind, count in export_counts.most_common():
        lines.append(f"- {kind}: {count}")
    lines.extend(["", "## Image Roles", ""])
    for role, count in role_counts.most_common():
        lines.append(f"- {role}: {count}")
    lines.extend(["", "## Asset Flags", ""])
    if flag_counts:
        for flag, count in flag_counts.most_common():
            lines.append(f"- {flag}: {count}")
    else:
        lines.append("- No flags")
    return "\n".join(lines) + "\n"


def catalog_removed_report_markdown(removed_assets: list[JsonDict]) -> str:
    flag_counts = Counter(flag for asset in removed_assets for flag in asset["flags"])
    folder_counts = Counter((asset["category"], "|".join(asset["product_folders"]) or "<none>") for asset in removed_assets)
    lines = [
        "# Removed Catalog Assets",
        "",
        f"- Removed visual assets: {len(removed_assets)}",
        "",
        "## Flags",
        "",
    ]
    for flag, count in flag_counts.most_common():
        lines.append(f"- {flag}: {count}")
    lines.extend(["", "## Folders", ""])
    for (category, folder), count in folder_counts.most_common(80):
        lines.append(f"- {category}/{folder}: {count}")
    return "\n".join(lines) + "\n"


def write_catalog_review_sheet(path: Path, title: str, assets: list[JsonDict], out_dir: Path, max_assets: int = 200) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for asset in assets[:max_assets]:
        figures = []
        for occurrence in asset["occurrences"][:12]:
            thumb = thumbs_dir / f"{occurrence['occurrence_id']}-{stable_name_digest(occurrence['path'])}.jpg"
            if not thumb.exists():
                make_thumbnail(Path(occurrence["path"]), thumb)
            caption = (
                f"{occurrence['export_kind']} / {occurrence['image_role']}<br>"
                f"{html.escape(occurrence['rel_path'])}<br>"
                f"ids: {html.escape(', '.join(occurrence['product_ids']) or 'none')}"
            )
            figures.append(
                "<figure>"
                f"<img src='{html.escape(str(Path('thumbs') / thumb.name))}' alt=''>"
                f"<figcaption>{caption}</figcaption>"
                "</figure>"
            )
        cards.append(
            "<section class='asset'>"
            f"<h2>{html.escape(asset['asset_id'])} <span>{asset['occurrence_count']} occurrences</span></h2>"
            f"<p><b>Category:</b> {html.escape(asset['category'])}</p>"
            f"<p><b>Product IDs:</b> {html.escape(', '.join(asset['product_ids']) or 'none')}</p>"
            f"<p><b>Folders:</b> {html.escape(', '.join(asset['product_folders']) or 'none')}</p>"
            f"<p><b>Flags:</b> {html.escape(', '.join(asset['flags']) or 'none')}</p>"
            "<div class='occurrences'>"
            + "\n".join(figures)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        ".asset{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".asset h2{font-size:18px;margin:0 0 8px}.asset h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".asset p{font-size:13px;margin:4px 0}.occurrences{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:180px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:180px;height:180px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{len(assets)} assets shown"
        + (f" (capped at {max_assets})" if len(assets) > max_assets else "")
        + ".</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def write_catalog_review_sheets(out_dir: Path, assets: list[JsonDict]) -> None:
    sheets = out_dir / "review_sheets"
    sheets.mkdir(parents=True, exist_ok=True)
    write_catalog_review_sheet(sheets / "01_all_assets.html", "Catalog Visual Assets", assets, out_dir)
    for flag in [
        "shared_across_product_folders",
        "multiple_product_ids",
        "folder_filename_id_mismatch",
        "missing_product_id",
        "single_occurrence",
    ]:
        write_catalog_review_sheet(
            sheets / f"02_{flag}.html",
            flag.replace("_", " ").title(),
            [asset for asset in assets if flag in asset["flags"]],
            out_dir,
        )


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def split_pipe_list(value: str) -> list[str]:
    return [item for item in value.split("|") if item]


def split_manifest_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace(",", "|").split("|") if item.strip()]


def truthy_manifest_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def load_manifest(path: Path) -> list[VisualAsset]:
    if not path.exists():
        msg = f"manifest does not exist: {path}"
        raise FileNotFoundError(msg)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        base_required = {
            "asset_id",
            "preferred_path",
            "quality_path",
            "occurrence_count",
        }
        required = set(base_required)
        if "reference_cluster_ids" not in fieldnames and "final_product_ids" not in fieldnames:
            required.add("reference_cluster_ids")
        missing = required.difference(reader.fieldnames or [])
        if missing:
            msg = f"manifest missing required columns: {', '.join(sorted(missing))}"
            raise ValueError(msg)
        assets = []
        for row in reader:
            preferred_path = row["preferred_path"]
            if not preferred_path:
                msg = f"asset {row['asset_id']} has no preferred_path"
                raise ValueError(msg)
            assets.append(
                VisualAsset(
                    asset_id=row["asset_id"],
                    preferred_path=preferred_path,
                    quality_path=row["quality_path"],
                    reference_cluster_ids=manifest_reference_labels(row),
                    sources=manifest_sources(row),
                    kinds=manifest_kinds(row),
                    shot_keys=split_manifest_list(row.get("shot_keys", "")),
                    confidence=float(row.get("confidence", "") or 1),
                    flags=split_manifest_list(row.get("flags", "")),
                    occurrence_count=int(row["occurrence_count"] or 0),
                )
            )
    if not assets:
        msg = "manifest has zero assets"
        raise ValueError(msg)
    return assets


def manifest_reference_labels(row: dict[str, str]) -> list[str]:
    if "final_product_ids" in row:
        if not truthy_manifest_value(row.get("identity_eligible", "")):
            return []
        return split_manifest_list(row["final_product_ids"])
    return split_manifest_list(row.get("reference_cluster_ids", ""))


def manifest_sources(row: dict[str, str]) -> list[str]:
    sources = split_manifest_list(row.get("sources", ""))
    if sources:
        return sources
    if "category" in row:
        return ["catalog"]
    return []


def manifest_kinds(row: dict[str, str]) -> list[str]:
    kinds = split_manifest_list(row.get("kinds", ""))
    if kinds:
        return kinds
    return split_manifest_list(row.get("export_kinds", ""))


def identity_assets(assets: list[VisualAsset]) -> list[VisualAsset]:
    return [asset for asset in assets if asset.reference_cluster_ids]


def normalize_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class EmbeddingProvider:
    provider_id = "base"

    def embed(self, image_path: Path) -> list[float]:
        raise NotImplementedError


class FakeEmbeddingProvider(EmbeddingProvider):
    provider_id = "fake-hash-v1"

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions

    def embed(self, image_path: Path) -> list[float]:
        digest = hashlib.sha256(sha256(image_path).encode("ascii")).digest()
        values: list[float] = []
        while len(values) < self.dimensions:
            for byte in digest:
                values.append((byte / 127.5) - 1.0)
                if len(values) == self.dimensions:
                    break
            digest = hashlib.sha256(digest).digest()
        return normalize_vector(values)


class DinoV2EmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        model_name: str = "dinov2_vits14",
        device: str = "auto",
        image_size: int = 224,
        local_files_only: bool = False,
    ) -> None:
        try:
            import torch
        except ImportError as exc:
            msg = "DINOv2 provider requires PyTorch. Install torch first."
            raise RuntimeError(msg) from exc

        self.torch = torch
        self.model_name = model_name
        self.image_size = image_size
        self.local_files_only = local_files_only
        self.device = self.resolve_device(device)
        self.provider_id = f"dinov2-{model_name}-{self.device}-s{image_size}"
        if "TORCH_HOME" not in os.environ:
            torch_home = Path.cwd() / ".model_cache" / "torch"
            torch_home.mkdir(parents=True, exist_ok=True)
            os.environ["TORCH_HOME"] = str(torch_home)
        self.backend = "torch_hub"
        self.model = self.load_model()
        self.model.eval()
        self.model.to(self.device)

    def load_model(self) -> Any:
        try:
            from transformers import AutoModel
        except ImportError as exc:
            msg = "DINOv2 provider requires transformers. Install transformers first."
            raise RuntimeError(msg) from exc

        self.backend = "transformers"
        repo_id = {
            "dinov2_vits14": "facebook/dinov2-small",
            "dinov2_vitb14": "facebook/dinov2-base",
            "dinov2_vitl14": "facebook/dinov2-large",
            "dinov2_vitg14": "facebook/dinov2-giant",
        }.get(self.model_name, self.model_name)
        return AutoModel.from_pretrained(  # nosec B615
            repo_id,
            revision="main",
            local_files_only=self.local_files_only,
        )

    def resolve_device(self, device: str) -> str:
        if device != "auto":
            return device
        torch = self.torch
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def image_tensor(self, image_path: Path) -> Any:
        return image_tensor_from_pillow(
            image_path,
            self.image_size,
            self.torch,
            self.device,
            [0.485, 0.456, 0.406],
            [0.229, 0.224, 0.225],
        )

    def embed(self, image_path: Path) -> list[float]:
        torch = self.torch
        with tempfile.TemporaryDirectory(prefix="jewelry-dinov2-") as tmp:
            del tmp
            tensor = self.image_tensor(image_path)
            with torch.no_grad():
                if self.backend == "transformers":
                    output = self.model(pixel_values=tensor)
                    vector = output.pooler_output if getattr(output, "pooler_output", None) is not None else output.last_hidden_state[:, 0]
                elif hasattr(self.model, "forward_features"):
                    output = self.model.forward_features(tensor)
                    vector = output["x_norm_clstoken"] if isinstance(output, dict) else output
                else:
                    vector = self.model(tensor)
                vector = vector.squeeze(0).detach().cpu().float().tolist()
        return normalize_vector([float(value) for value in vector])


class TransformerImageEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        provider_name: str,
        model_id: str,
        device: str = "auto",
        image_size: int = 224,
        local_files_only: bool = False,
    ) -> None:
        try:
            import torch
            from transformers import AutoModel
        except ImportError as exc:
            msg = f"{provider_name} provider requires torch and transformers."
            raise RuntimeError(msg) from exc

        self.torch = torch
        self.provider_name = provider_name
        self.model_id = model_id
        self.image_size = image_size
        self.local_files_only = local_files_only
        self.device = self.resolve_device(device)
        safe_model_id = model_id.replace("/", "_")
        self.provider_id = f"{provider_name}-{safe_model_id}-{self.device}-s{image_size}"
        self.model = AutoModel.from_pretrained(  # nosec B615
            model_id,
            revision="main",
            local_files_only=local_files_only,
        )
        self.model.eval()
        self.model.to(self.device)

    def resolve_device(self, device: str) -> str:
        if device != "auto":
            return device
        torch = self.torch
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def image_tensor(self, image_path: Path) -> Any:
        if self.provider_name == "clip":
            mean = [0.48145466, 0.4578275, 0.40821073]
            std = [0.26862954, 0.26130258, 0.27577711]
        else:
            mean = [0.5, 0.5, 0.5]
            std = [0.5, 0.5, 0.5]
        return image_tensor_from_pillow(image_path, self.image_size, self.torch, self.device, mean, std)

    def embed(self, image_path: Path) -> list[float]:
        torch = self.torch
        with tempfile.TemporaryDirectory(prefix=f"jewelry-{self.provider_name}-") as tmp:
            del tmp
            tensor = self.image_tensor(image_path)
            with torch.no_grad():
                if hasattr(self.model, "get_image_features"):
                    vector = self.model.get_image_features(pixel_values=tensor)
                    if not hasattr(vector, "squeeze"):
                        vector = vector.pooler_output if getattr(vector, "pooler_output", None) is not None else vector.last_hidden_state[:, 0]
                else:
                    output = self.model(pixel_values=tensor)
                    vector = output.pooler_output if getattr(output, "pooler_output", None) is not None else output.last_hidden_state[:, 0]
                vector = vector.squeeze(0).detach().cpu().float().tolist()
        return normalize_vector([float(value) for value in vector])


def image_tensor_from_pillow(
    image_path: Path,
    image_size: int,
    torch: Any,
    device: str,
    mean_values: list[float],
    std_values: list[float],
) -> Any:
    try:
        from PIL import ImageOps
    except ImportError as exc:
        msg = "Pillow is required for image preprocessing. Install pillow first."
        raise RuntimeError(msg) from exc

    with open_pillow_image(image_path) as source_image:
        image = ImageOps.exif_transpose(source_image).convert("RGB")
        image.thumbnail((image_size, image_size), pillow_resample_lanczos())
        width, height = image.size
        pixels = list(image.getdata())
    image_tensor = torch.tensor(pixels, dtype=torch.float32).view(height, width, 3).permute(2, 0, 1) / 255.0
    canvas = torch.ones((3, image_size, image_size), dtype=torch.float32)
    top = max((image_size - height) // 2, 0)
    left = max((image_size - width) // 2, 0)
    canvas[:, top : top + height, left : left + width] = image_tensor[:, :image_size, :image_size]
    mean = torch.tensor(mean_values, dtype=torch.float32).view(3, 1, 1)
    std = torch.tensor(std_values, dtype=torch.float32).view(3, 1, 1)
    return ((canvas - mean) / std).unsqueeze(0).to(device)


def build_embedding_provider(args: argparse.Namespace) -> EmbeddingProvider:
    if args.provider == "fake":
        return FakeEmbeddingProvider()
    if args.provider == "dinov2":
        return DinoV2EmbeddingProvider(
            model_name=args.dinov2_model,
            device=args.device,
            image_size=args.image_size,
            local_files_only=args.offline_model_cache,
        )
    if args.provider == "clip":
        return TransformerImageEmbeddingProvider(
            provider_name="clip",
            model_id=args.model_id or "openai/clip-vit-base-patch32",
            device=args.device,
            image_size=args.image_size,
            local_files_only=args.offline_model_cache,
        )
    if args.provider == "siglip":
        return TransformerImageEmbeddingProvider(
            provider_name="siglip",
            model_id=args.model_id or "google/siglip-base-patch16-224",
            device=args.device,
            image_size=args.image_size,
            local_files_only=args.offline_model_cache,
        )
    msg = f"unknown provider: {args.provider}"
    raise ValueError(msg)


def load_embedding_cache(path: Path) -> dict[str, JsonDict]:
    if not path.exists():
        return {}
    return cast("dict[str, JsonDict]", json.loads(path.read_text(encoding="utf-8")))


def embedding_cache_key(provider: EmbeddingProvider, image_path: Path, view: str) -> tuple[str, str]:
    image_hash = sha256(image_path)
    return f"{provider.provider_id}|{view}|{image_hash}", image_hash


def embed_assets(
    assets: list[VisualAsset],
    provider: EmbeddingProvider,
    out_dir: Path,
) -> tuple[dict[str, list[float]], list[JsonDict]]:
    embeddings_dir = out_dir / "embeddings"
    embeddings_dir.mkdir(parents=True, exist_ok=True)
    cache_path = embeddings_dir / "embedding_cache.json"
    cache = load_embedding_cache(cache_path)
    vectors: dict[str, list[float]] = {}
    records: list[JsonDict] = []

    for index, asset in enumerate(assets, start=1):
        image_path = Path(asset.preferred_path)
        if not image_path.exists():
            records.append(
                {
                    "asset_id": asset.asset_id,
                    "status": "missing_image",
                    "image_path": str(image_path),
                }
            )
            continue
        key, image_hash = embedding_cache_key(provider, image_path, "full")
        if key in cache:
            vector = cache[key]["vector"]
            status = "cache_hit"
        else:
            print(f"Embedding {index}/{len(assets)} {asset.asset_id}: {image_path.name}")
            vector = provider.embed(image_path)
            cache[key] = {
                "provider": provider.provider_id,
                "view": "full",
                "image_sha256": image_hash,
                "image_path": str(image_path),
                "vector": vector,
            }
            status = "embedded"
        vectors[asset.asset_id] = vector
        records.append(
            {
                "asset_id": asset.asset_id,
                "status": status,
                "provider": provider.provider_id,
                "view": "full",
                "image_sha256": image_hash,
                "image_path": str(image_path),
                "dimensions": len(vector),
            }
        )

    write_json(cache_path, cache)
    write_json(embeddings_dir / "embeddings.json", records)
    return vectors, records


def all_pair_scores(assets: list[VisualAsset], vectors: dict[str, list[float]]) -> list[JsonDict]:
    scored_assets = [asset for asset in assets if asset.asset_id in vectors]
    rows = []
    for i, left in enumerate(scored_assets):
        for right in scored_assets[i + 1 :]:
            rows.append(
                {
                    "source_asset_id": left.asset_id,
                    "target_asset_id": right.asset_id,
                    "score": cosine_similarity(vectors[left.asset_id], vectors[right.asset_id]),
                    "source_view": "full",
                    "target_view": "full",
                }
            )
    return rows


def write_pair_scores_csv(path: Path, rows: list[JsonDict]) -> None:
    fields = ["source_asset_id", "target_asset_id", "score", "source_view", "target_view"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "score": f"{row['score']:.6f}"})


def cluster_from_scores(assets: list[VisualAsset], scores: list[JsonDict], threshold: float) -> list[JsonDict]:
    asset_ids = [asset.asset_id for asset in assets]
    uf = UnionFind(asset_ids)
    for row in scores:
        if row["score"] >= threshold:
            uf.union(row["source_asset_id"], row["target_asset_id"])
    by_root: dict[str, list[str]] = defaultdict(list)
    for asset_id in asset_ids:
        by_root[uf.find(asset_id)].append(asset_id)
    clusters = []
    for index, members in enumerate(sorted(by_root.values(), key=lambda group: (len(group) == 1, group)), start=1):
        clusters.append(
            {
                "cluster_id": f"C{index:04d}",
                "asset_ids": sorted(members),
                "size": len(members),
                "threshold": threshold,
            }
        )
    return clusters


def asset_label_map(assets: list[VisualAsset]) -> dict[str, set[str]]:
    return {asset.asset_id: set(asset.reference_cluster_ids) for asset in assets}


def cluster_lookup(clusters: list[JsonDict]) -> dict[str, str]:
    lookup = {}
    for cluster in clusters:
        for asset_id in cluster["asset_ids"]:
            lookup[asset_id] = cluster["cluster_id"]
    return lookup


def benchmark_clusters(assets: list[VisualAsset], clusters: list[JsonDict]) -> JsonDict:
    labels = asset_label_map(assets)
    predicted = cluster_lookup(clusters)
    scored_pairs = 0
    true_positive = 0
    false_positive = 0
    false_negative = 0
    asset_ids = [asset.asset_id for asset in assets]
    for i, left_id in enumerate(asset_ids):
        for right_id in asset_ids[i + 1 :]:
            left_labels = labels[left_id]
            right_labels = labels[right_id]
            if not left_labels or not right_labels:
                continue
            scored_pairs += 1
            same_reference = bool(left_labels.intersection(right_labels))
            same_predicted = predicted[left_id] == predicted[right_id]
            if same_reference and same_predicted:
                true_positive += 1
            elif not same_reference and same_predicted:
                false_positive += 1
            elif same_reference and not same_predicted:
                false_negative += 1
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    merge_errors = merge_error_rows(clusters, labels)
    split_errors = split_error_rows(clusters, labels)
    return {
        "asset_count": len(assets),
        "scored_asset_count": sum(1 for asset in assets if asset.reference_cluster_ids),
        "cluster_count": len(clusters),
        "singleton_count": sum(1 for cluster in clusters if len(cluster["asset_ids"]) == 1),
        "scored_pairs": scored_pairs,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "predicted_positive": true_positive + false_positive,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "merge_error_count": len(merge_errors),
        "split_error_count": len(split_errors),
        "merge_errors": merge_errors,
        "split_errors": split_errors,
    }


def merge_error_rows(clusters: list[JsonDict], labels: dict[str, set[str]]) -> list[JsonDict]:
    rows = []
    for cluster in clusters:
        cluster_labels = sorted(set().union(*(labels[asset_id] for asset_id in cluster["asset_ids"])))
        if len(cluster_labels) > 1:
            rows.append(
                {
                    "cluster_id": cluster["cluster_id"],
                    "asset_ids": cluster["asset_ids"],
                    "reference_cluster_ids": cluster_labels,
                }
            )
    return rows


def split_error_rows(clusters: list[JsonDict], labels: dict[str, set[str]]) -> list[JsonDict]:
    by_reference: dict[str, set[str]] = defaultdict(set)
    cluster_by_asset = cluster_lookup(clusters)
    for asset_id, reference_labels in labels.items():
        for label in reference_labels:
            by_reference[label].add(cluster_by_asset[asset_id])
    rows = []
    for label, cluster_ids in sorted(by_reference.items()):
        if len(cluster_ids) > 1:
            rows.append({"reference_cluster_id": label, "predicted_cluster_ids": sorted(cluster_ids)})
    return rows


def threshold_sweep(assets: list[VisualAsset], scores: list[JsonDict], thresholds: list[float]) -> list[JsonDict]:
    rows = []
    for threshold in thresholds:
        clusters = cluster_from_scores(assets, scores, threshold)
        benchmark = benchmark_clusters(assets, clusters)
        rows.append(
            {
                "threshold": threshold,
                "cluster_count": benchmark["cluster_count"],
                "singleton_count": benchmark["singleton_count"],
                "precision": benchmark["precision"],
                "recall": benchmark["recall"],
                "f1": benchmark["f1"],
                "merge_error_count": benchmark["merge_error_count"],
                "split_error_count": benchmark["split_error_count"],
                "false_positive": benchmark["false_positive"],
                "false_negative": benchmark["false_negative"],
                "predicted_positive": benchmark["predicted_positive"],
            }
        )
    return rows


def choose_threshold(rows: list[JsonDict], min_precision: float) -> float:
    eligible = [row for row in rows if row["precision"] >= min_precision]
    if eligible:
        best = sorted(eligible, key=lambda row: (row["recall"], row["f1"], row["threshold"]), reverse=True)[0]
    else:
        useful = [row for row in rows if row.get("predicted_positive", 0) > 0]
        candidates = useful or rows
        best = sorted(candidates, key=lambda row: (row["precision"], row["recall"], row["f1"], row["threshold"]), reverse=True)[0]
    return float(best["threshold"])


def write_threshold_sweep_csv(path: Path, rows: list[JsonDict]) -> None:
    fields = [
        "threshold",
        "cluster_count",
        "singleton_count",
        "precision",
        "recall",
        "f1",
        "merge_error_count",
        "split_error_count",
        "false_positive",
        "false_negative",
        "predicted_positive",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "threshold": f"{row['threshold']:.4f}",
                    "precision": f"{row['precision']:.6f}",
                    "recall": f"{row['recall']:.6f}",
                    "f1": f"{row['f1']:.6f}",
                }
            )


def benchmark_report_markdown(provider_id: str, threshold: float, sweep: list[JsonDict], benchmark: JsonDict) -> str:
    lines = [
        "# Product Clustering Benchmark",
        "",
        "## Summary",
        "",
        f"- Provider: `{provider_id}`",
        f"- Recommended threshold: `{threshold:.4f}`",
        f"- Assets: {benchmark['asset_count']}",
        f"- Predicted clusters: {benchmark['cluster_count']}",
        f"- Singletons: {benchmark['singleton_count']}",
        f"- Pairwise precision: {benchmark['precision']:.3f}",
        f"- Pairwise recall: {benchmark['recall']:.3f}",
        f"- Pairwise F1: {benchmark['f1']:.3f}",
        f"- Predicted positive pairs: {benchmark['predicted_positive']}",
        f"- Merge disagreements: {benchmark['merge_error_count']}",
        f"- Split disagreements: {benchmark['split_error_count']}",
        "",
        "The reference clusters are treated as benchmark labels, not guaranteed truth.",
        "",
        "## Threshold Sweep",
        "",
        "| Threshold | Precision | Recall | F1 | Clusters | Singletons | Merges | Splits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sweep:
        lines.append(
            f"| {row['threshold']:.4f} | {row['precision']:.3f} | {row['recall']:.3f} | "
            f"{row['f1']:.3f} | {row['cluster_count']} | {row['singleton_count']} | "
            f"{row['merge_error_count']} | {row['split_error_count']} |"
        )
    if benchmark["merge_errors"]:
        lines.extend(["", "## Merge Disagreements", ""])
        for row in benchmark["merge_errors"][:50]:
            lines.append(
                f"- {row['cluster_id']}: {', '.join(row['asset_ids'])} "
                f"contains labels {', '.join(row['reference_cluster_ids'])}"
            )
    if benchmark["split_errors"]:
        lines.extend(["", "## Split Disagreements", ""])
        for row in benchmark["split_errors"][:50]:
            lines.append(
                f"- {row['reference_cluster_id']}: split across {', '.join(row['predicted_cluster_ids'])}"
            )
    lines.append("")
    return "\n".join(lines)


def asset_by_id(assets: list[VisualAsset]) -> dict[str, VisualAsset]:
    return {asset.asset_id: asset for asset in assets}


def write_cluster_review_sheet(path: Path, title: str, clusters: list[JsonDict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    cards = []
    for cluster in clusters:
        figures = []
        for asset_id in cluster["asset_ids"]:
            asset = lookup[asset_id]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset_id}-{stable_name_digest(asset.preferred_path)}.jpg"
            if not thumb.exists():
                make_thumbnail(source, thumb)
            caption = (
                f"{asset.asset_id}<br>"
                f"ref: {html.escape(', '.join(asset.reference_cluster_ids) or 'none')}<br>"
                f"{html.escape(source.name)}"
            )
            figures.append(
                "<figure>"
                f"<img src='{html.escape(str(Path('thumbs') / thumb.name))}' alt=''>"
                f"<figcaption>{caption}</figcaption>"
                "</figure>"
            )
        label_counts = Counter(label for asset_id in cluster["asset_ids"] for label in lookup[asset_id].reference_cluster_ids)
        cards.append(
            "<section class='cluster'>"
            f"<h2>{html.escape(cluster['cluster_id'])} <span>{cluster['size']} assets</span></h2>"
            f"<p><b>Reference labels:</b> {html.escape(', '.join(sorted(label_counts)) or 'none')}</p>"
            "<div class='assets'>"
            + "\n".join(figures)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "h1{font-size:24px}.cluster{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".cluster h2{font-size:18px;margin:0 0 8px}.cluster h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".cluster p{font-size:13px;margin:4px 0}.assets{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:180px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:180px;height:180px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{len(clusters)} clusters shown.</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def write_clustering_review_sheets(out_dir: Path, clusters: list[JsonDict], assets: list[VisualAsset], benchmark: JsonDict) -> None:
    sheets_dir = out_dir / "review_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    write_cluster_review_sheet(sheets_dir / "01_predicted_clusters.html", "Predicted Product Clusters", clusters, assets, out_dir)
    lookup = asset_by_id(assets)
    merge_cluster_ids = {row["cluster_id"] for row in benchmark["merge_errors"]}
    merge_clusters = [cluster for cluster in clusters if cluster["cluster_id"] in merge_cluster_ids]
    write_cluster_review_sheet(sheets_dir / "02_merge_disagreements.html", "Merge Disagreements", merge_clusters, assets, out_dir)
    split_cluster_ids = set()
    for row in benchmark["split_errors"]:
        split_cluster_ids.update(row["predicted_cluster_ids"])
    split_clusters = [cluster for cluster in clusters if cluster["cluster_id"] in split_cluster_ids]
    write_cluster_review_sheet(sheets_dir / "03_split_disagreements.html", "Split Disagreements", split_clusters, assets, out_dir)
    singleton_clusters = [cluster for cluster in clusters if cluster["size"] == 1 and lookup[cluster["asset_ids"][0]].reference_cluster_ids]
    write_cluster_review_sheet(sheets_dir / "04_singletons.html", "Singleton Predicted Clusters", singleton_clusters, assets, out_dir)


def pair_diagnostics(assets: list[VisualAsset], scores: list[JsonDict], threshold: float, limit: int = 80) -> dict[str, list[JsonDict]]:
    labels = asset_label_map(assets)
    missed_same = []
    dangerous_different = []
    for row in scores:
        left = row["source_asset_id"]
        right = row["target_asset_id"]
        left_labels = labels[left]
        right_labels = labels[right]
        if not left_labels or not right_labels:
            continue
        same_reference = bool(left_labels.intersection(right_labels))
        diagnostic = {
            **row,
            "source_reference_cluster_ids": sorted(left_labels),
            "target_reference_cluster_ids": sorted(right_labels),
            "shared_reference_cluster_ids": sorted(left_labels.intersection(right_labels)),
        }
        if same_reference and row["score"] < threshold:
            missed_same.append(diagnostic)
        elif not same_reference:
            dangerous_different.append(diagnostic)
    return {
        "missed_same_product_pairs": sorted(missed_same, key=lambda item: item["score"], reverse=True)[:limit],
        "hard_same_product_pairs": sorted(missed_same, key=lambda item: item["score"])[:limit],
        "dangerous_different_product_pairs": sorted(dangerous_different, key=lambda item: item["score"], reverse=True)[:limit],
    }


PRODUCT_SAME_DECISIONS = {"same_product", "same_physical_product", "same_sellable_product"}
DESIGN_SAME_DECISIONS = {"same_design_variant"}
DIFFERENT_DECISIONS = {"different_product", "different_design"}
VALID_AI_DECISIONS = PRODUCT_SAME_DECISIONS | DESIGN_SAME_DECISIONS | DIFFERENT_DECISIONS | {"unsure"}
AI_PAIR_PROMPT_VERSION = "product_design_v3_edit_dedup_2026_05_27"
VALID_JEWELRY_BOX_TYPES = {"ring", "earring", "necklace", "pendant", "bracelet", "unknown_jewelry"}
JEWELRY_BOX_PROMPT_VERSION = "jewelry_box_v1_2026_05_29"
OWLV2_PROMPTS = ["ring", "jewelry ring", "gold ring", "gemstone ring", "diamond ring", "ring on finger", "small ring"]
OWLV2_PREFERRED_LABELS = {"gold ring", "jewelry ring", "ring", "gemstone ring", "diamond ring", "small ring"}
OWLV2_DEPRIORITIZED_LABELS = {"ring on finger"}
OWLV2_PROMPT_VERSION = "owlv2_ring_prompts_v1_2026_05_29"
OWLV2_MIN_SCORE = 0.12
OWLV2_MIN_AREA_RATIO = 0.0002
OWLV2_MAX_AREA_RATIO = 0.20
OWLV2_MODEL_CACHE: dict[tuple[str, str], tuple[Any, Any, Any, Any]] = {}
FOREGROUND_BACKGROUND_DISTANCE = 55
FOREGROUND_DETAIL_RATIO = 8.0
FOREGROUND_MIN_BOX_AREA_RATIO = 0.20
FOREGROUND_MIN_BACKGROUND_RGB = 220
FOREGROUND_MAX_PIXEL_AREA_RATIO = 0.45
IMAGE_PROFILE_PROMPT_VERSION = "image_profile_v1_2026_05_29"
VALID_PROFILE_SHOT_TYPES = {"model_lifestyle", "clean_product", "macro_detail", "packaging", "uncertain"}
VALID_PROFILE_VIEW_POLICIES = {"full_only", "crop_only", "full_and_crop", "review"}
EVIDENCE_PROFILE_PROMPT_VERSION = "multi_view_profile_v1_2026_05_29"
RETRIEVAL_ADJUDICATION_PROMPT_VERSION = "retrieval_adjudication_v2_strict_identity_2026_05_29"
VALID_EVIDENCE_SCENE_TYPES = {
    "clean_product",
    "model_lifestyle",
    "macro_detail",
    "multi_item",
    "packaging",
    "uncertain",
}
VALID_EVIDENCE_POLICIES = {"full_only", "full_plus_crop", "crop_heavy", "review"}
VALID_JEWELRY_DOMINANCE = {"tiny", "small", "medium", "dominant"}
VALID_OBJECT_COMPLETENESS = {"complete", "partial", "detail_only", "uncertain"}
VALID_RETRIEVAL_ADJUDICATION_DECISIONS = {"same_product", "same_design_variant", "different", "unsure"}
EVIDENCE_VIEW_TYPES = {"full_image", "vlm_context", "owlv2_padded", "owlv2_context"}
PRODUCT_PROFILE_SCHEMA_VERSION = "1.0"
PRODUCT_EMBED_SCHEMA_VERSION = "1.0"
PRODUCT_EMBED_PREPROCESS_VERSION = "jewelry-evidence-v1"


def product_same_decision(label: str) -> bool:
    return label in PRODUCT_SAME_DECISIONS


def non_product_same_decision(label: str) -> bool:
    return label in DESIGN_SAME_DECISIONS or label in DIFFERENT_DECISIONS or label == "unsure"


def decision_lookup_by_pair(decisions: dict[str, JsonDict]) -> dict[str, JsonDict]:
    lookup = {}
    for key, decision in decisions.items():
        pair_key = decision.get("pair_key") or key
        if "--" not in pair_key:
            continue
        lookup[pair_key] = decision
    return lookup


def candidate_pairs(assets: list[VisualAsset], scores: list[JsonDict], threshold: float, top_k: int = 0) -> list[JsonDict]:
    labels = asset_label_map(assets)
    by_key: dict[str, JsonDict] = {}
    neighbors: dict[str, list[JsonDict]] = defaultdict(list)
    for row in scores:
        left = row["source_asset_id"]
        right = row["target_asset_id"]
        neighbors[left].append(row)
        neighbors[right].append(row)
        reasons = []
        if row["score"] >= threshold:
            reasons.append(f"threshold>={threshold:.4f}")
        if not reasons:
            continue
        add_candidate_pair(by_key, row, labels, threshold, reasons)

    if top_k > 0:
        for rows in neighbors.values():
            for row in sorted(rows, key=lambda item: item["score"], reverse=True)[:top_k]:
                add_candidate_pair(by_key, row, labels, threshold, [f"top_{top_k}_neighbor"])

    return sorted(by_key.values(), key=lambda item: item["score"], reverse=True)


def add_candidate_pair(
    by_key: dict[str, JsonDict],
    row: JsonDict,
    labels: dict[str, set[str]],
    threshold: float,
    reasons: list[str],
) -> None:
    left = row["source_asset_id"]
    right = row["target_asset_id"]
    key = candidate_pair_key(row)
    left_labels = labels[left]
    right_labels = labels[right]
    if key not in by_key:
        by_key[key] = {
            **row,
            "candidate_threshold": threshold,
            "candidate_reasons": [],
            "source_reference_cluster_ids": sorted(left_labels),
            "target_reference_cluster_ids": sorted(right_labels),
            "shared_reference_cluster_ids": sorted(left_labels.intersection(right_labels)),
            "benchmark_same_reference": bool(left_labels and right_labels and left_labels.intersection(right_labels)),
        }
    current = by_key[key]["candidate_reasons"]
    for reason in reasons:
        if reason not in current:
            current.append(reason)


def candidate_coverage_report(pairs: list[JsonDict], assets: list[VisualAsset]) -> JsonDict:
    labels = asset_label_map(assets)
    asset_ids = [asset.asset_id for asset in assets]
    total_same = 0
    for i, left in enumerate(asset_ids):
        for right in asset_ids[i + 1 :]:
            if labels[left] and labels[right] and labels[left].intersection(labels[right]):
                total_same += 1
    candidate_same = sum(1 for pair in pairs if pair.get("benchmark_same_reference"))
    candidate_different = len(pairs) - candidate_same
    return {
        "candidate_pair_count": len(pairs),
        "candidate_same_reference_pairs": candidate_same,
        "candidate_different_reference_pairs": candidate_different,
        "total_same_reference_pairs": total_same,
        "candidate_recall_ceiling": candidate_same / total_same if total_same else 0.0,
    }


def write_pair_review_sheet(path: Path, title: str, pairs: list[JsonDict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    rows = []
    for pair in pairs:
        figures = []
        for key in ("source_asset_id", "target_asset_id"):
            asset = lookup[pair[key]]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset.asset_id}-{stable_name_digest(asset.preferred_path)}.jpg"
            if not thumb.exists():
                make_thumbnail(source, thumb)
            figures.append(
                "<figure>"
                f"<img src='{html.escape(str(Path('thumbs') / thumb.name))}' alt=''>"
                f"<figcaption>{asset.asset_id}<br>ref: {html.escape(', '.join(asset.reference_cluster_ids) or 'none')}</figcaption>"
                "</figure>"
            )
        rows.append(
            "<section class='pair'>"
            f"<h2>{pair['source_asset_id']} - {pair['target_asset_id']} <span>score {pair['score']:.4f}</span></h2>"
            f"<p><b>Candidate reasons:</b> {html.escape(', '.join(pair.get('candidate_reasons') or []))}</p>"
            f"<p><b>Shared reference:</b> {html.escape(', '.join(pair['shared_reference_cluster_ids']) or 'none')}</p>"
            f"<p><b>Left:</b> {html.escape(', '.join(pair['source_reference_cluster_ids']) or 'none')}</p>"
            f"<p><b>Right:</b> {html.escape(', '.join(pair['target_reference_cluster_ids']) or 'none')}</p>"
            "<div class='assets'>"
            + "\n".join(figures)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "h1{font-size:24px}.pair{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".pair h2{font-size:18px;margin:0 0 8px}.pair h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".pair p{font-size:13px;margin:4px 0}.assets{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:220px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:220px;height:220px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{len(pairs)} pairs shown.</p>"
        + "\n".join(rows)
        + "</body></html>",
        encoding="utf-8",
    )


def write_pair_diagnostics(out_dir: Path, diagnostics: dict[str, list[JsonDict]], assets: list[VisualAsset]) -> None:
    sheets_dir = out_dir / "review_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "pair_diagnostics.json", diagnostics)
    write_pair_review_sheet(
        sheets_dir / "05_missed_same_product_pairs.html",
        "Missed Same-Product Pairs Near Threshold",
        diagnostics["missed_same_product_pairs"],
        assets,
        out_dir,
    )
    write_pair_review_sheet(
        sheets_dir / "06_dangerous_different_product_pairs.html",
        "Dangerous Different-Product Pairs",
        diagnostics["dangerous_different_product_pairs"],
        assets,
        out_dir,
    )
    write_pair_review_sheet(
        sheets_dir / "07_hard_same_product_pairs.html",
        "Hard Same-Product Pairs",
        diagnostics["hard_same_product_pairs"],
        assets,
        out_dir,
    )


def write_candidate_pairs(out_dir: Path, pairs: list[JsonDict], assets: list[VisualAsset]) -> None:
    write_json(out_dir / "candidate_pairs.json", pairs)
    write_json(out_dir / "candidate_coverage.json", candidate_coverage_report(pairs, assets))
    sheets_dir = out_dir / "review_sheets"
    write_pair_review_sheet(
        sheets_dir / "08_candidate_pairs.html",
        "Candidate Pairs For Adjudication",
        pairs,
        assets,
        out_dir,
    )


def candidate_pair_key(pair: JsonDict) -> str:
    return "--".join(sorted([pair["source_asset_id"], pair["target_asset_id"]]))


def load_candidate_pairs(path: Path) -> list[JsonDict]:
    if not path.exists():
        msg = f"candidate pair file does not exist: {path}"
        raise FileNotFoundError(msg)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        msg = "candidate pair file must contain a JSON list"
        raise TypeError(msg)
    return payload


def load_ai_decision_cache(path: Path) -> dict[str, JsonDict]:
    if not path.exists():
        return {}
    return cast("dict[str, JsonDict]", json.loads(path.read_text(encoding="utf-8")))


def benchmark_ai_decisions(pairs: list[JsonDict], decisions: dict[str, JsonDict]) -> JsonDict:
    true_positive = false_positive = false_negative = true_negative = unsure = design_variant = 0
    rows = []
    decisions_by_pair = decision_lookup_by_pair(decisions)
    for pair in pairs:
        key = candidate_pair_key(pair)
        decision = decisions_by_pair.get(key, {})
        label = decision.get("decision", "missing")
        same_reference = bool(pair.get("benchmark_same_reference"))
        if product_same_decision(label) and same_reference:
            true_positive += 1
        elif product_same_decision(label) and not same_reference:
            false_positive += 1
        elif label in DESIGN_SAME_DECISIONS:
            design_variant += 1
            if same_reference:
                false_negative += 1
            else:
                true_negative += 1
        elif label in DIFFERENT_DECISIONS and same_reference:
            false_negative += 1
        elif label in DIFFERENT_DECISIONS and not same_reference:
            true_negative += 1
        else:
            unsure += 1
            if same_reference:
                false_negative += 1
        rows.append(
            {
                **pair,
                "ai_decision": label,
                "ai_confidence": decision.get("confidence"),
                "ai_reason": decision.get("reason", ""),
                "ai_product_evidence": decision.get("product_evidence", []),
                "ai_design_evidence": decision.get("design_evidence", []),
                "ai_difference_evidence": decision.get("difference_evidence", []),
                "ai_review_required": decision.get("review_required"),
                "ai_review_flags": decision.get("review_flags", []),
            }
        )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "candidate_pair_count": len(pairs),
        "decided_pair_count": sum(1 for pair in pairs if candidate_pair_key(pair) in decisions_by_pair),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "design_variant": design_variant,
        "unsure_or_missing": unsure,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "rows": rows,
    }


def ai_benchmark_markdown(model: str, benchmark: JsonDict) -> str:
    lines = [
        "# AI Pair Adjudication Benchmark",
        "",
        f"- Model: `{model}`",
        f"- Candidate pairs: {benchmark['candidate_pair_count']}",
        f"- Decided pairs: {benchmark['decided_pair_count']}",
        f"- Precision: {benchmark['precision']:.3f}",
        f"- Recall: {benchmark['recall']:.3f}",
        f"- F1: {benchmark['f1']:.3f}",
        f"- True positives: {benchmark['true_positive']}",
        f"- False positives: {benchmark['false_positive']}",
        f"- False negatives: {benchmark['false_negative']}",
        f"- True negatives: {benchmark['true_negative']}",
        f"- Same-design variants: {benchmark['design_variant']}",
        f"- Unsure or missing: {benchmark['unsure_or_missing']}",
        "",
        "Reference labels are benchmark labels, not guaranteed truth.",
        "",
    ]
    return "\n".join(lines)


def resize_image_to_jpeg(source: Path, destination: Path, max_size: int) -> bool:
    try:
        from PIL import ImageOps

        destination.parent.mkdir(parents=True, exist_ok=True)
        with open_pillow_image(source) as image:
            prepared = ImageOps.exif_transpose(image).convert("RGB")
            prepared.thumbnail((max_size, max_size), pillow_resample_lanczos())
            prepared.save(destination, "JPEG")
        return destination.exists()
    except Exception:
        return False


def image_data_url_for_api(path: Path, tmpdir: Path, max_size: int) -> str:
    out = tmpdir / f"{stable_name_digest(str(path))}-{max_size}.jpg"
    if not resize_image_to_jpeg(path, out, max_size):
        msg = f"failed to prepare API image: {path}"
        raise RuntimeError(msg)
    encoded = base64.b64encode(out.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def ai_decision_cache_key(pair: JsonDict, left: VisualAsset, right: VisualAsset, model: str, max_image_size: int) -> str:
    left_hash = sha256(Path(left.preferred_path))
    right_hash = sha256(Path(right.preferred_path))
    parts = [
        candidate_pair_key(pair),
        model,
        AI_PAIR_PROMPT_VERSION,
        str(max_image_size),
        left_hash,
        right_hash,
    ]
    return "|".join(parts)


def retryable_api_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 409, 429, 500, 502, 503, 504}
    return bool(isinstance(exc, (urllib.error.URLError, TimeoutError)))


def parse_ai_decision_text(text: str) -> JsonDict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    payload = json.loads(cleaned)
    decision = payload.get("decision")
    if decision not in VALID_AI_DECISIONS:
        msg = f"invalid AI decision: {decision}"
        raise ValueError(msg)
    confidence = float(payload.get("confidence", 0))
    confidence = max(0.0, min(1.0, confidence))
    reason = payload.get("reason")
    if not reason:
        evidence_parts = []
        for key in ("product_evidence", "design_evidence", "difference_evidence"):
            values = payload.get(key) or []
            if isinstance(values, list) and values:
                evidence_parts.append(f"{key}: " + "; ".join(str(value) for value in values[:4]))
        reason = " | ".join(evidence_parts)
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(reason or "")[:1000],
        "product_evidence": payload.get("product_evidence") or [],
        "design_evidence": payload.get("design_evidence") or [],
        "difference_evidence": payload.get("difference_evidence") or [],
        "variant_fields": payload.get("variant_fields") or {},
        "review_required": bool(payload.get("review_required", False)),
        "review_flags": payload.get("review_flags") or [],
    }


def call_openai_pair_judge(
    api_key: str,
    model: str,
    left_image_url: str,
    right_image_url: str,
    timeout: int,
) -> JsonDict:
    prompt = ai_pair_judge_prompt()
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": "Image A:"},
                    {"type": "image_url", "image_url": {"url": left_image_url}},
                    {"type": "text", "text": "Image B:"},
                    {"type": "image_url", "image_url": {"url": right_image_url}},
                ],
            }
        ],
    }
    api_url = "https://api.openai.com/v1/chat/completions"
    request = urllib.request.Request(  # noqa: S310
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        if not request.full_url.startswith("https://"):
            msg = f"unexpected OpenAI API URL scheme: {request.full_url}"
            raise ValueError(msg)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if retryable_api_error(exc):
            raise
        message = exc.read().decode("utf-8", errors="replace")
        msg = f"OpenAI API error {exc.code}: {message}"
        raise RuntimeError(msg) from exc
    text = payload["choices"][0]["message"]["content"]
    parsed = parse_ai_decision_text(text)
    parsed["raw_response"] = payload
    return parsed


def jewelry_box_prompt(width: int | None, height: int | None) -> str:
    size_hint = f"Image size is {width}x{height} pixels. " if width and height else ""
    return (
        f"{size_hint}"
        "Find every visible jewelry item in this image. Return only visible rings, earrings, necklaces, pendants, "
        "bracelets, or unknown jewelry. Do not identify products or use catalog labels. Do not infer jewelry that is "
        "not visible. Return approximate bounding boxes in pixel coordinates relative to the original image. "
        "For rings, box the visible jewelry object itself, not the finger, hand, or surrounding skin. Include the visible "
        "metal band and stone/head when visible. The box center should lie on jewelry, not on adjacent finger skin. "
        "If you are uncertain, return up to three plausible boxes for the same visible jewelry item rather than one "
        "overconfident box. "
        "Use this exact JSON shape: "
        '{"items":[{"type":"ring","box":[x,y,w,h],"confidence":0.0,"visibility":"clear|partial|tiny|occluded",'
        '"notes":"short visual reason"}]}. If no jewelry is visible, return {"items":[]}.'
    )


def clamp_box(box: list[float], width: int, height: int) -> tuple[int, int, int, int] | None:
    if len(box) != 4:
        return None
    x, y, w, h = (float(value) for value in box)
    if w <= 0 or h <= 0:
        return None
    x1 = max(0, min(width - 1, round(x)))
    y1 = max(0, min(height - 1, round(y)))
    x2 = max(1, min(width, round(x + w)))
    y2 = max(1, min(height, round(y + h)))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


def expand_crop_box(
    box: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
    mode: str,
) -> tuple[int, int, int, int]:
    x, y, w, h = box
    cx = x + w / 2
    cy = y + h / 2
    if mode == "tight":
        target_w_float = float(w)
        target_h_float = float(h)
    elif mode == "padded":
        scale = 2.5
        target_w_float = w * scale
        target_h_float = h * scale
    elif mode == "square_padded":
        scale = 3.0
        side = max(w, h) * scale
        target_w_float = side
        target_h_float = side
    elif mode == "context":
        scale = 4.0
        side = max(w, h) * scale
        target_w_float = side
        target_h_float = side
    else:
        msg = f"unknown crop expansion mode: {mode}"
        raise ValueError(msg)
    target_w = max(1, min(image_width, round(target_w_float)))
    target_h = max(1, min(image_height, round(target_h_float)))
    left = round(cx - target_w / 2)
    top = round(cy - target_h / 2)
    left = max(0, min(image_width - target_w, left))
    top = max(0, min(image_height - target_h, top))
    return left, top, target_w, target_h


def parse_jewelry_box_response(text: str, image_width: int, image_height: int) -> list[JsonDict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    payload = json.loads(cleaned)
    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        msg = "jewelry box response must contain an items list"
        raise TypeError(msg)
    items = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        raw_type = str(raw_item.get("type", "unknown_jewelry")).strip().lower()
        box_type = raw_type if raw_type in VALID_JEWELRY_BOX_TYPES else "unknown_jewelry"
        raw_box = raw_item.get("box", [])
        if not isinstance(raw_box, list):
            continue
        clamped = clamp_box(raw_box, image_width, image_height)
        if clamped is None:
            continue
        confidence = max(0.0, min(1.0, float(raw_item.get("confidence", 0))))
        items.append(
            {
                "box_id": f"B{index:02d}",
                "type": box_type,
                "box": list(clamped),
                "confidence": confidence,
                "visibility": str(raw_item.get("visibility", ""))[:80],
                "notes": str(raw_item.get("notes", ""))[:300],
            }
        )
    return sorted(items, key=lambda item: item["confidence"], reverse=True)


def call_openai_jewelry_box_detector(
    api_key: str,
    model: str,
    image_url: str,
    width: int | None,
    height: int | None,
    timeout: int,
) -> JsonDict:
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": jewelry_box_prompt(width, height)},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        if not request.full_url.startswith("https://"):
            msg = f"unexpected OpenAI API URL scheme: {request.full_url}"
            raise ValueError(msg)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        msg = f"OpenAI API error {exc.code}: {message}"
        raise RuntimeError(msg) from exc
    text = payload["choices"][0]["message"]["content"]
    items = parse_jewelry_box_response(text, width or 1, height or 1)
    return {"items": items, "raw_response": payload, "prompt_version": JEWELRY_BOX_PROMPT_VERSION}


def ai_pair_judge_prompt() -> str:
    return (
        "You are judging jewelry catalog photos for product and design clustering. "
        "Classify the relationship between Image A and Image B. "
        "same_physical_product means the same exact sellable jewelry item/version, possibly from different angles, crops, edits, "
        "model shots, macro/detail shots, or packaging shots. It requires matching structure and no meaningful contradictions. "
        "same_sellable_product means same catalog product/version even if not literally the same physical photographed instance. "
        "same_design_variant means same design family but different product/variant, such as different metal tone, stone species/color, "
        "size/model, chain length, or charm variant. This is a valid final classification and does not automatically need human review. "
        "different_design means visually/product-structurally different design. "
        "Important for this dataset: catalog/studio images may have Photoshop edits, color correction, lighting changes, and fixed/unfixed "
        "versions of the same exact ring. A rose/yellow/white-looking metal tone difference can be an edit artifact. "
        "Color or metal-tone difference alone must NOT be classified as same_design_variant when the jewelry structure, shape, texture, "
        "stone layout, and proportions otherwise match. In that case, classify as same_physical_product or same_sellable_product. "
        "For this image-only run, do not use same_design_variant for metal tone, color cast, or Photoshop-looking color changes. "
        "If the only visible difference is metal/color tone and the form matches, the decision must be same_physical_product or "
        "same_sellable_product. Use same_design_variant only for a real non-color visible variant, such as different stone species, "
        "stone layout, size/model, chain length, charm, or another intentional catalog option that is not explainable as editing. "
        "Do not classify as same product just because style is similar. Stone count, stone positions, setting, focal geometry, motif, "
        "finish/texture, charms, and product type matter. If confident, classify; use unsure only when the boundary truly cannot be decided. "
        "Return only JSON with keys: decision, confidence, product_evidence, design_evidence, difference_evidence, variant_fields, "
        "review_required, review_flags, reason. "
        "decision must be one of: same_physical_product, same_sellable_product, same_design_variant, different_design, unsure. "
        "confidence must be 0 to 1. review_required should be true only for identity conflict, variant boundary unclear, insufficient views, "
        "stone count unclear, metal/source conflict, or contradictory evidence."
    )


def write_ai_review_sheet(path: Path, title: str, rows: list[JsonDict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    cards = []
    for row in rows:
        figures = []
        for key in ("source_asset_id", "target_asset_id"):
            asset = lookup[row[key]]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset.asset_id}-{stable_name_digest(asset.preferred_path)}.jpg"
            if not thumb.exists():
                make_thumbnail(source, thumb)
            figures.append(
                "<figure>"
                f"<img src='{html.escape(str(Path('thumbs') / thumb.name))}' alt=''>"
                f"<figcaption>{asset.asset_id}<br>ref: {html.escape(', '.join(asset.reference_cluster_ids) or 'none')}</figcaption>"
                "</figure>"
            )
        cards.append(
            "<section class='pair'>"
            f"<h2>{row['source_asset_id']} - {row['target_asset_id']} <span>score {row['score']:.4f}</span></h2>"
            f"<p><b>AI:</b> {html.escape(str(row.get('ai_decision')))} "
            f"({html.escape(str(row.get('ai_confidence')))} confidence)</p>"
            f"<p><b>Benchmark same:</b> {html.escape(str(row.get('benchmark_same_reference')))}</p>"
            f"<p><b>Reason:</b> {html.escape(row.get('ai_reason') or '')}</p>"
            f"<p><b>Review:</b> {html.escape(str(row.get('ai_review_required')))} "
            f"{html.escape(', '.join(row.get('ai_review_flags') or []))}</p>"
            "<div class='assets'>"
            + "\n".join(figures)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "h1{font-size:24px}.pair{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".pair h2{font-size:18px;margin:0 0 8px}.pair h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".pair p{font-size:13px;margin:4px 0}.assets{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:220px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:220px;height:220px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{len(rows)} pairs shown.</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def write_ai_adjudication_outputs(out_dir: Path, assets: list[VisualAsset], model: str, decisions: dict[str, JsonDict], benchmark: JsonDict) -> None:
    write_json(out_dir / "ai_decisions.json", decisions)
    write_json(out_dir / "ai_benchmark.json", {key: value for key, value in benchmark.items() if key != "rows"})
    (out_dir / "ai_benchmark.md").write_text(ai_benchmark_markdown(model, benchmark), encoding="utf-8")
    sheets_dir = out_dir / "review_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    write_ai_review_sheet(sheets_dir / "09_ai_all_decisions.html", "AI Pair Decisions", benchmark["rows"], assets, out_dir)
    write_ai_review_sheet(
        sheets_dir / "10_ai_false_positives.html",
        "AI False Positives",
        [row for row in benchmark["rows"] if product_same_decision(row["ai_decision"]) and not row.get("benchmark_same_reference")],
        assets,
        out_dir,
    )
    write_ai_review_sheet(
        sheets_dir / "11_ai_false_negatives.html",
        "AI False Negatives",
        [row for row in benchmark["rows"] if not product_same_decision(row["ai_decision"]) and row.get("benchmark_same_reference")],
        assets,
        out_dir,
    )
    for decision in sorted(VALID_AI_DECISIONS):
        safe_name = decision.replace("_", "-")
        write_ai_review_sheet(
            sheets_dir / f"12_ai_{safe_name}.html",
            f"AI {decision}",
            [row for row in benchmark["rows"] if row["ai_decision"] == decision],
            assets,
            out_dir,
        )


def selected_lifestyle_manifest_rows(manifest: Path, limit: int, category: str = "") -> list[JsonDict]:
    rows = []
    normalized_category = catalog_normalized_category(category) if category else None
    with manifest.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if normalized_category and row.get("category") != normalized_category:
                continue
            roles = row.get("image_roles", "")
            media_role = row.get("media_role", "")
            if "model_or_lifestyle" not in roles and media_role not in {"supporting", "shared_supporting"}:
                continue
            path = Path(row.get("preferred_path", ""))
            if not path.exists():
                continue
            rows.append(dict(row))
            if limit > 0 and len(rows) >= limit:
                break
    return rows


def asset_is_live_shot(asset: JsonDict) -> bool:
    return "model_or_lifestyle" in str(asset.get("image_roles", ""))


def crop_image(source: Path, destination: Path, box: tuple[int, int, int, int]) -> bool:
    x, y, w, h = box
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import ImageOps

        with open_pillow_image(source) as image:
            ImageOps.exif_transpose(image).crop((x, y, x + w, y + h)).convert("RGB").save(destination, "JPEG")
        return destination.exists()
    except Exception:
        return False


def box_area_ratio(box: list[int] | tuple[int, int, int, int], image_width: int, image_height: int) -> float:
    return (box[2] * box[3]) / max(1, image_width * image_height)


def box_touches_edge(box: list[int] | tuple[int, int, int, int], image_width: int, image_height: int) -> bool:
    x, y, w, h = box
    return x <= 0 or y <= 0 or x + w >= image_width or y + h >= image_height


def box_iou(left: list[int] | tuple[int, int, int, int], right: list[int] | tuple[int, int, int, int]) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    ix1 = max(lx, rx)
    iy1 = max(ly, ry)
    ix2 = min(lx + lw, rx + rw)
    iy2 = min(ly + lh, ry + rh)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    intersection = iw * ih
    union = lw * lh + rw * rh - intersection
    return intersection / union if union else 0.0


def foreground_product_box(source: Path, image_width: int, image_height: int, sample_size: int = 384) -> JsonDict | None:
    pixel_values: list[tuple[int, int, int]]
    try:
        with open_pillow_image(source) as source_image:
            image = source_image.convert("RGB")
            image.thumbnail((sample_size, sample_size), pillow_resample_lanczos())
            width, height = cast("tuple[int, int]", image.size)
            image_module = cast("Any", image)
            raw_pixels = image_module.get_flattened_data() if hasattr(image_module, "get_flattened_data") else image.getdata()
            pixel_values = [cast("tuple[int, int, int]", pixel) for pixel in raw_pixels]
    except Exception:
        return None

    def pixel_at(x: int, y: int) -> tuple[int, int, int]:
        return pixel_values[y * width + x]

    border: list[tuple[int, int, int]] = []
    for x in range(width):
        border.append(pixel_at(x, 0))
        border.append(pixel_at(x, height - 1))
    for y in range(height):
        border.append(pixel_at(0, y))
        border.append(pixel_at(width - 1, y))
    background = tuple(sorted(pixel[channel] for pixel in border)[len(border) // 2] for channel in range(3))
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            red, green, blue = pixel_at(x, y)
            distance = abs(red - background[0]) + abs(green - background[1]) + abs(blue - background[2])
            if distance > FOREGROUND_BACKGROUND_DISTANCE and not (red > 245 and green > 245 and blue > 245):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    x1 = min(xs)
    x2 = max(xs) + 1
    y1 = min(ys)
    y2 = max(ys) + 1
    scale_x = image_width / width
    scale_y = image_height / height
    box = clamp_box([x1 * scale_x, y1 * scale_y, (x2 - x1) * scale_x, (y2 - y1) * scale_y], image_width, image_height)
    if box is None:
        return None
    pixel_area_ratio = len(xs) / max(1, width * height)
    return {
        "box": list(box),
        "box_area_ratio": box_area_ratio(box, image_width, image_height),
        "pixel_area_ratio": pixel_area_ratio,
        "background_rgb": list(background),
    }


def owlv2_detection_sort_key(item: JsonDict) -> tuple[float, float]:
    label = str(item.get("label", ""))
    penalty = 0.10 if label in OWLV2_DEPRIORITIZED_LABELS else 0.0
    bonus = 0.03 if label in OWLV2_PREFERRED_LABELS else 0.0
    return float(item.get("score", 0.0)) + bonus - penalty, -float(item.get("area_ratio", 0.0))


def filter_owlv2_detections(raw_items: list[JsonDict], image_width: int, image_height: int) -> tuple[list[JsonDict], list[str]]:
    flags: list[str] = []
    scored = []
    dropped_tiny = 0
    dropped_huge = 0
    for item in raw_items:
        box = item.get("box", [])
        if not isinstance(box, list):
            continue
        clamped = clamp_box([float(value) for value in box], image_width, image_height)
        if clamped is None:
            continue
        area_ratio = box_area_ratio(clamped, image_width, image_height)
        if area_ratio < OWLV2_MIN_AREA_RATIO:
            dropped_tiny += 1
            continue
        if area_ratio > OWLV2_MAX_AREA_RATIO:
            dropped_huge += 1
            continue
        scored.append({**item, "box": list(clamped), "area_ratio": area_ratio})

    strong = [item for item in scored if float(item.get("score", 0.0)) >= OWLV2_MIN_SCORE]
    if strong:
        candidates = strong
    else:
        candidates = scored
        if scored:
            flags.append("low_detector_score")

    deduped: list[JsonDict] = []
    for item in sorted(candidates, key=owlv2_detection_sort_key, reverse=True):
        if any(box_iou(cast("list[int]", item["box"]), cast("list[int]", kept["box"])) >= 0.70 for kept in deduped):
            continue
        deduped.append(item)

    if not deduped:
        flags.append("no_owlv2_box")
        if dropped_tiny:
            flags.append("box_too_tiny")
        if dropped_huge:
            flags.append("box_too_huge")
    else:
        selected = deduped[0]
        if box_touches_edge(cast("list[int]", selected["box"]), image_width, image_height):
            flags.append("box_touches_edge")
        if str(selected.get("label", "")) in OWLV2_DEPRIORITIZED_LABELS:
            flags.append("selected_label_deprioritized")
        if float(selected.get("score", 0.0)) < OWLV2_MIN_SCORE:
            flags.append("low_detector_score")
        if len(deduped) > 1 and float(deduped[0].get("score", 0.0)) - float(deduped[1].get("score", 0.0)) <= 0.05:
            flags.append("multiple_close_competing_boxes")

    return deduped, sorted(set(flags))


def detector_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        return "cpu"
    return "cpu"


def load_owlv2_model(model_id: str, device_request: str) -> tuple[Any, Any, Any, Any, str]:
    try:
        import torch
        from PIL import Image
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
    except ImportError as exc:
        msg = "OWLv2 detector requires torch, Pillow, and transformers. Install requirements-local.txt first."
        raise RuntimeError(msg) from exc

    device = detector_device(device_request)
    cache_key = (model_id, device)
    if cache_key not in OWLV2_MODEL_CACHE:
        processor = Owlv2Processor.from_pretrained(model_id)
        model = Owlv2ForObjectDetection.from_pretrained(model_id).to(device)
        OWLV2_MODEL_CACHE[cache_key] = (torch, Image, processor, model)
    torch_module, image_module, processor, model = OWLV2_MODEL_CACHE[cache_key]
    return torch_module, image_module, processor, model, device


def call_owlv2_detector(source: Path, model_id: str, device_request: str, threshold: float) -> JsonDict:
    torch, image_module, processor, model, device = load_owlv2_model(model_id, device_request)
    image = image_module.open(source).convert("RGB")
    raw_inputs = processor(text=[OWLV2_PROMPTS], images=image, return_tensors="pt")
    inputs: dict[str, Any] = {key: value.to(device) if hasattr(value, "to") else value for key, value in raw_inputs.items()}
    with torch.no_grad():
        outputs = model(**inputs)
    target_sizes = torch.tensor([image.size[::-1]], device=device)
    post_process = cast("Any", processor.post_process_object_detection)
    results = post_process(outputs=outputs, target_sizes=target_sizes, threshold=threshold)[0]
    items = []
    for index, (score, label, box) in enumerate(zip(results["scores"], results["labels"], results["boxes"]), start=1):
        x1, y1, x2, y2 = [round(float(value)) for value in box.tolist()]
        clamped = clamp_box([x1, y1, x2 - x1, y2 - y1], image.width, image.height)
        if clamped is None:
            continue
        label_index = int(label)
        prompt = OWLV2_PROMPTS[label_index] if 0 <= label_index < len(OWLV2_PROMPTS) else f"label_{label_index}"
        items.append(
            {
                "box_id": f"O{index:02d}",
                "type": "ring",
                "label": prompt,
                "box": list(clamped),
                "score": float(score),
                "confidence": float(score),
                "visibility": "",
                "notes": f"OWLv2 prompt: {prompt}",
            }
        )
    return {"items": items, "prompt_version": OWLV2_PROMPT_VERSION, "model_id": model_id, "device": device}


def image_id_for_hash(image_hash: str, existing_ids: set[str]) -> str:
    for length in (12, 16, 20, 24, 32, 64):
        candidate = image_hash[:length]
        if candidate not in existing_ids:
            return candidate
    return image_hash


def build_image_registry(input_folder: Path) -> list[JsonDict]:
    if not input_folder.exists():
        msg = f"input folder does not exist: {input_folder}"
        raise FileNotFoundError(msg)
    records = []
    seen_hashes: dict[str, str] = {}
    used_ids: set[str] = set()
    for path in iter_image_files(input_folder):
        image_hash = sha256(path)
        if image_hash in seen_hashes:
            image_id = seen_hashes[image_hash]
            status = "duplicate"
        else:
            image_id = image_id_for_hash(image_hash, used_ids)
            seen_hashes[image_hash] = image_id
            used_ids.add(image_id)
            status = "ready"
        width, height = image_size(path)
        records.append(
            {
                "image_id": image_id,
                "source_path": str(path.resolve()),
                "filename": path.name,
                "width": width or 0,
                "height": height or 0,
                "sha256": image_hash,
                "status": status,
            }
        )
    return records


def write_image_manifest_csv(path: Path, records: list[JsonDict]) -> None:
    fields = ["image_id", "source_path", "filename", "width", "height", "sha256", "status"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fields})


def load_image_manifest(path: Path) -> list[JsonDict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        msg = "image manifest must contain a JSON list"
        raise TypeError(msg)
    records = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        required = {"image_id", "source_path", "filename", "width", "height", "sha256", "status"}
        missing = required.difference(raw)
        if missing:
            msg = f"image manifest record missing: {', '.join(sorted(missing))}"
            raise ValueError(msg)
        records.append(dict(raw))
    return records


def image_registry_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    records = build_image_registry(Path(args.input_folder).resolve())
    write_json(out_dir / "image_manifest.json", records)
    write_image_manifest_csv(out_dir / "image_manifest.csv", records)
    print(f"Images: {len(records)}")
    print(f"Ready: {sum(1 for record in records if record['status'] == 'ready')}")
    print(f"Wrote: {out_dir}")
    return 0


def single_final_product_id(row: JsonDict) -> str:
    ids = split_manifest_list(str(row.get("final_product_ids", "")))
    return ids[0] if len(ids) == 1 else ""


def benchmark_row_sort_key(row: JsonDict) -> tuple[str, str, str]:
    return str(row.get("category", "")), single_final_product_id(row), str(row.get("asset_id", ""))


def select_mixed_benchmark_rows(
    rows: list[JsonDict],
    per_category_products: int,
    identity_per_product: int,
    supporting_per_category: int,
) -> list[JsonDict]:
    by_category_product: dict[tuple[str, str], list[JsonDict]] = defaultdict(list)
    supporting_by_category: dict[str, list[JsonDict]] = defaultdict(list)
    for row in rows:
        product_id = single_final_product_id(row)
        if not product_id:
            continue
        if str(row.get("identity_eligible", "")).lower() == "true":
            by_category_product[(str(row.get("category", "")), product_id)].append(row)
        elif str(row.get("media_role", "")) in {"supporting", "shared_supporting"}:
            supporting_by_category[str(row.get("category", ""))].append(row)

    selected: list[JsonDict] = []
    selected_keys: set[str] = set()
    categories = sorted({str(row.get("category", "")) for row in rows if row.get("category")})
    for category in categories:
        product_count = 0
        for (product_category, _product_id), product_rows in sorted(by_category_product.items()):
            if product_category != category or len(product_rows) < identity_per_product:
                continue
            for row in sorted(product_rows, key=benchmark_row_sort_key)[:identity_per_product]:
                key = str(row.get("asset_id", ""))
                if key and key not in selected_keys:
                    selected.append(row)
                    selected_keys.add(key)
            product_count += 1
            if product_count >= per_category_products:
                break
        supporting_count = 0
        for row in sorted(supporting_by_category.get(category, []), key=benchmark_row_sort_key):
            key = str(row.get("asset_id", ""))
            if not key or key in selected_keys:
                continue
            selected.append(row)
            selected_keys.add(key)
            supporting_count += 1
            if supporting_count >= supporting_per_category:
                break
    return selected


def copy_benchmark_image(row: JsonDict, destination_dir: Path) -> JsonDict:
    source = Path(str(row["preferred_path"])).resolve()
    if not source.exists():
        msg = f"benchmark source image missing: {source}"
        raise FileNotFoundError(msg)
    suffix = source.suffix.lower() or ".jpg"
    destination = destination_dir / f"{row['asset_id']}-{source.stem}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {**row, "benchmark_source_path": str(source), "benchmark_raw_path": str(destination)}


def write_selected_benchmark_manifest(path: Path, rows: list[JsonDict]) -> None:
    fields = [
        "asset_id",
        "category",
        "final_product_ids",
        "media_role",
        "identity_eligible",
        "clustering_policy",
        "benchmark_source_path",
        "benchmark_raw_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def materialize_mixed_benchmark_command(args: argparse.Namespace) -> int:
    with Path(args.labels).open(newline="", encoding="utf-8") as handle:
        rows = cast("list[JsonDict]", list(csv.DictReader(handle)))
    selected = select_mixed_benchmark_rows(
        rows,
        per_category_products=args.per_category_products,
        identity_per_product=args.identity_per_product,
        supporting_per_category=args.supporting_per_category,
    )
    if not selected:
        print("ERROR: no benchmark rows selected", file=sys.stderr)
        return 1
    out_dir = Path(args.out).resolve()
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    copied = [copy_benchmark_image(row, raw_dir) for row in selected]
    write_selected_benchmark_manifest(out_dir / "selected_manifest.csv", copied)
    write_json(out_dir / "selected_manifest.json", copied)
    print(f"Images: {len(copied)}")
    print(f"Raw folder: {raw_dir}")
    print(f"Wrote: {out_dir}")
    return 0


def profile_cache_key(record: JsonDict, model: str, max_image_size: int) -> str:
    return "|".join([str(record["sha256"]), model, EVIDENCE_PROFILE_PROMPT_VERSION, str(max_image_size)])


def image_profile_prompt(width: int, height: int) -> str:
    return (
        f"Image size is {width}x{height} pixels. Analyze this jewelry photo for routing, not identity matching. "
        "Return only JSON with keys: scene_type, has_hand, has_person, background_type, jewelry_items, quality_flags, "
        "recommended_evidence_policy. scene_type must be one of clean_product, model_lifestyle, macro_detail, multi_item, "
        "packaging, uncertain. recommended_evidence_policy must be one of full_only, full_plus_crop, crop_heavy, review. "
        "Each jewelry_items entry must have type, dominance, object_completeness, box, confidence, identity_features. "
        "Use pixel boxes [x,y,w,h] around visible jewelry evidence. identity_features are short visual descriptors only; "
        "do not identify catalog products or use metadata."
    )


def parse_image_profile_text(text: str, image_id: str, width: int, height: int) -> JsonDict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        msg = "image profile must be a JSON object"
        raise TypeError(msg)
    scene_type = str(payload.get("scene_type", "uncertain"))
    if scene_type not in VALID_EVIDENCE_SCENE_TYPES:
        msg = f"invalid scene_type: {scene_type}"
        raise ValueError(msg)
    policy = str(payload.get("recommended_evidence_policy", "review"))
    if policy not in VALID_EVIDENCE_POLICIES:
        msg = f"invalid recommended_evidence_policy: {policy}"
        raise ValueError(msg)
    items = []
    raw_items = payload.get("jewelry_items") or []
    if not isinstance(raw_items, list):
        msg = "jewelry_items must be a list"
        raise TypeError(msg)
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        raw_box = raw_item.get("box") or []
        if not isinstance(raw_box, list):
            continue
        box = clamp_box([float(value) for value in raw_box], max(1, width), max(1, height))
        if box is None:
            continue
        dominance = str(raw_item.get("dominance", "uncertain"))
        completeness = str(raw_item.get("object_completeness", "uncertain"))
        features = raw_item.get("identity_features") or []
        items.append(
            {
                "type": str(raw_item.get("type", "unknown_jewelry"))[:80],
                "dominance": dominance if dominance in VALID_JEWELRY_DOMINANCE else "tiny",
                "object_completeness": completeness if completeness in VALID_OBJECT_COMPLETENESS else "uncertain",
                "box": list(box),
                "confidence": max(0.0, min(1.0, float(raw_item.get("confidence", 0)))),
                "identity_features": [str(feature)[:120] for feature in features[:8]] if isinstance(features, list) else [],
            }
        )
    flags = payload.get("quality_flags") or []
    return {
        "image_id": image_id,
        "image_width": width,
        "image_height": height,
        "scene_type": scene_type,
        "has_hand": bool(payload.get("has_hand", False)),
        "has_person": bool(payload.get("has_person", False)),
        "background_type": str(payload.get("background_type", "uncertain"))[:80],
        "jewelry_items": items,
        "quality_flags": [str(flag)[:80] for flag in flags[:12]] if isinstance(flags, list) else [],
        "recommended_evidence_policy": policy,
    }


def call_openai_image_profile(
    api_key: str,
    model: str,
    image_url: str,
    image_id: str,
    width: int,
    height: int,
    timeout: int,
) -> JsonDict:
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": image_profile_prompt(width, height)},
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    parsed = parse_image_profile_text(payload["choices"][0]["message"]["content"], image_id, width, height)
    parsed["raw_response"] = payload
    return parsed


def product_profile_cache_key(source_sha256: str, model: str, max_image_size: int) -> str:
    return "|".join([source_sha256, model, EVIDENCE_PROFILE_PROMPT_VERSION, str(max_image_size)])


def product_profile_payload(image_path: Path, image_id: str, args: argparse.Namespace) -> JsonDict:
    record = single_image_manifest_record(image_path, image_id)
    profile: JsonDict
    if args.mock_response:
        raw_text = Path(args.mock_response).read_text(encoding="utf-8")
        profile = parse_image_profile_text(raw_text, image_id, int(record["width"]), int(record["height"]))
        raw_response: JsonDict | None = {"mock_response": raw_text}
    else:
        load_dotenv()
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            msg = "OPENAI_API_KEY is not set"
            raise RuntimeError(msg)
        with tempfile.TemporaryDirectory(prefix="jewelry-product-profile-") as tmp:
            image_url = image_data_url_for_api(image_path, Path(tmp), args.max_image_size)
            profile = call_openai_image_profile(
                api_key,
                args.model,
                image_url,
                image_id,
                int(record["width"]),
                int(record["height"]),
                args.timeout,
            )
        raw_response = cast("JsonDict | None", profile.get("raw_response"))
    parsed_profile = {key: value for key, value in profile.items() if key != "raw_response"}
    return {
        "schema_version": PRODUCT_PROFILE_SCHEMA_VERSION,
        "image_id": image_id,
        "source_sha256": record["sha256"],
        "model": args.model,
        "prompt_version": EVIDENCE_PROFILE_PROMPT_VERSION,
        "max_image_size": args.max_image_size,
        "cache_key": product_profile_cache_key(str(record["sha256"]), args.model, args.max_image_size),
        "profile": parsed_profile,
        "raw_response": raw_response,
    }


def product_profile_error(message: str, error_type: str = "product_profile_error") -> JsonDict:
    return {
        "schema_version": PRODUCT_PROFILE_SCHEMA_VERSION,
        "status": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def product_profile_command(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    try:
        payload = product_profile_payload(Path(args.image).resolve(), args.image_id, args)
    except Exception as exc:
        payload = product_profile_error(str(exc), exc.__class__.__name__)
        write_json(out, payload)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2
    write_json(out, payload)
    print(f"Wrote: {out}")
    return 0


def image_profile_command(args: argparse.Namespace) -> int:
    load_dotenv()
    manifest = load_image_manifest(Path(args.manifest).resolve())
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "image_profile_cache.json"
    cache = load_ai_decision_cache(cache_path)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not args.from_cache and not api_key:
        print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    profiles = []
    with tempfile.TemporaryDirectory(prefix="jewelry-profile-") as tmp:
        tmpdir = Path(tmp)
        for index, record in enumerate(manifest, start=1):
            if record.get("status") != "ready":
                continue
            key = profile_cache_key(record, args.model, args.max_image_size)
            if key not in cache:
                if args.from_cache:
                    continue
                source = Path(str(record["source_path"]))
                print(f"Profiling {index}/{len(manifest)} {record['image_id']}: {source.name}", flush=True)
                image_url = image_data_url_for_api(source, tmpdir, args.max_image_size)
                profile = call_openai_image_profile(
                    api_key,
                    args.model,
                    image_url,
                    str(record["image_id"]),
                    int(record.get("width") or 1),
                    int(record.get("height") or 1),
                    args.timeout,
                )
                cache[key] = {
                    "model": args.model,
                    "prompt_version": EVIDENCE_PROFILE_PROMPT_VERSION,
                    "max_image_size": args.max_image_size,
                    "image_sha256": record["sha256"],
                    "profile": profile,
                }
            profiles.append(cast("JsonDict", cache[key]["profile"]))
    write_json(cache_path, cache)
    write_json(out_dir / "image_profiles.json", profiles)
    print(f"Profiles: {len(profiles)}")
    print(f"Wrote: {out_dir}")
    return 0


def load_profiles(path: Path) -> dict[str, JsonDict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        msg = "image profiles must contain a JSON list"
        raise TypeError(msg)
    profiles = {}
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        image_id = str(raw.get("image_id", ""))
        if not image_id:
            msg = "image profile missing image_id"
            raise ValueError(msg)
        profiles[image_id] = raw
    return profiles


def strongest_profile_item(profile: JsonDict) -> JsonDict | None:
    items = [item for item in profile.get("jewelry_items", []) if isinstance(item, dict)]
    if not items:
        return None
    return max(items, key=lambda item: float(item.get("confidence", 0.0)) * (item.get("box", [0, 0, 0, 0])[2] or 1))


def should_generate_crop_views(profile: JsonDict) -> bool:
    scene_type = str(profile.get("scene_type", "uncertain"))
    if scene_type in {"model_lifestyle", "multi_item", "uncertain"} or profile.get("has_hand"):
        return True
    if str(profile.get("recommended_evidence_policy")) in {"full_plus_crop", "crop_heavy", "review"}:
        return True
    for item in profile.get("jewelry_items", []) or []:
        if isinstance(item, dict) and item.get("dominance") in {"tiny", "small"}:
            return True
    return False


def view_record(
    image_id: str,
    view_type: str,
    source: str,
    box: tuple[int, int, int, int],
    view_path: Path,
    risk_flags: list[str] | None = None,
    usable: bool = True,
) -> JsonDict:
    return {
        "view_id": f"{image_id}_{view_type}",
        "image_id": image_id,
        "view_type": view_type,
        "source": source,
        "box": list(box),
        "view_path": str(view_path),
        "risk_flags": sorted(set(risk_flags or [])),
        "usable_for_retrieval": usable,
    }


def copy_or_crop_view(source_path: Path, destination: Path, box: tuple[int, int, int, int]) -> bool:
    if box[0] == 0 and box[1] == 0:
        width, height = image_size(source_path)
        if width == box[2] and height == box[3]:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
            return True
    return crop_image(source_path, destination, box)


def generate_evidence_views(
    manifest: list[JsonDict],
    profiles: dict[str, JsonDict],
    out_dir: Path,
    detector: str = "profile",
    owlv2_model: str = "google/owlv2-base-patch16-ensemble",
    owlv2_threshold: float = 0.05,
    device: str = "auto",
) -> list[JsonDict]:
    views_dir = out_dir / "views"
    views: list[JsonDict] = []
    for record in manifest:
        if record.get("status") != "ready":
            continue
        image_id = str(record["image_id"])
        source_path = Path(str(record["source_path"]))
        width = int(record.get("width") or 0)
        height = int(record.get("height") or 0)
        if not width or not height:
            measured_width, measured_height = image_size(source_path)
            width = measured_width or 1
            height = measured_height or 1
        profile = profiles.get(image_id, {"scene_type": "uncertain", "quality_flags": ["missing_profile"], "jewelry_items": []})
        risk_flags = list(profile.get("quality_flags") or [])
        full_path = views_dir / f"{image_id}_full_image.jpg"
        copy_or_crop_view(source_path, full_path, (0, 0, width, height))
        views.append(view_record(image_id, "full_image", "full", (0, 0, width, height), full_path, []))

        item = strongest_profile_item(profile)
        if not should_generate_crop_views(profile):
            continue
        if item is None:
            risk_flags.append("missing_jewelry_box")
            box = expand_crop_box((0, 0, width, height), width, height, "context")
        else:
            box = cast("tuple[int, int, int, int]", tuple(cast("list[int]", item["box"])))
        if str(profile.get("scene_type")) == "uncertain" or str(profile.get("recommended_evidence_policy")) == "review":
            risk_flags.append("profile_review_risk")

        vlm_box = expand_crop_box(box, width, height, "context")
        vlm_path = views_dir / f"{image_id}_vlm_context.jpg"
        if crop_image(source_path, vlm_path, vlm_box):
            views.append(view_record(image_id, "vlm_context", "vlm", vlm_box, vlm_path, risk_flags))

        detector_items: list[JsonDict] = []
        if detector == "owlv2":
            detection = call_owlv2_detector(source_path, owlv2_model, device, owlv2_threshold)
            detector_items, detector_flags = filter_owlv2_detections(cast("list[JsonDict]", detection["items"]), width, height)
            risk_flags.extend(detector_flags)
        if detector_items:
            detector_box = cast("tuple[int, int, int, int]", tuple(cast("list[int]", detector_items[0]["box"])))
            detector_source = "owlv2"
        else:
            detector_box = box
            detector_source = "vlm"
        for view_type, mode in (("owlv2_padded", "padded"), ("owlv2_context", "context")):
            expanded = expand_crop_box(detector_box, width, height, mode)
            view_path = views_dir / f"{image_id}_{view_type}.jpg"
            if crop_image(source_path, view_path, expanded):
                views.append(view_record(image_id, view_type, detector_source, expanded, view_path, risk_flags))
            if sum(1 for view in views if view["image_id"] == image_id) >= 4:
                break
    return views


def write_evidence_review(path: Path, views: list[JsonDict], out_dir: Path) -> None:
    by_image: dict[str, list[JsonDict]] = defaultdict(list)
    for view in views:
        by_image[str(view["image_id"])].append(view)
    sections = []
    for image_id, image_views in sorted(by_image.items()):
        figures = []
        for view in image_views:
            rel = os.path.relpath(str(view["view_path"]), out_dir)
            figures.append(
                "<figure>"
                f"<img src='{html.escape(rel)}' alt=''>"
                f"<figcaption>{html.escape(view['view_type'])}<br>{html.escape(', '.join(view.get('risk_flags') or []))}</figcaption>"
                "</figure>"
            )
        sections.append(f"<section><h2>{html.escape(image_id)}</h2><div class='views'>{''.join(figures)}</div></section>")
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Evidence Views</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "section{background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}.views{display:flex;gap:10px;flex-wrap:wrap}"
        "figure{margin:0;width:210px;border:1px solid #e5e7eb;background:#fafafa;padding:6px}img{width:210px;height:210px;object-fit:contain;background:#eee}"
        "figcaption{font-size:12px;word-break:break-word}</style></head><body><h1>Evidence Views</h1>"
        + "\n".join(sections)
        + "</body></html>",
        encoding="utf-8",
    )


def generate_evidence_command(args: argparse.Namespace) -> int:
    manifest = load_image_manifest(Path(args.manifest).resolve())
    profiles = load_profiles(Path(args.profiles).resolve())
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    views = generate_evidence_views(
        manifest,
        profiles,
        out_dir,
        detector=args.detector,
        owlv2_model=args.owlv2_model,
        owlv2_threshold=args.owlv2_threshold,
        device=args.device,
    )
    write_json(out_dir / "evidence_views.json", views)
    write_evidence_review(out_dir / "evidence_review.html", views, out_dir)
    print(f"Views: {len(views)}")
    print(f"Wrote: {out_dir}")
    return 0


def load_evidence_views(path: Path) -> list[JsonDict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        msg = "evidence views must contain a JSON list"
        raise TypeError(msg)
    views = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        if raw.get("view_type") not in EVIDENCE_VIEW_TYPES:
            msg = f"invalid view_type: {raw.get('view_type')}"
            raise ValueError(msg)
        views.append(raw)
    return views


def embed_evidence_views(
    views: list[JsonDict],
    provider: EmbeddingProvider,
    out_dir: Path,
) -> tuple[dict[str, list[float]], list[JsonDict]]:
    cache_path = out_dir / "view_embedding_cache.json"
    cache = load_embedding_cache(cache_path)
    vectors: dict[str, list[float]] = {}
    records: list[JsonDict] = []
    usable = [view for view in views if view.get("usable_for_retrieval", True)]
    for index, view in enumerate(usable, start=1):
        view_path = Path(str(view["view_path"]))
        if not view_path.exists():
            records.append({"view_id": view["view_id"], "status": "missing_image", "view_path": str(view_path)})
            continue
        key, image_hash = embedding_cache_key(provider, view_path, str(view["view_id"]))
        if key in cache:
            vector = cache[key]["vector"]
            status = "cache_hit"
        else:
            print(f"Embedding view {index}/{len(usable)} {view['view_id']}", flush=True)
            vector = provider.embed(view_path)
            cache[key] = {
                "provider": provider.provider_id,
                "view": view["view_id"],
                "image_sha256": image_hash,
                "image_path": str(view_path),
                "vector": vector,
            }
            status = "embedded"
        vectors[str(view["view_id"])] = vector
        records.append(
            {
                "view_id": view["view_id"],
                "image_id": view["image_id"],
                "view_type": view["view_type"],
                "status": status,
                "provider": provider.provider_id,
                "image_sha256": image_hash,
                "dimensions": len(vector),
            }
        )
    write_json(cache_path, cache)
    return vectors, records


def single_image_manifest_record(image_path: Path, image_id: str) -> JsonDict:
    if not image_path.exists():
        msg = f"image does not exist: {image_path}"
        raise FileNotFoundError(msg)
    if not image_path.is_file():
        msg = f"image is not a file: {image_path}"
        raise FileNotFoundError(msg)
    width, height = image_size(image_path)
    if not width or not height:
        msg = f"could not read image dimensions: {image_path}"
        raise ValueError(msg)
    return {
        "image_id": image_id,
        "source_path": str(image_path.resolve()),
        "filename": image_path.name,
        "width": int(width),
        "height": int(height),
        "sha256": sha256(image_path),
        "status": "ready",
    }


def default_product_embed_views(record: JsonDict) -> list[JsonDict]:
    width = int(record["width"])
    height = int(record["height"])
    image_id = str(record["image_id"])
    return [
        view_record(
            image_id=image_id,
            view_type="full_image",
            source="full",
            box=(0, 0, width, height),
            view_path=Path(str(record["source_path"])),
            risk_flags=[],
            usable=True,
        )
    ]


def profile_product_embed_views(record: JsonDict, profile: JsonDict, out_dir: Path, args: argparse.Namespace) -> list[JsonDict]:
    profile = dict(profile)
    profile["image_id"] = str(record["image_id"])
    return generate_evidence_views(
        [record],
        {str(record["image_id"]): profile},
        out_dir,
        detector=args.detector,
        owlv2_model=args.owlv2_model,
        owlv2_threshold=args.owlv2_threshold,
        device=args.device,
    )


def product_embedding_payload(image_path: Path, image_id: str, provider: EmbeddingProvider, args: argparse.Namespace) -> JsonDict:
    record = single_image_manifest_record(image_path, image_id)
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="jewelry-product-embed-") as tmp:
        tmpdir = Path(tmp)
        profile: JsonDict | None = None
        if args.profile:
            raw_profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
            if not isinstance(raw_profile, dict):
                msg = "profile JSON must contain one object"
                raise TypeError(msg)
            if isinstance(raw_profile.get("profile"), dict):
                profile = cast("JsonDict", raw_profile["profile"])
            else:
                profile = cast("JsonDict", raw_profile)
            views = profile_product_embed_views(record, profile, tmpdir / "evidence", args)
        else:
            views = default_product_embed_views(record)
            warnings.append("no_profile_supplied_full_image_only")

        vectors, records = embed_evidence_views(views, provider, tmpdir / "embeddings")
        record_by_view_id = {str(item["view_id"]): item for item in records}
        crops = []
        embedding_dim = 0
        for view in views:
            view_id = str(view["view_id"])
            vector = vectors.get(view_id)
            if vector is None:
                status = record_by_view_id.get(view_id, {}).get("status", "missing_embedding")
                warnings.append(f"{view_id}:{status}")
                continue
            embedding_dim = len(vector)
            crops.append(
                {
                    "crop_id": view_id,
                    "view_type": view["view_type"],
                    "box": view["box"],
                    "source": view["source"],
                    "risk_flags": view.get("risk_flags", []),
                    "usable_for_retrieval": bool(view.get("usable_for_retrieval", True)),
                    "embedding": vector,
                }
            )

    if not crops:
        msg = "no usable embeddings were produced"
        raise RuntimeError(msg)
    payload: JsonDict = {
        "schema_version": PRODUCT_EMBED_SCHEMA_VERSION,
        "image_id": image_id,
        "embedding_model": provider.provider_id,
        "preprocess_version": PRODUCT_EMBED_PREPROCESS_VERSION,
        "embedding_dim": embedding_dim,
        "source_sha256": record["sha256"],
        "source_uri": str(image_path.resolve()),
        "crops": crops,
        "warnings": warnings,
    }
    if profile is not None:
        payload["profile_scene_type"] = str(profile.get("scene_type", "uncertain"))
        payload["profile_evidence_policy"] = str(profile.get("recommended_evidence_policy", ""))
        payload["profile_has_hand"] = bool(profile.get("has_hand"))
        payload["profile_has_person"] = bool(profile.get("has_person"))
    return payload


def product_embed_error(message: str, error_type: str = "product_embed_error") -> JsonDict:
    return {
        "schema_version": PRODUCT_EMBED_SCHEMA_VERSION,
        "status": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def product_embed_command(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    try:
        provider = build_embedding_provider(args)
        payload = product_embedding_payload(Path(args.image).resolve(), args.image_id, provider, args)
    except Exception as exc:
        payload = product_embed_error(str(exc), exc.__class__.__name__)
        write_json(out, payload)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2
    write_json(out, payload)
    print(f"Wrote: {out}")
    return 0


def feature_tokens(profile: JsonDict) -> set[str]:
    tokens: set[str] = set()
    for item in profile.get("jewelry_items", []) or []:
        if not isinstance(item, dict):
            continue
        tokens.add(str(item.get("type", "")).lower())
        for feature in item.get("identity_features", []) or []:
            tokens.update(re.findall(r"[a-z0-9]+", str(feature).lower()))
    return {token for token in tokens if token and len(token) > 2}


def profile_feature_agreement(left: JsonDict | None, right: JsonDict | None) -> float:
    if not left or not right:
        return 0.0
    left_tokens = feature_tokens(left)
    right_tokens = feature_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def full_image_similarity(rows: list[JsonDict]) -> float:
    for row in rows:
        if row["query_view_type"] == "full_image" and row["candidate_view_type"] == "full_image":
            return float(row["similarity"])
    return 0.0


def score_retrieval_candidate(rows: list[JsonDict], feature_agreement: float) -> JsonDict:
    sorted_rows = sorted(rows, key=lambda item: float(item["similarity"]), reverse=True)
    best = float(sorted_rows[0]["similarity"]) if sorted_rows else 0.0
    second = float(sorted_rows[1]["similarity"]) if len(sorted_rows) > 1 else 0.0
    full = full_image_similarity(sorted_rows)
    if len(sorted_rows) > 1:
        score = 0.60 * best + 0.25 * second + 0.10 * full + 0.05 * feature_agreement
    else:
        score = 0.85 * best + 0.10 * full + 0.05 * feature_agreement
    return {
        "score": score,
        "best_view_similarity": best,
        "second_view_agreement": second,
        "full_image_similarity": full,
        "profile_feature_agreement": feature_agreement,
        "strongest_match": sorted_rows[0] if sorted_rows else {},
        "view_match_count": len(sorted_rows),
    }


def build_retrieval_candidates(
    views: list[JsonDict],
    vectors: dict[str, list[float]],
    profiles: dict[str, JsonDict] | None = None,
    top_k: int = 20,
) -> list[JsonDict]:
    profiles = profiles or {}
    view_by_id = {str(view["view_id"]): view for view in views}
    views_by_image: dict[str, list[JsonDict]] = defaultdict(list)
    for view in views:
        if view.get("usable_for_retrieval", True) and str(view["view_id"]) in vectors:
            views_by_image[str(view["image_id"])].append(view)
    candidates = []
    for query_image_id, query_views in sorted(views_by_image.items()):
        grouped: dict[str, list[JsonDict]] = defaultdict(list)
        for query_view in query_views:
            query_vector = vectors[str(query_view["view_id"])]
            scored = []
            for candidate_view_id, candidate_vector in vectors.items():
                candidate_view = view_by_id[candidate_view_id]
                candidate_image_id = str(candidate_view["image_id"])
                if candidate_image_id == query_image_id:
                    continue
                scored.append(
                    {
                        "query_view_id": query_view["view_id"],
                        "query_view_type": query_view["view_type"],
                        "candidate_view_id": candidate_view_id,
                        "candidate_view_type": candidate_view["view_type"],
                        "candidate_image_id": candidate_image_id,
                        "similarity": cosine_similarity(query_vector, candidate_vector),
                    }
                )
            for row in sorted(scored, key=lambda item: float(item["similarity"]), reverse=True)[:top_k]:
                grouped[str(row["candidate_image_id"])].append(row)
        ranked = []
        for candidate_image_id, rows in grouped.items():
            agreement = profile_feature_agreement(profiles.get(query_image_id), profiles.get(candidate_image_id))
            score_parts = score_retrieval_candidate(rows, agreement)
            ranked.append(
                {
                    "query_image_id": query_image_id,
                    "candidate_image_id": candidate_image_id,
                    **score_parts,
                    "view_matches": sorted(rows, key=lambda item: float(item["similarity"]), reverse=True)[:top_k],
                }
            )
        for rank, item in enumerate(sorted(ranked, key=lambda row: float(row["score"]), reverse=True)[:top_k], start=1):
            candidates.append({**item, "rank": rank})
    return candidates


def retrieval_summary(candidates: list[JsonDict], image_count: int) -> JsonDict:
    top1_scores = [float(item["score"]) for item in candidates if int(item["rank"]) == 1]
    margins = []
    by_query: dict[str, list[JsonDict]] = defaultdict(list)
    for item in candidates:
        by_query[str(item["query_image_id"])].append(item)
    for rows in by_query.values():
        ranked = sorted(rows, key=lambda item: int(item["rank"]))
        if len(ranked) >= 2:
            margins.append(float(ranked[0]["score"]) - float(ranked[1]["score"]))
    return {
        "image_count": image_count,
        "candidate_count": len(candidates),
        "queries_with_candidates": len(by_query),
        "mean_top1_score": sum(top1_scores) / len(top1_scores) if top1_scores else 0.0,
        "mean_top1_margin": sum(margins) / len(margins) if margins else 0.0,
    }


def write_retrieval_review(path: Path, candidates: list[JsonDict]) -> None:
    rows = []
    for item in candidates:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item['query_image_id']))}</td>"
            f"<td>{html.escape(str(item['rank']))}</td>"
            f"<td>{html.escape(str(item['candidate_image_id']))}</td>"
            f"<td>{float(item['score']):.4f}</td>"
            f"<td>{float(item['best_view_similarity']):.4f}</td>"
            f"<td>{html.escape(str(item.get('strongest_match', {}).get('query_view_type', '')))} - "
            f"{html.escape(str(item.get('strongest_match', {}).get('candidate_view_type', '')))}</td>"
            "</tr>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Retrieval Candidates</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;font-size:13px;text-align:left}"
        "th{background:#f3f4f6}</style></head><body><h1>Retrieval Candidates</h1><table><thead><tr>"
        "<th>Query</th><th>Rank</th><th>Candidate</th><th>Score</th><th>Best</th><th>Views</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table></body></html>",
        encoding="utf-8",
    )


def multi_view_retrieve_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    views = load_evidence_views(Path(args.evidence).resolve())
    profiles = load_profiles(Path(args.profiles).resolve()) if args.profiles else {}
    provider = build_embedding_provider(args)
    vectors, embedding_records = embed_evidence_views(views, provider, out_dir)
    candidates = build_retrieval_candidates(views, vectors, profiles, top_k=args.top_k)
    write_json(out_dir / "view_embeddings.json", embedding_records)
    write_json(out_dir / "retrieval_candidates.json", candidates)
    write_json(out_dir / "retrieval_summary.json", retrieval_summary(candidates, len({view["image_id"] for view in views})))
    write_retrieval_review(out_dir / "retrieval_review.html", candidates)
    print(f"Embedded views: {len(vectors)}")
    print(f"Candidates: {len(candidates)}")
    print(f"Wrote: {out_dir}")
    return 0


def candidate_margin(candidate: JsonDict, by_query: dict[str, list[JsonDict]]) -> float:
    rows = sorted(by_query[str(candidate["query_image_id"])], key=lambda item: float(item["score"]), reverse=True)
    if len(rows) <= 1:
        return 1.0
    candidate_id = candidate.get("candidate_image_id")
    top_two_ids = {rows[0].get("candidate_image_id"), rows[1].get("candidate_image_id")}
    if candidate_id not in top_two_ids:
        return 1.0
    return float(rows[0]["score"]) - float(rows[1]["score"])


def retrieval_candidate_risk(candidate: JsonDict, profiles: dict[str, JsonDict], margin: float, low_margin: float) -> list[str]:
    flags = []
    if int(candidate.get("rank", 99)) <= 3:
        flags.append("top_3_candidate")
    if margin <= low_margin:
        flags.append("low_score_margin")
    if float(candidate.get("second_view_agreement", 0.0)) <= 0 or float(candidate.get("best_view_similarity", 0.0)) - float(candidate.get("second_view_agreement", 0.0)) > 0.10:
        flags.append("conflicting_or_single_view_evidence")
    for image_id in (str(candidate["query_image_id"]), str(candidate["candidate_image_id"])):
        profile = profiles.get(image_id, {})
        if profile.get("recommended_evidence_policy") == "review" or profile.get("quality_flags"):
            flags.append("profile_quality_risk")
    return sorted(set(flags))


def unordered_image_pair_key(left_id: object, right_id: object) -> tuple[str, str]:
    left, right = sorted([str(left_id), str(right_id)])
    return left, right


def dedupe_unordered_candidates(candidates: list[JsonDict]) -> list[JsonDict]:
    best_by_pair: dict[tuple[str, str], JsonDict] = {}
    for candidate in candidates:
        key = unordered_image_pair_key(candidate["query_image_id"], candidate["candidate_image_id"])
        current = best_by_pair.get(key)
        if current is None or float(candidate.get("score", 0.0)) > float(current.get("score", 0.0)):
            best_by_pair[key] = candidate
    return sorted(best_by_pair.values(), key=lambda item: (str(item["query_image_id"]), int(item.get("rank", 99)), -float(item.get("score", 0.0))))


def adjudication_queue(candidates: list[JsonDict], profiles: dict[str, JsonDict], low_margin: float) -> list[JsonDict]:
    by_query: dict[str, list[JsonDict]] = defaultdict(list)
    for candidate in candidates:
        by_query[str(candidate["query_image_id"])].append(candidate)
    queue = []
    for candidate in candidates:
        margin = candidate_margin(candidate, by_query)
        flags = retrieval_candidate_risk(candidate, profiles, margin, low_margin)
        rank = int(candidate.get("rank", 99))
        include = rank == 1
        include = include or (rank == 2 and "low_score_margin" in flags)
        include = include or (rank <= 3 and ("profile_quality_risk" in flags or "conflicting_or_single_view_evidence" in flags))
        if include:
            queue.append({**candidate, "score_margin": margin, "adjudication_reasons": flags})
    return dedupe_unordered_candidates(queue)


def parse_retrieval_adjudication_text(text: str) -> JsonDict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    payload = json.loads(cleaned)
    decision = str(payload.get("decision", "unsure"))
    if decision not in VALID_RETRIEVAL_ADJUDICATION_DECISIONS:
        msg = f"invalid retrieval adjudication decision: {decision}"
        raise ValueError(msg)
    raw_confidence = payload.get("confidence", 0)
    if isinstance(raw_confidence, str):
        confidence = {"low": 0.35, "medium": 0.65, "high": 0.85}.get(raw_confidence.strip().lower(), 0.0)
    else:
        confidence = float(raw_confidence)
    return {
        "decision": decision,
        "confidence": max(0.0, min(1.0, confidence)),
        "review_required": bool(payload.get("review_required", decision == "unsure")),
        "review_flags": payload.get("review_flags") or [],
        "reason": str(payload.get("reason", ""))[:1000],
    }


def retrieval_adjudication_cache_key(candidate: JsonDict, views_by_id: dict[str, JsonDict], model: str, max_image_size: int) -> str:
    strongest = candidate.get("strongest_match", {})
    query_view = views_by_id[str(strongest.get("query_view_id"))]
    candidate_view = views_by_id[str(strongest.get("candidate_view_id"))]
    return "|".join(
        [
            str(candidate["query_image_id"]),
            str(candidate["candidate_image_id"]),
            model,
            RETRIEVAL_ADJUDICATION_PROMPT_VERSION,
            str(max_image_size),
            sha256(Path(str(query_view["view_path"]))),
            sha256(Path(str(candidate_view["view_path"]))),
        ]
    )


def retrieval_adjudication_prompt(candidate: JsonDict, query_profile: JsonDict, candidate_profile: JsonDict) -> str:
    return (
        "Decide whether two jewelry photos show the same sellable product. Use visual evidence only. "
        "The first pair of images are the strongest retrieved views; full images provide context when included. "
        "VLM profile summaries are routing hints and may be wrong. Embedding scores are candidate-generation evidence, not truth. "
        "Be strict about product identity. same_product requires visible product-specific structure that would distinguish this exact "
        "sellable jewelry item from a similar design: stone layout/count/shape, setting geometry, band/chain structure, motif, texture, "
        "proportions, and distinctive construction. Do not choose same_product just because the item is the same jewelry type, metal tone, "
        "single-stone style, hand pose, crop geometry, lighting, or model/lifestyle composition. If jewelry is tiny, blurry, generic, or "
        "lacks enough visible distinguishing detail, choose same_design_variant when it appears to be the same design family, otherwise unsure. "
        "Use same_product only when there is clear identity evidence and no meaningful contradiction. "
        "Return only JSON with keys query_image_id, candidate_id, decision, confidence, review_required, review_flags, reason. "
        "decision must be one of same_product, same_design_variant, different, unsure. "
        f"Query profile: {json.dumps(query_profile, ensure_ascii=False)[:1800]}\n"
        f"Candidate profile: {json.dumps(candidate_profile, ensure_ascii=False)[:1800]}\n"
        f"Retrieval scores: score={float(candidate['score']):.4f}, best={float(candidate['best_view_similarity']):.4f}, "
        f"second={float(candidate['second_view_agreement']):.4f}, full={float(candidate['full_image_similarity']):.4f}."
    )


def call_openai_retrieval_adjudicator(
    api_key: str,
    model: str,
    candidate: JsonDict,
    views_by_id: dict[str, JsonDict],
    profiles: dict[str, JsonDict],
    tmpdir: Path,
    max_image_size: int,
    timeout: int,
) -> JsonDict:
    strongest = candidate.get("strongest_match", {})
    query_view = views_by_id[str(strongest["query_view_id"])]
    candidate_view = views_by_id[str(strongest["candidate_view_id"])]
    query_full_view = views_by_id.get(f"{candidate['query_image_id']}_full_image")
    candidate_full_view = views_by_id.get(f"{candidate['candidate_image_id']}_full_image")
    prompt = retrieval_adjudication_prompt(
        candidate,
        profiles.get(str(candidate["query_image_id"]), {}),
        profiles.get(str(candidate["candidate_image_id"]), {}),
    )
    content: list[JsonDict] = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "Strongest query view:"},
        {"type": "image_url", "image_url": {"url": image_data_url_for_api(Path(str(query_view["view_path"])), tmpdir, max_image_size)}},
        {"type": "text", "text": "Strongest candidate view:"},
        {"type": "image_url", "image_url": {"url": image_data_url_for_api(Path(str(candidate_view["view_path"])), tmpdir, max_image_size)}},
    ]
    if query_full_view and query_full_view["view_id"] != query_view["view_id"]:
        content.extend(
            [
                {"type": "text", "text": "Full query image:"},
                {"type": "image_url", "image_url": {"url": image_data_url_for_api(Path(str(query_full_view["view_path"])), tmpdir, max_image_size)}},
            ]
        )
    if candidate_full_view and candidate_full_view["view_id"] != candidate_view["view_id"]:
        content.extend(
            [
                {"type": "text", "text": "Full candidate image:"},
                {"type": "image_url", "image_url": {"url": image_data_url_for_api(Path(str(candidate_full_view["view_path"])), tmpdir, max_image_size)}},
            ]
        )
    body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": content}]}
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    parsed = parse_retrieval_adjudication_text(payload["choices"][0]["message"]["content"])
    parsed["raw_response"] = payload
    return parsed


def decision_pair_key(decision: JsonDict) -> tuple[str, str]:
    return str(decision.get("query_image_id")), str(decision.get("candidate_id"))


def candidate_pair_key_for_retrieval(candidate: JsonDict) -> tuple[str, str]:
    return str(candidate.get("query_image_id")), str(candidate.get("candidate_image_id"))


def json_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def auto_accepted_match(
    decision: JsonDict,
    candidate: JsonDict,
    profiles: dict[str, JsonDict] | None = None,
    min_confidence: float = 0.90,
    min_score: float = 0.90,
    min_margin: float = 0.03,
) -> bool:
    if decision.get("decision") not in {"same_product", "same_design_variant"}:
        return False
    if decision.get("review_required"):
        return False
    min_confidence, min_score, min_margin = auto_accept_thresholds(decision, candidate, profiles, min_confidence, min_score, min_margin)
    return (
        json_float(decision.get("confidence"), 0.0) >= min_confidence
        and json_float(candidate.get("score", decision.get("retrieval_score")), 0.0) >= min_score
        and json_float(candidate.get("score_margin", decision.get("score_margin")), 0.0) >= min_margin
    )


def strongly_rejected_match(decision: JsonDict, min_confidence: float = 0.90) -> bool:
    return decision.get("decision") == "different" and json_float(decision.get("confidence"), 0.0) >= min_confidence and not decision.get("review_required")


def profile_identity_strength(profile: JsonDict | None) -> float:
    if not profile:
        return 0.0
    if profile.get("scene_type") == "multi_item":
        return 0.0
    item_count = len([item for item in profile.get("jewelry_items", []) or [] if isinstance(item, dict)])
    if item_count != 1:
        return 0.0
    image_width = int(profile.get("image_width") or 0)
    image_height = int(profile.get("image_height") or 0)
    if not image_width or not image_height:
        boxes = [item.get("box") for item in profile.get("jewelry_items", []) or [] if isinstance(item, dict)]
        image_width = max([int(box[0] + box[2]) for box in boxes if isinstance(box, list) and len(box) == 4] or [1])
        image_height = max([int(box[1] + box[3]) for box in boxes if isinstance(box, list) and len(box) == 4] or [1])
    strength = 0.0
    for item in profile.get("jewelry_items", []) or []:
        if not isinstance(item, dict):
            continue
        dominance = str(item.get("dominance", "tiny"))
        dominance_score = {"tiny": 0.0, "small": 0.15, "medium": 0.55, "dominant": 0.80}.get(dominance, 0.0)
        box = item.get("box")
        if isinstance(box, list) and len(box) == 4:
            area_ratio = (float(box[2]) * float(box[3])) / max(1, image_width * image_height)
            if area_ratio >= 0.20:
                dominance_score = max(dominance_score, 0.80)
            elif area_ratio >= 0.08:
                dominance_score = max(dominance_score, 0.55)
            elif area_ratio >= 0.03:
                dominance_score = max(dominance_score, 0.15)
        features = item.get("identity_features") or []
        feature_score = min(0.60, 0.15 * len(features)) if isinstance(features, list) else 0.0
        completeness = 0.15 if item.get("object_completeness") == "complete" else 0.0
        strength = max(strength, dominance_score + feature_score + completeness)
    return min(1.0, strength)


def weak_same_product_identity_evidence(decision: JsonDict, profiles: dict[str, JsonDict] | None) -> bool:
    if decision.get("decision") != "same_product" or not profiles:
        return False
    query_strength = profile_identity_strength(profiles.get(str(decision.get("query_image_id"))))
    candidate_strength = profile_identity_strength(profiles.get(str(decision.get("candidate_id"))))
    return min(query_strength, candidate_strength) < 0.50


def strong_same_product_identity_evidence(decision: JsonDict, profiles: dict[str, JsonDict] | None) -> bool:
    if decision.get("decision") != "same_product" or not profiles:
        return False
    query_strength = profile_identity_strength(profiles.get(str(decision.get("query_image_id"))))
    candidate_strength = profile_identity_strength(profiles.get(str(decision.get("candidate_id"))))
    return min(query_strength, candidate_strength) >= 0.65


def auto_accept_thresholds(
    decision: JsonDict,
    candidate: JsonDict,
    profiles: dict[str, JsonDict] | None,
    min_confidence: float,
    min_score: float,
    min_margin: float,
) -> tuple[float, float, float]:
    if not strong_same_product_identity_evidence(decision, profiles):
        return min_confidence, min_score, min_margin
    confidence = json_float(decision.get("confidence"), 0.0)
    margin = json_float(candidate.get("score_margin", decision.get("score_margin")), 0.0)
    if confidence >= 0.95 and margin >= max(min_margin, 0.04):
        return min_confidence, min(min_score, 0.78), max(min_margin, 0.04)
    return min_confidence, min_score, min_margin


def demotable_same_product(
    decision: JsonDict,
    candidate: JsonDict,
    profiles: dict[str, JsonDict] | None,
    min_confidence: float,
    min_score: float,
) -> bool:
    return (
        weak_same_product_identity_evidence(decision, profiles)
        and json_float(decision.get("confidence"), 0.0) >= min_confidence
        and json_float(candidate.get("score", decision.get("retrieval_score")), 0.0) >= min_score
        and not decision.get("review_required")
    )


def isolated_positive_auto_accept(
    decision: JsonDict,
    candidate: JsonDict,
    rows: list[tuple[JsonDict, JsonDict]],
    min_confidence: float,
) -> bool:
    if decision.get("review_required") or decision.get("decision") not in {"same_product", "same_design_variant"}:
        return False
    positives = [row for row in rows if row[0].get("decision") in {"same_product", "same_design_variant", "unsure"} and not row[0].get("review_required")]
    if len(positives) != 1:
        return False
    score = json_float(candidate.get("score", decision.get("retrieval_score")), 0.0)
    confidence = json_float(decision.get("confidence"), 0.0)
    if decision.get("decision") == "same_product":
        return confidence >= max(min_confidence, 0.95) and score >= 0.88
    return confidence >= 0.85 and score >= 0.88


def context_auto_accepted_match(
    decision: JsonDict,
    candidate: JsonDict,
    rows: list[tuple[JsonDict, JsonDict]],
    profiles: dict[str, JsonDict] | None,
    min_confidence: float,
    min_score: float,
    min_margin: float,
) -> bool:
    return auto_accepted_match(decision, candidate, profiles, min_confidence, min_score, min_margin) or isolated_positive_auto_accept(
        decision,
        candidate,
        rows,
        min_confidence,
    )


def human_review_queue(
    decisions: dict[str, JsonDict],
    candidates: list[JsonDict],
    sample_rate: float,
    profiles: dict[str, JsonDict] | None = None,
    min_confidence: float = 0.90,
    min_score: float = 0.90,
    min_margin: float = 0.03,
) -> list[JsonDict]:
    candidate_lookup = {candidate_pair_key_for_retrieval(candidate): candidate for candidate in candidates}
    by_query: dict[str, list[tuple[JsonDict, JsonDict]]] = defaultdict(list)
    for decision in decisions.values():
        candidate = candidate_lookup.get(decision_pair_key(decision), {})
        by_query[str(decision.get("query_image_id"))].append((decision, candidate))

    queue = []
    for query_image_id, query_rows in sorted(by_query.items()):
        rows = sorted(query_rows, key=lambda item: json_float(item[1].get("score", item[0].get("retrieval_score")), 0.0), reverse=True)
        review_flags: set[str] = set()
        review_candidates: list[str] = []
        accepted = [
            (decision, candidate)
            for decision, candidate in rows
            if context_auto_accepted_match(decision, candidate, rows, profiles, min_confidence, min_score, min_margin)
        ]
        unresolved_positive = [
            (decision, candidate)
            for decision, candidate in rows
            if decision.get("decision") in {"same_product", "same_design_variant", "unsure"}
            and not context_auto_accepted_match(decision, candidate, rows, profiles, min_confidence, min_score, min_margin)
            and not demotable_same_product(decision, candidate, profiles, min_confidence, min_score)
        ]
        for decision, candidate in rows:
            flags = {str(flag) for flag in decision.get("review_flags", []) + candidate.get("adjudication_reasons", [])}
            if decision.get("review_required") or decision.get("decision") == "unsure":
                review_flags.update(flags or {"adjudicator_unsure"})
                review_candidates.append(str(decision.get("candidate_id")))
            elif decision.get("decision") in {"same_product", "same_design_variant"} and not context_auto_accepted_match(
                decision,
                candidate,
                rows,
                profiles,
                min_confidence,
                min_score,
                min_margin,
            ) and not demotable_same_product(decision, candidate, profiles, min_confidence, min_score):
                review_flags.update(flags or {"positive_below_auto_accept_band"})
                review_candidates.append(str(decision.get("candidate_id")))
        if len(accepted) > 1:
            best_score = json_float(accepted[0][1].get("score", accepted[0][0].get("retrieval_score")), 0.0)
            close_accepted = [
                str(decision.get("candidate_id"))
                for decision, candidate in accepted[1:]
                if best_score - json_float(candidate.get("score", decision.get("retrieval_score")), 0.0) <= min_margin
            ]
            if close_accepted:
                review_flags.add("multiple_close_auto_matches")
                review_candidates.extend(close_accepted)
        if unresolved_positive:
            review_flags.add("positive_below_auto_accept_band")
        if review_flags:
            candidate_ids = sorted(set(review_candidates)) or [str(rows[0][0].get("candidate_id"))]
            queue.append(
                {
                    "query_image_id": query_image_id,
                    "candidate_id": candidate_ids[0],
                    "candidate_ids": candidate_ids,
                    "review_flags": sorted(review_flags),
                    "sampled_auto_pass": False,
                }
            )
            continue
        digest = int(stable_name_digest(query_image_id)[:8], 16) / 0xFFFFFFFF
        if sample_rate > 0 and rows and digest < sample_rate:
            queue.append(
                {
                    "query_image_id": query_image_id,
                    "candidate_id": str(rows[0][0].get("candidate_id")),
                    "candidate_ids": [str(rows[0][0].get("candidate_id"))],
                    "review_flags": ["random_qa_sample"],
                    "sampled_auto_pass": True,
                }
            )
    return queue


def final_matches_from_decisions(
    decisions: dict[str, JsonDict],
    candidates: list[JsonDict] | None = None,
    profiles: dict[str, JsonDict] | None = None,
    min_confidence: float = 0.90,
    min_score: float = 0.90,
    min_margin: float = 0.03,
) -> list[JsonDict]:
    candidate_lookup = {candidate_pair_key_for_retrieval(candidate): candidate for candidate in candidates or []}
    by_query: dict[str, list[tuple[JsonDict, JsonDict]]] = defaultdict(list)
    for decision in decisions.values():
        candidate = candidate_lookup.get(decision_pair_key(decision), {})
        by_query[str(decision.get("query_image_id"))].append((decision, candidate))
    rows = []
    seen_pairs: set[tuple[str, str]] = set()
    for decision in decisions.values():
        candidate = candidate_lookup.get(decision_pair_key(decision), {})
        query_rows = by_query.get(str(decision.get("query_image_id")), [(decision, candidate)])
        accepted = context_auto_accepted_match(decision, candidate, query_rows, profiles, min_confidence, min_score, min_margin)
        demoted = demotable_same_product(decision, candidate, profiles, min_confidence, min_score)
        if not accepted and not demoted:
            continue
        pair_key = unordered_image_pair_key(decision["query_image_id"], decision["candidate_id"])
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        output_decision = "same_design_variant" if demoted else decision["decision"]
        rows.append(
            {
                "query_image_id": decision["query_image_id"],
                "candidate_id": decision["candidate_id"],
                "decision": output_decision,
                "raw_decision": decision["decision"],
                "demoted_from_same_product": demoted,
                "confidence": decision["confidence"],
                "retrieval_score": candidate.get("score", decision.get("retrieval_score")),
                "score_margin": candidate.get("score_margin", decision.get("score_margin")),
            }
        )
    return sorted(rows, key=lambda item: (str(item["query_image_id"]), str(item["candidate_id"])))


def write_human_review_html(path: Path, queue: list[JsonDict]) -> None:
    rows = [
        "<tr>"
        f"<td>{html.escape(str(item['query_image_id']))}</td>"
        f"<td>{html.escape(', '.join(str(candidate_id) for candidate_id in item.get('candidate_ids', [item.get('candidate_id')])) )}</td>"
        f"<td>{html.escape(', '.join(item.get('review_flags') or []))}</td>"
        "</tr>"
        for item in queue
    ]
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Human Review Queue</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left}</style></head>"
        "<body><h1>Human Review Queue</h1><table><thead><tr><th>Query</th><th>Candidate</th><th>Flags</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table></body></html>",
        encoding="utf-8",
    )


def registry_asset_id(record: JsonDict) -> str:
    match = re.match(r"(CA\d+)-", str(record.get("filename", "")))
    if match:
        return match.group(1)
    return str(record.get("image_id", ""))


def load_registry_asset_map(path: Path) -> dict[str, str]:
    return {str(record["image_id"]): registry_asset_id(record) for record in load_image_manifest(path)}


def load_final_product_label_map(path: Path) -> dict[str, set[str]]:
    labels: dict[str, set[str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "asset_id" not in (reader.fieldnames or []) or "final_product_ids" not in (reader.fieldnames or []):
            msg = "benchmark labels must include asset_id and final_product_ids"
            raise ValueError(msg)
        for row in reader:
            labels[str(row["asset_id"])] = set(split_manifest_list(row.get("final_product_ids", "")))
    return labels


def benchmark_match_outputs(
    registry_asset_map: dict[str, str],
    label_map: dict[str, set[str]],
    matches: list[JsonDict],
    review_queue: list[JsonDict],
    adjudication_count: int | None = None,
) -> JsonDict:
    same_product_rows = [match for match in matches if match.get("decision") == "same_product"]
    true_same = []
    false_same = []
    for match in same_product_rows:
        query_asset_id = registry_asset_map.get(str(match["query_image_id"]), "")
        candidate_asset_id = registry_asset_map.get(str(match["candidate_id"]), "")
        query_labels = label_map.get(query_asset_id, set())
        candidate_labels = label_map.get(candidate_asset_id, set())
        row = {
            **match,
            "query_asset_id": query_asset_id,
            "candidate_asset_id": candidate_asset_id,
            "query_final_product_ids": sorted(query_labels),
            "candidate_final_product_ids": sorted(candidate_labels),
        }
        if query_labels and candidate_labels and query_labels.intersection(candidate_labels):
            true_same.append(row)
        else:
            false_same.append(row)
    image_count = len(registry_asset_map)
    review_images = {str(item["query_image_id"]) for item in review_queue}
    review_image_count = len(review_images)
    naive_pairwise_candidate_count = image_count * (image_count - 1) // 2
    human_review_reduction_factor = (image_count / review_image_count) if review_image_count else float("inf")
    adjudication_reduction_factor = None
    if adjudication_count is not None:
        adjudication_reduction_factor = (naive_pairwise_candidate_count / adjudication_count) if adjudication_count else float("inf")
    precision = len(true_same) / len(same_product_rows) if same_product_rows else 1.0
    return {
        "image_count": image_count,
        "auto_match_count": len(matches),
        "auto_same_product_count": len(same_product_rows),
        "auto_same_product_true_positive": len(true_same),
        "auto_same_product_false_positive": len(false_same),
        "auto_same_product_precision": precision,
        "human_review_item_count": len(review_queue),
        "human_review_image_count": review_image_count,
        "human_review_image_rate": review_image_count / image_count if image_count else 0.0,
        "human_review_reduction_factor_vs_all_images": human_review_reduction_factor,
        "naive_pairwise_candidate_count": naive_pairwise_candidate_count,
        "ai_adjudicated_pair_count": adjudication_count,
        "ai_adjudication_reduction_factor_vs_pairwise": adjudication_reduction_factor,
        "target_same_product_precision_met": precision >= 0.98,
        "target_review_rate_10pct_met": (review_image_count / image_count if image_count else 0.0) <= 0.10,
        "target_10x_human_review_efficiency_met": human_review_reduction_factor >= 10,
        "target_10x_ai_pairwise_efficiency_met": adjudication_reduction_factor is not None and adjudication_reduction_factor >= 10,
        "false_same_product_matches": false_same,
    }


def match_benchmark_markdown(benchmark: JsonDict) -> str:
    ai_pair_factor = benchmark.get("ai_adjudication_reduction_factor_vs_pairwise")
    lines = [
        "# Multi-View Match Benchmark",
        "",
        f"- Images: {benchmark['image_count']}",
        f"- Auto matches: {benchmark['auto_match_count']}",
        f"- Auto same-product matches: {benchmark['auto_same_product_count']}",
        f"- Auto same-product precision: {benchmark['auto_same_product_precision']:.3f}",
        f"- Same-product false positives: {benchmark['auto_same_product_false_positive']}",
        f"- Human review images: {benchmark['human_review_image_count']}",
        f"- Human review image rate: {benchmark['human_review_image_rate']:.3f}",
        f"- Human review reduction vs all-image review: {float(benchmark['human_review_reduction_factor_vs_all_images']):.1f}x",
        f"- Naive pairwise candidate count: {benchmark['naive_pairwise_candidate_count']}",
        f"- AI adjudicated retrieval pairs: {benchmark.get('ai_adjudicated_pair_count')}",
        f"- AI adjudication reduction vs naive pairwise: {float(ai_pair_factor):.1f}x" if ai_pair_factor is not None else "- AI adjudication reduction vs naive pairwise: n/a",
        f"- Precision target met: {benchmark['target_same_product_precision_met']}",
        f"- 10% review target met: {benchmark['target_review_rate_10pct_met']}",
        f"- 10x human-review efficiency target met: {benchmark['target_10x_human_review_efficiency_met']}",
        f"- 10x AI pairwise-efficiency target met: {benchmark['target_10x_ai_pairwise_efficiency_met']}",
        "",
        "## False Same-Product Matches",
        "",
    ]
    for row in benchmark["false_same_product_matches"]:
        lines.append(
            "- "
            f"{row['query_asset_id']} {row['query_final_product_ids']} -> "
            f"{row['candidate_asset_id']} {row['candidate_final_product_ids']} "
            f"score={float(row.get('retrieval_score') or 0):.4f} confidence={float(row.get('confidence') or 0):.2f}"
        )
    if not benchmark["false_same_product_matches"]:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def summarize_match_benchmarks(benchmarks: list[JsonDict]) -> JsonDict:
    image_count = sum(int(benchmark.get("image_count", 0)) for benchmark in benchmarks)
    review_image_count = sum(int(benchmark.get("human_review_image_count", 0)) for benchmark in benchmarks)
    auto_match_count = sum(int(benchmark.get("auto_match_count", 0)) for benchmark in benchmarks)
    auto_same_product_count = sum(int(benchmark.get("auto_same_product_count", 0)) for benchmark in benchmarks)
    auto_same_product_true_positive = sum(int(benchmark.get("auto_same_product_true_positive", 0)) for benchmark in benchmarks)
    auto_same_product_false_positive = sum(int(benchmark.get("auto_same_product_false_positive", 0)) for benchmark in benchmarks)
    naive_pairwise_candidate_count = sum(int(benchmark.get("naive_pairwise_candidate_count", 0)) for benchmark in benchmarks)
    adjudication_counts = [int(benchmark["ai_adjudicated_pair_count"]) for benchmark in benchmarks if benchmark.get("ai_adjudicated_pair_count") is not None]
    ai_adjudicated_pair_count = sum(adjudication_counts) if len(adjudication_counts) == len(benchmarks) else None
    precision = auto_same_product_true_positive / auto_same_product_count if auto_same_product_count else 1.0
    review_rate = review_image_count / image_count if image_count else 0.0
    human_review_reduction_factor = image_count / review_image_count if review_image_count else float("inf")
    ai_adjudication_reduction_factor = None
    if ai_adjudicated_pair_count is not None:
        ai_adjudication_reduction_factor = naive_pairwise_candidate_count / ai_adjudicated_pair_count if ai_adjudicated_pair_count else float("inf")
    return {
        "benchmark_run_count": len(benchmarks),
        "image_count": image_count,
        "auto_match_count": auto_match_count,
        "auto_same_product_count": auto_same_product_count,
        "auto_same_product_true_positive": auto_same_product_true_positive,
        "auto_same_product_false_positive": auto_same_product_false_positive,
        "auto_same_product_precision": precision,
        "human_review_image_count": review_image_count,
        "human_review_image_rate": review_rate,
        "human_review_reduction_factor_vs_all_images": human_review_reduction_factor,
        "naive_pairwise_candidate_count": naive_pairwise_candidate_count,
        "ai_adjudicated_pair_count": ai_adjudicated_pair_count,
        "ai_adjudication_reduction_factor_vs_pairwise": ai_adjudication_reduction_factor,
        "target_same_product_precision_met": precision >= 0.98,
        "target_review_rate_10pct_met": review_rate <= 0.10,
        "target_10x_human_review_efficiency_met": human_review_reduction_factor >= 10,
        "target_10x_ai_pairwise_efficiency_met": ai_adjudication_reduction_factor is not None and ai_adjudication_reduction_factor >= 10,
    }


def match_benchmark_summary_markdown(summary: JsonDict) -> str:
    ai_pair_factor = summary.get("ai_adjudication_reduction_factor_vs_pairwise")
    lines = [
        "# Multi-View Match Benchmark Summary",
        "",
        f"- Benchmark runs: {summary['benchmark_run_count']}",
        f"- Images: {summary['image_count']}",
        f"- Auto matches: {summary['auto_match_count']}",
        f"- Auto same-product matches: {summary['auto_same_product_count']}",
        f"- Auto same-product precision: {summary['auto_same_product_precision']:.3f}",
        f"- Same-product false positives: {summary['auto_same_product_false_positive']}",
        f"- Human review images: {summary['human_review_image_count']}",
        f"- Human review image rate: {summary['human_review_image_rate']:.3f}",
        f"- Human review reduction vs all-image review: {float(summary['human_review_reduction_factor_vs_all_images']):.1f}x",
        f"- Naive pairwise candidate count: {summary['naive_pairwise_candidate_count']}",
        f"- AI adjudicated retrieval pairs: {summary.get('ai_adjudicated_pair_count')}",
        f"- AI adjudication reduction vs naive pairwise: {float(ai_pair_factor):.1f}x" if ai_pair_factor is not None else "- AI adjudication reduction vs naive pairwise: n/a",
        f"- Precision target met: {summary['target_same_product_precision_met']}",
        f"- 10% review target met: {summary['target_review_rate_10pct_met']}",
        f"- 10x human-review efficiency target met: {summary['target_10x_human_review_efficiency_met']}",
        f"- 10x AI pairwise-efficiency target met: {summary['target_10x_ai_pairwise_efficiency_met']}",
    ]
    return "\n".join(lines) + "\n"


def benchmark_matches_command(args: argparse.Namespace) -> int:
    registry_asset_map = load_registry_asset_map(Path(args.registry).resolve())
    label_map = load_final_product_label_map(Path(args.labels).resolve())
    matches = cast("list[JsonDict]", json.loads(Path(args.matches).read_text(encoding="utf-8")))
    review_queue = cast("list[JsonDict]", json.loads(Path(args.review_queue).read_text(encoding="utf-8")))
    adjudication_count = None
    if args.adjudications:
        adjudications = cast("dict[str, JsonDict] | list[JsonDict]", json.loads(Path(args.adjudications).read_text(encoding="utf-8")))
        adjudication_count = len(adjudications)
    benchmark = benchmark_match_outputs(registry_asset_map, label_map, matches, review_queue, adjudication_count=adjudication_count)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "match_benchmark.json", benchmark)
    (out_dir / "match_benchmark.md").write_text(match_benchmark_markdown(benchmark), encoding="utf-8")
    print(f"Auto same-product precision: {benchmark['auto_same_product_precision']:.3f}")
    print(f"Human review image rate: {benchmark['human_review_image_rate']:.3f}")
    print(f"Wrote: {out_dir}")
    return 0


def benchmark_summary_command(args: argparse.Namespace) -> int:
    benchmarks = [cast("JsonDict", json.loads(Path(path).read_text(encoding="utf-8"))) for path in args.benchmarks]
    summary = summarize_match_benchmarks(benchmarks)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "match_benchmark_summary.json", summary)
    (out_dir / "match_benchmark_summary.md").write_text(match_benchmark_summary_markdown(summary), encoding="utf-8")
    print(f"Auto same-product precision: {summary['auto_same_product_precision']:.3f}")
    print(f"Human review image rate: {summary['human_review_image_rate']:.3f}")
    print(f"Wrote: {out_dir}")
    return 0


def adjudicate_retrieval_command(args: argparse.Namespace) -> int:
    load_dotenv()
    profiles = load_profiles(Path(args.profiles).resolve())
    views = load_evidence_views(Path(args.evidence).resolve())
    views_by_id = {str(view["view_id"]): view for view in views}
    candidates = cast("list[JsonDict]", json.loads(Path(args.retrieval).read_text(encoding="utf-8")))
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "retrieval_adjudication_cache.json"
    cache = load_ai_decision_cache(cache_path)
    queue = adjudication_queue(candidates, profiles, args.low_margin)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not args.from_cache and not api_key:
        print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    decisions: dict[str, JsonDict] = {}
    with tempfile.TemporaryDirectory(prefix="jewelry-retrieval-judge-") as tmp:
        tmpdir = Path(tmp)
        for index, candidate in enumerate(queue, start=1):
            strongest = candidate.get("strongest_match") or {}
            if not strongest:
                continue
            key = retrieval_adjudication_cache_key(candidate, views_by_id, args.model, args.max_image_size)
            if key not in cache:
                if args.from_cache:
                    continue
                print(f"Adjudicating {index}/{len(queue)} {candidate['query_image_id']} -> {candidate['candidate_image_id']}", flush=True)
                decision = call_openai_retrieval_adjudicator(
                    api_key,
                    args.model,
                    candidate,
                    views_by_id,
                    profiles,
                    tmpdir,
                    args.max_image_size,
                    args.timeout,
                )
                cache[key] = {
                    "model": args.model,
                    "prompt_version": RETRIEVAL_ADJUDICATION_PROMPT_VERSION,
                    "query_image_id": candidate["query_image_id"],
                    "candidate_id": candidate["candidate_image_id"],
                    "decision": decision,
                }
                write_json(cache_path, cache)
            decision = {
                **cast("JsonDict", cache[key]["decision"]),
                "query_image_id": candidate["query_image_id"],
                "candidate_id": candidate["candidate_image_id"],
                "retrieval_score": candidate["score"],
                "score_margin": candidate["score_margin"],
                "adjudication_reasons": candidate["adjudication_reasons"],
            }
            decisions[key] = decision
    review_queue = human_review_queue(
        decisions,
        queue,
        args.qa_sample_rate,
        profiles=profiles,
        min_confidence=args.auto_accept_confidence,
        min_score=args.auto_accept_score,
        min_margin=args.auto_accept_margin,
    )
    final_matches = final_matches_from_decisions(
        decisions,
        queue,
        profiles=profiles,
        min_confidence=args.auto_accept_confidence,
        min_score=args.auto_accept_score,
        min_margin=args.auto_accept_margin,
    )
    write_json(cache_path, cache)
    write_json(out_dir / "retrieval_adjudication_queue.json", queue)
    write_json(out_dir / "retrieval_adjudications.json", decisions)
    write_json(out_dir / "human_review_queue.json", review_queue)
    write_human_review_html(out_dir / "human_review.html", review_queue)
    write_json(out_dir / "final_matches.json", final_matches)
    print(f"Adjudication queue: {len(queue)}")
    print(f"Decisions: {len(decisions)}")
    print(f"Human review queue: {len(review_queue)}")
    print(f"Wrote: {out_dir}")
    return 0


def crop_review_record(asset_id: str, flags: list[str], best_crop_id: str) -> JsonDict:
    usable = [
        f"{asset_id}_full_image",
        f"{asset_id}_foreground_product",
        f"{asset_id}_owlv2_base",
        f"{asset_id}_owlv2_padded",
        f"{asset_id}_owlv2_context",
    ]
    return {
        "verdict": "pass_good",
        "best_crop_id": best_crop_id,
        "usable_crop_ids": usable,
        "notes": "",
        "auto_review_required": bool(flags),
        "auto_review_flags": flags,
        "auto_label_reason": "default OWLv2 padded crop selected" if best_crop_id.endswith("_owlv2_padded") else "full image selected as fallback",
    }


def build_crop_review_queue(auto_labels: dict[str, JsonDict], sample_rate: float) -> list[JsonDict]:
    queue = []
    for asset_id, record in sorted(auto_labels.items()):
        flags = list(record.get("auto_review_flags") or [])
        if record.get("auto_review_required"):
            queue.append({"asset_id": asset_id, "review_flags": flags, "sampled_auto_pass": False})
            continue
        digest = int(stable_name_digest(asset_id)[:8], 16) / 0xFFFFFFFF
        if sample_rate > 0 and digest < sample_rate:
            queue.append({"asset_id": asset_id, "review_flags": ["random_auto_pass_sample"], "sampled_auto_pass": True})
    return queue


def crop_localization_summary(
    assets: list[JsonDict],
    auto_labels: dict[str, JsonDict],
    review_queue: list[JsonDict],
    final_labels: dict[str, JsonDict],
) -> JsonDict:
    labels = {**auto_labels, **final_labels}
    verdict_counts = Counter(str(record.get("verdict", "")) for record in labels.values())
    best_counts: Counter[str] = Counter()
    for asset_id, record in labels.items():
        best_counts[candidate_kind(str(record.get("best_crop_id", "")), asset_id)] += 1
    pass_good = verdict_counts["pass_good"]
    pass_usable = verdict_counts["pass_usable"]
    total_assets = len(assets)
    return {
        "total_assets": total_assets,
        "auto_pass_count": sum(1 for record in auto_labels.values() if not record.get("auto_review_required")),
        "review_queue_count": len(review_queue),
        "reviewed_count": len(final_labels),
        "pass_good": pass_good,
        "pass_usable": pass_usable,
        "fail_missed_jewelry": verdict_counts["fail_missed_jewelry"],
        "fail_wrong_location": verdict_counts["fail_wrong_location"] + verdict_counts["fail_wrong_object"],
        "fail_too_tight": verdict_counts["fail_too_tight"],
        "fail_too_wide": verdict_counts["fail_too_wide"],
        "best_candidate_distribution": dict(best_counts),
        "failure_assets": sorted(asset_id for asset_id, record in labels.items() if str(record.get("verdict", "")).startswith("fail_")),
        "usable_localization_rate": (pass_good + pass_usable) / total_assets if total_assets else 0.0,
        "manual_review_rate": len(review_queue) / total_assets if total_assets else 0.0,
    }


def crop_localization_summary_markdown(summary: JsonDict) -> str:
    lines = [
        "# Crop Localization Summary",
        "",
        f"- Total assets: {summary['total_assets']}",
        f"- Auto-pass count: {summary['auto_pass_count']}",
        f"- Review queue count: {summary['review_queue_count']}",
        f"- Reviewed count: {summary['reviewed_count']}",
        f"- Usable localization rate: {summary['usable_localization_rate']:.3f}",
        f"- Manual review rate: {summary['manual_review_rate']:.3f}",
        "",
        "## Verdicts",
        "",
        f"- pass_good: {summary['pass_good']}",
        f"- pass_usable: {summary['pass_usable']}",
        f"- fail_missed_jewelry: {summary['fail_missed_jewelry']}",
        f"- fail_wrong_location: {summary['fail_wrong_location']}",
        f"- fail_too_tight: {summary['fail_too_tight']}",
        f"- fail_too_wide: {summary['fail_too_wide']}",
        "",
        "## Best Candidate Distribution",
        "",
    ]
    for key, count in sorted(cast("dict[str, int]", summary["best_candidate_distribution"]).items()):
        lines.append(f"- {key or '<none>'}: {count}")
    lines.extend(["", "## Failure Assets", ""])
    failures = cast("list[str]", summary["failure_assets"])
    lines.extend([f"- {asset_id}" for asset_id in failures] or ["- None"])
    return "\n".join(lines) + "\n"


def add_crop_candidate(
    all_candidates: list[JsonDict],
    source: Path,
    out_dir: Path,
    asset_id: str,
    crop_id: str,
    box: tuple[int, int, int, int],
    source_box: list[int],
    scale_name: str,
    rank: int,
    item: JsonDict,
) -> bool:
    crop_path = out_dir / "crops" / f"{crop_id}.jpg"
    if not crop_image(source, crop_path, box):
        return False
    all_candidates.append(
        {
            "asset_id": asset_id,
            "crop_id": crop_id,
            "type": item.get("type", "ring"),
            "label": item.get("label", item.get("type", "ring")),
            "box": list(box),
            "source_box": source_box,
            "scale": scale_name,
            "rank": rank,
            "confidence": float(item.get("confidence") or item.get("score") or 0.0),
            "score": float(item.get("score") or item.get("confidence") or 0.0),
            "visibility": item.get("visibility", ""),
            "notes": item.get("notes", ""),
            "crop_path": str(crop_path),
        }
    )
    return True


def candidate_kind(crop_id: str, asset_id: str) -> str:
    return crop_id.removeprefix(f"{asset_id}_")


def write_crop_probe_review(
    out_dir: Path,
    assets: list[JsonDict],
    candidates: list[JsonDict],
    auto_labels: dict[str, JsonDict] | None = None,
    review_queue: list[JsonDict] | None = None,
) -> None:
    originals_dir = out_dir / "review_originals"
    originals_dir.mkdir(parents=True, exist_ok=True)
    by_asset: dict[str, list[JsonDict]] = defaultdict(list)
    for candidate in candidates:
        by_asset[str(candidate["asset_id"])].append(candidate)
    auto_labels = auto_labels or {}
    queue_by_asset = {str(item["asset_id"]): item for item in review_queue or []}
    sections = []
    for asset in assets:
        asset_id = str(asset["asset_id"])
        source = Path(str(asset["preferred_path"]))
        width, height = image_size(source)
        width = width or 1
        height = height or 1
        original_review_image = originals_dir / f"{asset_id}-{stable_name_digest(str(source))}.jpg"
        if not original_review_image.exists() and not resize_image_to_jpeg(source, original_review_image, 1200):
            original_review_image = source
        display_width = 560
        scale = display_width / width
        boxes = []
        crop_figures = []
        for candidate in by_asset.get(asset_id, []):
            x, y, w, h = candidate["box"]
            left = x * scale
            top = y * scale
            box_width = w * scale
            box_height = h * scale
            label = f"{candidate['rank']}. {candidate['type']} {candidate['scale']} {candidate['confidence']:.2f}"
            boxes.append(
                "<div class='box' style='"
                f"left:{left:.2f}px;top:{top:.2f}px;width:{box_width:.2f}px;height:{box_height:.2f}px"
                f"'><span>{html.escape(label)}</span></div>"
            )
            crop_path = Path(str(candidate["crop_path"]))
            crop_id = html.escape(str(candidate["crop_id"]))
            crop_figures.append(
                f"<figure data-crop-id='{crop_id}'>"
                f"<img src='{html.escape(os.path.relpath(crop_path, out_dir))}' alt=''>"
                f"<figcaption>{html.escape(label)}<br>{html.escape(str(candidate.get('notes', '')))}</figcaption>"
                "<div class='crop-actions'>"
                f"<button type='button' data-action='best' data-crop-id='{crop_id}'>Best</button>"
                f"<button type='button' data-action='usable' data-crop-id='{crop_id}'>Usable</button>"
                "</div>"
                "</figure>"
            )
        queue_item = queue_by_asset.get(asset_id, {})
        flags = queue_item.get("review_flags") or auto_labels.get(asset_id, {}).get("auto_review_flags") or []
        flag_text = ", ".join(str(flag) for flag in flags) or "none"
        sections.append(
            f"<section data-asset-id='{html.escape(asset_id)}'>"
            f"<h2>{html.escape(asset_id)} "
            f"<span>{html.escape(str(asset.get('final_product_ids', '')))} / {html.escape(str(asset.get('image_roles', '')))}</span></h2>"
            f"<p class='flags'><b>Reason flags:</b> {html.escape(flag_text)}</p>"
            "<div class='asset-actions'>"
            "<label>Verdict "
            f"<select data-field='verdict' data-asset-id='{html.escape(asset_id)}'>"
            "<option value=''>Unreviewed</option>"
            "<option value='pass_good'>Pass good</option>"
            "<option value='pass_usable'>Pass usable</option>"
            "<option value='fail_missed_jewelry'>Fail missed jewelry</option>"
            "<option value='fail_wrong_object'>Fail wrong object</option>"
            "<option value='fail_too_wide'>Fail too wide</option>"
            "<option value='fail_too_tight'>Fail too tight</option>"
            "<option value='fail_multi_product_unseparated'>Fail multi-product unseparated</option>"
            "<option value='fail_low_visibility'>Fail low visibility</option>"
            "</select></label>"
            "<label> Notes "
            f"<input data-field='notes' data-asset-id='{html.escape(asset_id)}' placeholder='short note'>"
            "</label>"
            "</div>"
            "<div class='layout'>"
            "<div class='original-panel'><h3>Original + Boxes</h3><div class='overlay'>"
            f"<img src='{html.escape(os.path.relpath(original_review_image, out_dir))}' style='width:{display_width}px' alt=''>"
            + "".join(boxes)
            + "</div></div>"
            "<div class='crop-panel'><h3>Crop Candidates</h3><div class='crops'>"
            + "".join(crop_figures)
            + "</div></div></div>"
            "</section>"
        )
    (out_dir / "crop_review.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Jewelry Crop Probe</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        ".toolbar{position:sticky;top:0;z-index:10;background:#111827;color:white;padding:10px 12px;margin:-24px -24px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}"
        ".toolbar button{background:#f9fafb;color:#111827;border:0;border-radius:4px;padding:7px 10px;font-weight:600}.toolbar span{font-size:13px;color:#d1d5db}"
        "h1{font-size:24px}section{background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 22px;padding:14px}"
        "h2{font-size:18px;margin:0 0 12px}h2 span{font-size:12px;color:#667085;font-weight:400}"
        ".asset-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0 0 12px}.asset-actions label{font-size:13px}.asset-actions select,.asset-actions input{font:inherit;padding:5px;border:1px solid #cbd5e1;border-radius:4px}.asset-actions input{width:360px;max-width:100%}"
        ".layout{display:grid;grid-template-columns:minmax(320px,560px) minmax(360px,1fr);gap:18px;align-items:start}.original-panel,.crop-panel{min-width:0}.original-panel h3,.crop-panel h3{font-size:13px;margin:0 0 8px;color:#475569}.overlay{position:relative;background:#eee;width:560px;max-width:100%;overflow:hidden}"
        ".overlay img{display:block;max-width:100%;height:auto}.box{position:absolute;border:2px solid #e11d48;box-sizing:border-box;pointer-events:none}.box span{position:absolute;left:0;top:0;background:#e11d48;color:white;font-size:11px;padding:2px 4px;white-space:nowrap}"
        ".crops{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;max-width:780px}figure{margin:0;border:1px solid #e5e7eb;background:#fafafa;padding:6px}"
        "figure.best{border-color:#059669;box-shadow:0 0 0 2px #059669 inset}figure.usable{background:#ecfdf5}.crop-actions{display:flex;gap:6px;margin-top:6px}.crop-actions button{font-size:11px;border:1px solid #cbd5e1;border-radius:4px;background:white;padding:4px 6px}.crop-actions button.active{background:#059669;color:white;border-color:#059669}"
        "figure img{width:100%;height:170px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "@media (max-width: 980px){.layout{grid-template-columns:1fr}.overlay{width:100%}}"
        "</style></head><body>"
        "<div class='toolbar'>"
        "<button type='button' id='export-json'>Export JSON</button>"
        "<button type='button' id='export-csv'>Export CSV</button>"
        "<button type='button' id='save-repo'>Save to repo</button>"
        "<button type='button' id='clear-review'>Clear</button>"
        "<span id='summary'>0 reviewed</span>"
        "</div>"
        "<h1>Jewelry Crop Probe</h1>"
        f"<p><b>Needs review:</b> {len(review_queue or [])} "
        f"<b>Auto-pass:</b> {sum(1 for record in auto_labels.values() if not record.get('auto_review_required'))}</p>"
        + "\n".join(sections)
        + """
<script>
const storageKey = "jewelry-crop-review:" + location.pathname;
let state = JSON.parse(localStorage.getItem(storageKey) || "{}");

async function loadExistingLabels() {
  try {
    for (const name of ["crop_review_labels_auto.json", "crop_review_labels.json"]) {
      const response = await fetch(name, {cache: "no-store"});
      if (!response.ok) continue;
      const serverState = await response.json();
      state = {...state, ...serverState};
    }
    localStorage.setItem(storageKey, JSON.stringify(state));
  } catch (error) {
    // Existing labels are optional.
  }
}

function ensureAsset(assetId) {
  state[assetId] ||= { verdict: "", notes: "", best_crop_id: "", usable_crop_ids: [] };
  return state[assetId];
}

function assetForCrop(cropId) {
  const figure = document.querySelector(`figure[data-crop-id="${CSS.escape(cropId)}"]`);
  return figure?.closest("section")?.dataset.assetId || "";
}

function save() {
  localStorage.setItem(storageKey, JSON.stringify(state));
  render();
}

function render() {
  document.querySelectorAll("section[data-asset-id]").forEach(section => {
    const assetId = section.dataset.assetId;
    const record = ensureAsset(assetId);
    const verdict = section.querySelector("[data-field='verdict']");
    const notes = section.querySelector("[data-field='notes']");
    verdict.value = record.verdict || "";
    notes.value = record.notes || "";
    section.querySelectorAll("figure[data-crop-id]").forEach(figure => {
      const cropId = figure.dataset.cropId;
      const isBest = record.best_crop_id === cropId;
      const isUsable = (record.usable_crop_ids || []).includes(cropId);
      figure.classList.toggle("best", isBest);
      figure.classList.toggle("usable", isUsable);
      figure.querySelector("[data-action='best']").classList.toggle("active", isBest);
      figure.querySelector("[data-action='usable']").classList.toggle("active", isUsable);
    });
  });
  const reviewed = Object.values(state).filter(record => record.verdict || record.best_crop_id || record.usable_crop_ids?.length).length;
  document.getElementById("summary").textContent = `${reviewed} reviewed`;
}

function download(name, text, type) {
  const blob = new Blob([text], {type});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

function csvEscape(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

document.addEventListener("click", event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const cropId = button.dataset.cropId;
  const assetId = assetForCrop(cropId);
  const record = ensureAsset(assetId);
  if (button.dataset.action === "best") {
    record.best_crop_id = record.best_crop_id === cropId ? "" : cropId;
  } else if (button.dataset.action === "usable") {
    const set = new Set(record.usable_crop_ids || []);
    set.has(cropId) ? set.delete(cropId) : set.add(cropId);
    record.usable_crop_ids = Array.from(set);
  }
  save();
});

document.addEventListener("change", event => {
  const input = event.target.closest("[data-field='verdict']");
  if (!input) return;
  ensureAsset(input.dataset.assetId).verdict = input.value;
  save();
});

document.addEventListener("input", event => {
  const input = event.target.closest("[data-field='notes']");
  if (!input) return;
  ensureAsset(input.dataset.assetId).notes = input.value;
  save();
});

document.getElementById("export-json").addEventListener("click", () => {
  download("crop_review_labels.json", JSON.stringify(state, null, 2), "application/json");
});

document.getElementById("export-csv").addEventListener("click", () => {
  const rows = [["asset_id","verdict","best_crop_id","usable_crop_ids","notes"]];
  for (const [assetId, record] of Object.entries(state)) {
    rows.push([assetId, record.verdict || "", record.best_crop_id || "", (record.usable_crop_ids || []).join("|"), record.notes || ""]);
  }
  download("crop_review_labels.csv", rows.map(row => row.map(csvEscape).join(",")).join("\\n"), "text/csv");
});

document.getElementById("save-repo").addEventListener("click", async () => {
  try {
    const response = await fetch("/save-labels", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(state)
    });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    document.getElementById("summary").textContent = `Saved ${payload.reviewed_count} reviewed`;
  } catch (error) {
    alert("Save failed. Open this page through jewelry-crop-review-server, not directly as a file.\\n\\n" + error);
  }
});

document.getElementById("clear-review").addEventListener("click", () => {
  if (!confirm("Clear saved review labels for this page?")) return;
  state = {};
  localStorage.removeItem(storageKey);
  render();
});

loadExistingLabels().finally(render);
</script>
"""
        + "</body></html>",
        encoding="utf-8",
    )


def jewelry_crop_probe_command(args: argparse.Namespace) -> int:
    load_dotenv()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = selected_lifestyle_manifest_rows(Path(args.manifest).resolve(), args.limit, args.category)
    if not assets:
        print("ERROR: no lifestyle/supporting assets found", file=sys.stderr)
        return 1
    if args.detector == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    all_candidates: list[JsonDict] = []
    detections: list[JsonDict] = []
    raw_owlv2_detections: list[JsonDict] = []
    auto_labels: dict[str, JsonDict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for asset_index, asset in enumerate(assets, start=1):
            asset_id = str(asset["asset_id"])
            source = Path(str(asset["preferred_path"]))
            width, height = image_size(source)
            if not width or not height:
                print(f"Skipping {asset_id}: cannot read image size", file=sys.stderr)
                continue
            print(f"Processing {asset_index}/{len(assets)} {asset_id}", flush=True)
            full_item = {
                "box_id": "FULL",
                "type": "full_image",
                "label": "full_image",
                "box": [0, 0, width, height],
                "score": 1.0,
                "confidence": 1.0,
                "visibility": "",
                "notes": "Full-image fallback",
            }
            add_crop_candidate(
                all_candidates,
                source,
                out_dir,
                asset_id,
                f"{asset_id}_full_image",
                (0, 0, width, height),
                [0, 0, width, height],
                "full_image",
                0,
                full_item,
            )
            if not asset_is_live_shot(asset):
                detections.append(
                    {
                        "asset": asset,
                        "items": [],
                        "prompt_version": "full_image_only_non_live_shot",
                        "flags": ["non_live_shot_full_image_only"],
                    }
                )
                auto_labels[asset_id] = crop_review_record(asset_id, [], f"{asset_id}_full_image")
                continue
            if args.detector == "owlv2":
                detection = call_owlv2_detector(source, args.owlv2_model, args.device, args.owlv2_threshold)
                filtered_items, flags = filter_owlv2_detections(cast("list[JsonDict]", detection["items"]), width, height)
                raw_owlv2_detections.append(
                    {
                        "asset": asset,
                        "items": detection["items"],
                        "filtered_items": filtered_items,
                        "prompt_version": detection["prompt_version"],
                        "model_id": detection["model_id"],
                        "device": detection["device"],
                        "flags": flags,
                    }
                )
                detections.append(
                    {
                        "asset": asset,
                        "items": filtered_items,
                        "prompt_version": detection["prompt_version"],
                        "flags": flags,
                    }
                )
                if filtered_items:
                    selected = filtered_items[0]
                    base_box = cast("tuple[int, int, int, int]", tuple(cast("list[int]", selected["box"])))
                    crop_flags = list(flags)
                    foreground = foreground_product_box(source, width, height)
                    foreground_detail_ratio = 0.0
                    foreground_crop_added = False
                    if foreground:
                        foreground_box = cast("tuple[int, int, int, int]", tuple(cast("list[int]", foreground["box"])))
                        foreground_detail_ratio = float(foreground["box_area_ratio"]) / max(float(selected.get("area_ratio", 0.0)), 0.000001)
                        background_rgb = cast("list[int]", foreground["background_rgb"])
                        product_shot_foreground = (
                            min(background_rgb) >= FOREGROUND_MIN_BACKGROUND_RGB
                            and float(foreground["pixel_area_ratio"]) <= FOREGROUND_MAX_PIXEL_AREA_RATIO
                        )
                        if (
                            product_shot_foreground
                            and foreground_detail_ratio >= FOREGROUND_DETAIL_RATIO
                            and float(foreground["box_area_ratio"]) >= FOREGROUND_MIN_BOX_AREA_RATIO
                        ):
                            crop_flags.append("owlv2_probable_detail_crop")
                            foreground_crop = expand_crop_box(foreground_box, width, height, "padded")
                            foreground_item = {
                                "box_id": "FG",
                                "type": "ring",
                                "label": "foreground_product",
                                "box": list(foreground_box),
                                "score": 1.0,
                                "confidence": 1.0,
                                "visibility": "",
                                "notes": f"Foreground product fallback; foreground/owlv2 area ratio {foreground_detail_ratio:.1f}",
                            }
                            foreground_crop_added = add_crop_candidate(
                                all_candidates,
                                source,
                                out_dir,
                                asset_id,
                                f"{asset_id}_foreground_product",
                                foreground_crop,
                                list(foreground_box),
                                "foreground_product",
                                1,
                                foreground_item,
                            )
                    rank = 1
                    expansions = [
                        ("owlv2_base", base_box),
                        ("owlv2_padded", expand_crop_box(base_box, width, height, "padded")),
                        ("owlv2_context", expand_crop_box(base_box, width, height, "context")),
                    ]
                    for scale_name, expanded in expansions:
                        if scale_name != "owlv2_context" and box_touches_edge(expanded, width, height) and "crop_touches_edge" not in crop_flags:
                            crop_flags.append("crop_touches_edge")
                        if add_crop_candidate(
                            all_candidates,
                            source,
                            out_dir,
                            asset_id,
                            f"{asset_id}_{scale_name}",
                            expanded,
                            cast("list[int]", selected["box"]),
                            scale_name,
                            rank,
                            selected,
                        ):
                            rank += 1
                    selected_area = float(selected.get("area_ratio", 0.0))
                    if foreground_crop_added:
                        best_crop_id = f"{asset_id}_foreground_product"
                    else:
                        best_crop_id = f"{asset_id}_full_image" if selected_area >= 0.08 else f"{asset_id}_owlv2_padded"
                    if best_crop_id.endswith("_full_image"):
                        crop_flags.append("full_image_probably_better")
                    auto_labels[asset_id] = crop_review_record(asset_id, sorted(set(crop_flags)), best_crop_id)
                else:
                    auto_labels[asset_id] = crop_review_record(
                        asset_id,
                        flags,
                        f"{asset_id}_full_image",
                    )
                continue

            api_key = os.environ["OPENAI_API_KEY"]
            image_url = image_data_url_for_api(source, tmpdir, args.max_image_size)
            detection = call_openai_jewelry_box_detector(api_key, args.model, image_url, width, height, args.timeout)
            detections.append(
                {
                    "asset": asset,
                    "items": detection["items"],
                    "prompt_version": detection["prompt_version"],
                }
            )
            rank = 1
            for item in detection["items"][: args.max_boxes]:
                base_box = cast("tuple[int, int, int, int]", tuple(cast("list[int]", item["box"])))
                for scale_name in ("tight", "padded", "square_padded", "context"):
                    expanded = expand_crop_box(base_box, width, height, scale_name)
                    crop_id = f"{asset_id}_{item['box_id']}_{scale_name}"
                    if add_crop_candidate(
                        all_candidates,
                        source,
                        out_dir,
                        asset_id,
                        crop_id,
                        expanded,
                        item["box"],
                        scale_name,
                        rank,
                        item,
                    ):
                        rank += 1
            auto_labels[asset_id] = crop_review_record(asset_id, ["openai_detector_manual_review"], f"{asset_id}_full_image")
    review_queue = build_crop_review_queue(auto_labels, args.auto_pass_sample_rate)
    queue_asset_ids = {str(item["asset_id"]) for item in review_queue}
    review_assets = [asset for asset in assets if str(asset["asset_id"]) in queue_asset_ids]
    final_labels_path = out_dir / "crop_review_labels.json"
    existing_labels_are_auto = False
    raw_existing_labels: object = {}
    if final_labels_path.exists():
        raw_existing_labels = json.loads(final_labels_path.read_text(encoding="utf-8"))
        if isinstance(raw_existing_labels, dict):
            existing_labels_are_auto = all(
                isinstance(record, dict) and "auto_label_reason" in record
                for record in raw_existing_labels.values()
            )
    final_labels = {} if existing_labels_are_auto else normalized_review_labels(raw_existing_labels)
    summary = crop_localization_summary(assets, auto_labels, review_queue, final_labels)
    write_json(out_dir / "selected_assets.json", assets)
    if raw_owlv2_detections:
        write_json(out_dir / "owlv2_raw_detections.json", raw_owlv2_detections)
    write_json(out_dir / "jewelry_box_detections.json", detections)
    write_json(out_dir / "crop_candidates.json", all_candidates)
    write_json(out_dir / "crop_review_labels_auto.json", auto_labels)
    if not final_labels_path.exists() or existing_labels_are_auto:
        write_review_label_files(out_dir, auto_labels)
    write_json(out_dir / "crop_review_queue.json", review_queue)
    write_json(out_dir / "localization_summary.json", summary)
    (out_dir / "localization_summary.md").write_text(crop_localization_summary_markdown(summary), encoding="utf-8")
    write_crop_probe_review(out_dir, review_assets, all_candidates, auto_labels, review_queue)
    print(f"Assets: {len(assets)}")
    print(f"Crop candidates: {len(all_candidates)}")
    print(f"Auto-pass: {summary['auto_pass_count']}")
    print(f"Review queue: {summary['review_queue_count']}")
    print(f"Wrote: {out_dir}")
    print(f"Review: {out_dir / 'crop_review.html'}")
    return 0


def normalized_review_labels(raw: object) -> dict[str, JsonDict]:
    if not isinstance(raw, dict):
        msg = "review labels payload must be a JSON object"
        raise TypeError(msg)
    labels: dict[str, JsonDict] = {}
    for asset_id, record in raw.items():
        if not isinstance(asset_id, str) or not isinstance(record, dict):
            continue
        usable_raw = record.get("usable_crop_ids", [])
        usable = [str(item) for item in usable_raw] if isinstance(usable_raw, list) else []
        labels[asset_id] = {
            "verdict": str(record.get("verdict", "")),
            "best_crop_id": str(record.get("best_crop_id", "")),
            "usable_crop_ids": usable,
            "notes": str(record.get("notes", "")),
        }
    return labels


def write_review_label_files(out_dir: Path, labels: dict[str, JsonDict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "crop_review_labels.json", labels)
    with (out_dir / "crop_review_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asset_id", "verdict", "best_crop_id", "usable_crop_ids", "notes"])
        writer.writeheader()
        for asset_id, record in sorted(labels.items()):
            writer.writerow(
                {
                    "asset_id": asset_id,
                    "verdict": record["verdict"],
                    "best_crop_id": record["best_crop_id"],
                    "usable_crop_ids": "|".join(cast("list[str]", record["usable_crop_ids"])),
                    "notes": record["notes"],
                }
            )


def import_crop_review_labels_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    labels: dict[str, JsonDict] = {}
    with Path(args.csv).resolve().open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            asset_id = row.get("asset_id", "")
            if not asset_id:
                continue
            labels[asset_id] = {
                "verdict": row.get("verdict", ""),
                "best_crop_id": row.get("best_crop_id", ""),
                "usable_crop_ids": split_manifest_list(row.get("usable_crop_ids", "")),
                "notes": row.get("notes", ""),
            }
    write_review_label_files(out_dir, labels)
    print(f"Imported labels: {len(labels)}")
    print(f"Wrote: {out_dir / 'crop_review_labels.json'}")
    print(f"Wrote: {out_dir / 'crop_review_labels.csv'}")
    return 0


class CropReviewRequestHandler(http.server.SimpleHTTPRequestHandler):
    review_dir: Path

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def do_POST(self) -> None:
        if self.path != "/save-labels":
            self.send_error(404, "unknown endpoint")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            labels = normalized_review_labels(json.loads(raw.decode("utf-8")))
            write_review_label_files(self.review_dir, labels)
            reviewed_count = sum(
                1
                for record in labels.values()
                if record["verdict"] or record["best_crop_id"] or cast("list[str]", record["usable_crop_ids"])
            )
            body = json.dumps({"ok": True, "reviewed_count": reviewed_count}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = str(exc).encode("utf-8", errors="replace")
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def review_server_available_port(host: str, requested_port: int) -> int:
    if requested_port:
        return requested_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def crop_review_server_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    review_page = out_dir / "crop_review.html"
    if not review_page.exists():
        print(f"ERROR: crop review page does not exist: {review_page}", file=sys.stderr)
        return 2
    host = str(args.host)
    port = review_server_available_port(host, int(args.port))
    CropReviewRequestHandler.review_dir = out_dir
    server = http.server.ThreadingHTTPServer(
        (host, port),
        lambda *handler_args, **handler_kwargs: CropReviewRequestHandler(
            *handler_args,
            directory=str(out_dir),
            **handler_kwargs,
        ),
    )
    url = f"http://{host}:{port}/crop_review.html"
    print(f"Serving crop review: {url}", flush=True)
    print(f"Saving labels to: {out_dir / 'crop_review_labels.json'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping crop review server.", flush=True)
    finally:
        server.server_close()
    return 0


def decision_asset_ids(decision: JsonDict) -> tuple[str, str] | None:
    left = decision.get("source_asset_id")
    right = decision.get("target_asset_id")
    if not left or not right:
        pair_key = decision.get("pair_key", "")
        if "--" in pair_key:
            left, right = pair_key.split("--", 1)
    if not left or not right:
        return None
    return str(left), str(right)


def pair_edge_record(decision: JsonDict) -> JsonDict:
    asset_ids = decision_asset_ids(decision)
    left, right = asset_ids if asset_ids else ("", "")
    return {
        "source_asset_id": left,
        "target_asset_id": right,
        "decision": decision.get("decision"),
        "confidence": decision.get("confidence"),
        "score": decision.get("score"),
        "review_required": decision.get("review_required", False),
        "review_flags": decision.get("review_flags", []),
        "reason": decision.get("reason", ""),
    }


def build_ai_cluster_export(assets: list[VisualAsset], decisions: dict[str, JsonDict], allow_design_variants: bool = True) -> JsonDict:
    asset_ids = [asset.asset_id for asset in assets]
    product_uf = UnionFind(asset_ids)
    usable_decisions: list[JsonDict] = []
    for key, decision in decisions.items():
        asset_pair = decision_asset_ids({**decision, "pair_key": decision.get("pair_key", key)})
        if not asset_pair:
            continue
        left, right = asset_pair
        if left not in product_uf.parent or right not in product_uf.parent:
            continue
        label = str(decision.get("decision", "missing"))
        record = {**decision, "pair_key": key, "source_asset_id": left, "target_asset_id": right}
        usable_decisions.append(record)
        if product_same_decision(label):
            product_uf.union(left, right)

    by_root: dict[str, list[str]] = defaultdict(list)
    for asset_id in asset_ids:
        by_root[product_uf.find(asset_id)].append(asset_id)

    product_clusters: list[JsonDict] = []
    asset_to_product: dict[str, str] = {}
    for index, group in enumerate(sorted((sorted(group) for group in by_root.values()), key=lambda group: group[0]), start=1):
        cluster_id = f"P{index:04d}"
        for asset_id in group:
            asset_to_product[asset_id] = cluster_id
        product_clusters.append(
            {
                "cluster_id": cluster_id,
                "asset_ids": group,
                "size": len(group),
                "positive_edges": [],
                "non_product_edges_inside": [],
                "review_required": False,
                "review_flags": [],
            }
        )

    product_by_id: dict[str, JsonDict] = {str(cluster["cluster_id"]): cluster for cluster in product_clusters}
    design_uf = UnionFind([str(cluster["cluster_id"]) for cluster in product_clusters])
    product_edges: list[JsonDict] = []
    design_edges: list[JsonDict] = []
    negative_edges: list[JsonDict] = []
    uncertain_edges: list[JsonDict] = []
    cross_product_edges: list[JsonDict] = []

    for decision in usable_decisions:
        left, right = str(decision["source_asset_id"]), str(decision["target_asset_id"])
        left_product = asset_to_product[left]
        right_product = asset_to_product[right]
        label = str(decision.get("decision", "missing"))
        edge = pair_edge_record(decision)
        edge["source_product_cluster_id"] = left_product
        edge["target_product_cluster_id"] = right_product
        if product_same_decision(label):
            product_edges.append(edge)
            product_by_id[left_product]["positive_edges"].append(edge)
            design_uf.union(left_product, right_product)
        elif label in DESIGN_SAME_DECISIONS and allow_design_variants:
            design_edges.append(edge)
            design_uf.union(left_product, right_product)
            if left_product == right_product:
                product_by_id[left_product]["non_product_edges_inside"].append(edge)
        elif label in DESIGN_SAME_DECISIONS:
            negative_edges.append({**edge, "decision": "different_design", "original_decision": label})
            if left_product == right_product:
                product_by_id[left_product]["non_product_edges_inside"].append(edge)
        elif label in DIFFERENT_DECISIONS:
            negative_edges.append(edge)
            if left_product == right_product:
                product_by_id[left_product]["non_product_edges_inside"].append(edge)
        else:
            uncertain_edges.append(edge)
            if left_product == right_product:
                product_by_id[left_product]["non_product_edges_inside"].append(edge)
        if left_product != right_product:
            cross_product_edges.append(edge)

    for cluster in product_clusters:
        flags: list[str] = []
        inside = cast("list[JsonDict]", cluster["non_product_edges_inside"])
        positive = cast("list[JsonDict]", cluster["positive_edges"])
        inside_labels = {str(edge["decision"]) for edge in inside}
        if inside_labels.intersection(DIFFERENT_DECISIONS):
            flags.append("negative_edge_inside_product_cluster")
        if inside_labels.intersection(DESIGN_SAME_DECISIONS):
            flags.append("design_variant_edge_inside_product_cluster")
        if "unsure" in inside_labels:
            flags.append("unsure_edge_inside_product_cluster")
        if any(edge.get("review_required") for edge in positive + inside):
            flags.append("ai_review_required_edge")
        cluster["review_flags"] = flags
        cluster["review_required"] = bool(flags)

    design_roots: dict[str, list[str]] = defaultdict(list)
    for cluster in product_clusters:
        product_cluster_id = str(cluster["cluster_id"])
        design_roots[design_uf.find(product_cluster_id)].append(product_cluster_id)

    design_clusters: list[JsonDict] = []
    product_to_design: dict[str, str] = {}
    for index, product_ids in enumerate(sorted((sorted(ids) for ids in design_roots.values()), key=lambda ids: ids[0]), start=1):
        cluster_id = f"D{index:04d}"
        for product_id in product_ids:
            product_to_design[product_id] = cluster_id
        cluster_asset_ids = sorted(
            asset_id
            for product_id in product_ids
            for asset_id in cast("list[str]", product_by_id[product_id]["asset_ids"])
        )
        design_clusters.append(
            {
                "cluster_id": cluster_id,
                "product_cluster_ids": product_ids,
                "asset_ids": cluster_asset_ids,
                "size": len(cluster_asset_ids),
            }
        )

    assignments = []
    asset_lookup = asset_by_id(assets)
    for asset_id in asset_ids:
        product_id = asset_to_product[asset_id]
        assignments.append(
            {
                "asset_id": asset_id,
                "product_cluster_id": product_id,
                "design_cluster_id": product_to_design[product_id],
                "preferred_path": asset_lookup[asset_id].preferred_path,
                "quality_path": asset_lookup[asset_id].quality_path,
                "reference_cluster_ids": asset_lookup[asset_id].reference_cluster_ids,
            }
        )

    review_queue = [
        {
            "type": "product_cluster",
            "cluster_id": cluster["cluster_id"],
            "asset_ids": cluster["asset_ids"],
            "review_flags": cluster["review_flags"],
        }
        for cluster in product_clusters
        if cluster["review_required"]
    ]
    approved_product_clusters = [cluster for cluster in product_clusters if not cluster["review_required"]]
    blocked_product_clusters = [cluster for cluster in product_clusters if cluster["review_required"]]

    return {
        "product_clusters": approved_product_clusters,
        "product_clusters_all": product_clusters,
        "blocked_product_clusters": blocked_product_clusters,
        "design_clusters": design_clusters,
        "assignments": assignments,
        "edges": {
            "product_same": product_edges,
            "design_same": design_edges,
            "different": negative_edges,
            "uncertain": uncertain_edges,
            "cross_product": cross_product_edges,
        },
        "review_queue": review_queue,
        "summary": {
            "asset_count": len(asset_ids),
            "allow_design_variants": allow_design_variants,
            "product_cluster_count": len(approved_product_clusters),
            "product_cluster_total_count": len(product_clusters),
            "blocked_product_cluster_count": len(blocked_product_clusters),
            "product_singleton_count": sum(1 for cluster in approved_product_clusters if cluster["size"] == 1),
            "design_cluster_count": len(design_clusters),
            "review_queue_count": len(review_queue),
            "product_edge_count": len(product_edges),
            "design_edge_count": len(design_edges),
            "negative_edge_count": len(negative_edges),
            "uncertain_edge_count": len(uncertain_edges),
        },
    }


def write_assignments_csv(path: Path, assignments: list[JsonDict]) -> None:
    fields = ["asset_id", "product_cluster_id", "design_cluster_id", "preferred_path", "quality_path", "reference_cluster_ids"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in assignments:
            writer.writerow({**row, "reference_cluster_ids": "|".join(row["reference_cluster_ids"])})


def cluster_export_markdown(export: JsonDict) -> str:
    summary = export["summary"]
    lines = [
        "# AI Cluster Export",
        "",
        f"- Assets: {summary['asset_count']}",
        f"- Design variants allowed: {summary['allow_design_variants']}",
        f"- Approved product clusters: {summary['product_cluster_count']}",
        f"- Total product clusters: {summary['product_cluster_total_count']}",
        f"- Blocked product clusters: {summary['blocked_product_cluster_count']}",
        f"- Product singletons: {summary['product_singleton_count']}",
        f"- Design clusters: {summary['design_cluster_count']}",
        f"- Product-same edges: {summary['product_edge_count']}",
        f"- Same-design edges: {summary['design_edge_count']}",
        f"- Different-design/product edges: {summary['negative_edge_count']}",
        f"- Unsure edges: {summary['uncertain_edge_count']}",
        f"- Review queue items: {summary['review_queue_count']}",
        "",
        "`product_clusters.json` contains approved clusters only. Review-blocked clusters are written separately.",
        "Design clusters connect product clusters through same-design-variant edges.",
        "",
    ]
    return "\n".join(lines)


def write_review_queue_sheet(path: Path, review_queue: list[JsonDict], assets: list[VisualAsset], out_dir: Path) -> None:
    clusters = [
        {"cluster_id": item["cluster_id"], "asset_ids": item["asset_ids"], "size": len(item["asset_ids"])}
        for item in review_queue
    ]
    write_cluster_review_sheet(path, "AI Cluster Review Queue", clusters, assets, out_dir)


def write_cluster_export_outputs(out_dir: Path, assets: list[VisualAsset], export: JsonDict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "product_clusters.json", export["product_clusters"])
    write_json(out_dir / "product_clusters_all.json", export["product_clusters_all"])
    write_json(out_dir / "blocked_product_clusters.json", export["blocked_product_clusters"])
    write_json(out_dir / "design_clusters.json", export["design_clusters"])
    write_json(out_dir / "cluster_edges.json", export["edges"])
    write_json(out_dir / "cluster_review_queue.json", export["review_queue"])
    write_json(out_dir / "cluster_summary.json", export["summary"])
    write_assignments_csv(out_dir / "asset_cluster_assignments.csv", export["assignments"])
    (out_dir / "cluster_export_summary.md").write_text(cluster_export_markdown(export), encoding="utf-8")
    sheets_dir = out_dir / "review_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    write_cluster_review_sheet(
        sheets_dir / "20_product_clusters.html",
        "Approved AI Product Clusters",
        export["product_clusters"],
        assets,
        out_dir,
    )
    write_cluster_review_sheet(
        sheets_dir / "20b_blocked_product_clusters.html",
        "Blocked Product Clusters",
        export["blocked_product_clusters"],
        assets,
        out_dir,
    )
    write_cluster_review_sheet(
        sheets_dir / "21_design_clusters.html",
        "AI Design Clusters",
        [
            {"cluster_id": cluster["cluster_id"], "asset_ids": cluster["asset_ids"], "size": cluster["size"]}
            for cluster in export["design_clusters"]
        ],
        assets,
        out_dir,
    )
    write_review_queue_sheet(sheets_dir / "22_cluster_review_queue.html", export["review_queue"], assets, out_dir)


def same_filename_different_hash(occurrences: list[Occurrence]) -> list[JsonDict]:
    by_name: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_name[occurrence.filename].append(occurrence)
    rows = []
    for filename, group in sorted(by_name.items()):
        hashes = sorted({occurrence.sha256 for occurrence in group})
        sources = sorted({occurrence.source for occurrence in group})
        if len(hashes) > 1 and len(sources) > 1:
            rows.append(
                {
                    "filename": filename,
                    "hash_count": len(hashes),
                    "sources": sources,
                    "occurrences": [asdict(occurrence) for occurrence in sorted(group, key=occurrence_sort_key)],
                }
            )
    return rows


def report_markdown(
    occurrences: list[Occurrence],
    assets: list[JsonDict],
    filename_conflicts: list[JsonDict],
    edit_duplicate_matches: list[JsonDict] | None = None,
) -> str:
    source_counts = Counter(occurrence.source for occurrence in occurrences)
    kind_counts = Counter((occurrence.source, occurrence.kind) for occurrence in occurrences)
    Counter(asset["occurrences"][0]["source"] for asset in assets)
    flag_counts = Counter(flag for asset in assets for flag in asset["flags"])
    group_sizes = Counter(len(asset["occurrences"]) for asset in assets)
    reference_clusters = sorted({label for asset in assets for label in asset["reference_cluster_ids"]})
    before_fix = sum(1 for occurrence in occurrences if occurrence.is_before_fix)
    lines = [
        "# Normalization Report",
        "",
        "## Summary",
        "",
        f"- Image occurrences: {len(occurrences)}",
        f"- Visual assets: {len(assets)}",
        f"- Reference clusters represented: {len(reference_clusters)}",
        f"- Before-fix occurrences: {before_fix}",
        f"- Same-filename/different-hash conflicts: {len(filename_conflicts)}",
        f"- Edit-duplicate matches: {len(edit_duplicate_matches or [])}",
        "",
        "## Occurrences By Source",
        "",
    ]
    for source, count in sorted(source_counts.items()):
        lines.append(f"- {source}: {count}")
    lines.extend(["", "## Occurrences By Source And Kind", ""])
    for (source, kind), count in sorted(kind_counts.items()):
        lines.append(f"- {source}/{kind}: {count}")
    lines.extend(["", "## Visual Asset Group Sizes", ""])
    for size, count in sorted(group_sizes.items()):
        lines.append(f"- {size} occurrence(s): {count} asset(s)")
    lines.extend(["", "## Asset Flags", ""])
    if flag_counts:
        for flag, count in flag_counts.most_common():
            lines.append(f"- {flag}: {count}")
    else:
        lines.append("- No flags")
    lines.extend(["", "## Preferred Source Counts", ""])
    preferred_by_source: Counter[str] = Counter()
    for asset in assets:
        preferred_id = asset["preferred_occurrence_id"]
        preferred_occurrence = next(item for item in asset["occurrences"] if item["occurrence_id"] == preferred_id)
        preferred_by_source[preferred_occurrence["source"]] += 1
    for source, count in sorted(preferred_by_source.items()):
        lines.append(f"- {source}: {count}")
    needs_review = [asset for asset in assets if "needs_review" in asset["flags"]]
    conflicts = [asset for asset in assets if "reference_conflict" in asset["flags"]]
    lines.extend(
        [
            "",
            "## Review Queue",
            "",
            f"- Needs review: {len(needs_review)} asset(s)",
            f"- Reference conflicts: {len(conflicts)} asset(s)",
            "",
            "Review the HTML sheets in `review_sheets/` before trusting product clustering.",
            "",
        ]
    )
    if conflicts:
        lines.extend(["## Reference Conflicts", ""])
        for asset in conflicts[:50]:
            lines.append(
                f"- {asset['asset_id']}: {', '.join(asset['reference_cluster_ids'])} "
                f"({len(asset['occurrences'])} occurrences)"
            )
        lines.append("")
    if edit_duplicate_matches:
        lines.extend(["## Edit-Duplicate Match Examples", ""])
        for row in edit_duplicate_matches[:50]:
            lines.append(
                f"- {row['source_rel_path']} -> {row['target_source']}/{row['target_rel_path']} "
                f"(distance {row['distance']}, shots {row['source_shot_key']} -> {row['target_shot_key']})"
            )
        lines.append("")
    return "\n".join(lines)


def make_thumbnail(source: Path, destination: Path) -> bool:
    return resize_image_to_jpeg(source, destination, 220)


def thumbnail_name(occurrence: JsonDict) -> str:
    digest = stable_name_digest(occurrence["path"])
    return f"{occurrence['occurrence_id']}-{digest}.jpg"


def write_review_sheet(path: Path, title: str, assets: list[JsonDict], out_dir: Path, max_assets: int = 120) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    rel_prefix = Path("thumbs")
    cards = []
    for asset in assets[:max_assets]:
        occurrence_html = []
        for occurrence in asset["occurrences"]:
            thumb = thumbs_dir / thumbnail_name(occurrence)
            if not thumb.exists():
                make_thumbnail(Path(occurrence["path"]), thumb)
            label = (
                f"{occurrence['source']} / {occurrence['kind']}"
                + (f" / {occurrence['reference_cluster_id']}" if occurrence["reference_cluster_id"] else "")
                + (" / before fix" if occurrence["is_before_fix"] else "")
            )
            occurrence_html.append(
                "<figure>"
                f"<img src='{html.escape(str(rel_prefix / thumb.name))}' alt=''>"
                f"<figcaption>{html.escape(label)}<br>{html.escape(occurrence['filename'])}</figcaption>"
                "</figure>"
            )
        cards.append(
            "<section class='asset'>"
            f"<h2>{html.escape(asset['asset_id'])} "
            f"<span>confidence {asset['confidence']}</span></h2>"
            f"<p><b>Flags:</b> {html.escape(', '.join(asset['flags']) or 'none')}</p>"
            f"<p><b>Reference:</b> {html.escape(', '.join(asset['reference_cluster_ids']) or 'none')}</p>"
            f"<p><b>Preferred:</b> {html.escape(asset['preferred_path'])}</p>"
            "<div class='occurrences'>"
            + "\n".join(occurrence_html)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        "h1{font-size:24px} .asset{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".asset h2{font-size:18px;margin:0 0 8px}.asset h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".asset p{font-size:13px;margin:4px 0}.occurrences{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:180px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:180px;height:180px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{len(assets)} assets shown"
        + (f" (capped at {max_assets})" if len(assets) > max_assets else "")
        + ".</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def write_edit_duplicate_review_sheet(
    path: Path,
    edit_duplicate_matches: list[JsonDict],
    occurrences: list[Occurrence],
    out_dir: Path,
    max_pairs: int = 200,
) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    occurrence_by_id = {occurrence.occurrence_id: asdict(occurrence) for occurrence in occurrences}
    cards = []
    for match in edit_duplicate_matches[:max_pairs]:
        figures = []
        for key in ("source_occurrence_id", "target_occurrence_id"):
            occurrence = occurrence_by_id[match[key]]
            thumb = thumbs_dir / thumbnail_name(occurrence)
            if not thumb.exists():
                make_thumbnail(Path(occurrence["path"]), thumb)
            label = (
                f"{occurrence['source']} / {occurrence['kind']}<br>"
                f"{html.escape(occurrence['rel_path'])}<br>"
                f"shot {html.escape(occurrence['shot_key'] or 'none')}"
            )
            figures.append(
                "<figure>"
                f"<img src='{html.escape(str(Path('thumbs') / thumb.name))}' alt=''>"
                f"<figcaption>{label}</figcaption>"
                "</figure>"
            )
        cards.append(
            "<section class='pair'>"
            f"<h2>{html.escape(match['source_occurrence_id'])} - {html.escape(match['target_occurrence_id'])} "
            f"<span>distance {match['distance']}</span></h2>"
            "<div class='occurrences'>"
            + "\n".join(figures)
            + "</div></section>"
        )
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;background:#f7f7f4;color:#1f2933}"
        ".pair{break-inside:avoid;background:white;border:1px solid #ddd;border-radius:6px;margin:0 0 18px;padding:14px}"
        ".pair h2{font-size:18px;margin:0 0 8px}.pair h2 span{font-size:13px;color:#667085;font-weight:400}"
        ".occurrences{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}"
        "figure{margin:0;width:220px;border:1px solid #e5e7eb;padding:6px;background:#fafafa}"
        "img{width:220px;height:220px;object-fit:contain;background:#eee}figcaption{font-size:11px;line-height:1.25;word-break:break-word}"
        "</style></head><body>"
        "<h1>Edit-Duplicate Matches</h1>"
        f"<p>{len(edit_duplicate_matches)} edit-dedup pairs shown"
        + (f" (capped at {max_pairs})" if len(edit_duplicate_matches) > max_pairs else "")
        + ".</p>"
        + "\n".join(cards)
        + "</body></html>",
        encoding="utf-8",
    )


def write_review_sheets(
    out_dir: Path,
    assets: list[JsonDict],
    filename_conflicts: list[JsonDict],
    occurrences: list[Occurrence],
    edit_duplicate_matches: list[JsonDict],
) -> None:
    sheets_dir = out_dir / "review_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    needs_review = [asset for asset in assets if "needs_review" in asset["flags"]]
    reference_conflicts = [asset for asset in assets if "reference_conflict" in asset["flags"]]
    fixed_candidates = [asset for asset in assets if "has_fixed_preferred_candidate" in asset["flags"]]
    before_fix = [asset for asset in assets if "contains_before_fix" in asset["flags"]]
    singletons = [asset for asset in assets if "single_occurrence" in asset["flags"]]
    write_review_sheet(sheets_dir / "01_needs_review.html", "Needs Review", needs_review, out_dir)
    write_review_sheet(sheets_dir / "02_reference_conflicts.html", "Reference Conflicts", reference_conflicts, out_dir)
    write_review_sheet(sheets_dir / "03_fixed_preferred_candidates.html", "Fixed Preferred Candidates", fixed_candidates, out_dir)
    write_review_sheet(sheets_dir / "04_before_fix_assets.html", "Assets Containing Before-Fix Files", before_fix, out_dir)
    write_review_sheet(sheets_dir / "05_single_occurrence_assets.html", "Single Occurrence Assets", singletons, out_dir)

    # Filename conflicts are not visual assets, so render them as a simple HTML table.
    rows = []
    for item in filename_conflicts[:200]:
        cells = []
        for occurrence in item["occurrences"]:
            cells.append(
                f"{html.escape(occurrence['source'])} / {html.escape(occurrence['kind'])} / "
                f"{html.escape(occurrence['reference_cluster_id'])}<br>"
                f"{html.escape(occurrence['rel_path'])}<br>"
                f"<code>{html.escape(occurrence['sha256'][:12])}</code>"
            )
        rows.append(
            "<tr>"
            f"<td>{html.escape(item['filename'])}</td>"
            f"<td>{item['hash_count']}</td>"
            f"<td>{'<hr>'.join(cells)}</td>"
            "</tr>"
        )
    (sheets_dir / "06_same_filename_different_hash.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;vertical-align:top}"
        "code{font-size:12px}</style></head><body>"
        "<h1>Same Filename, Different Hash</h1>"
        f"<p>{len(filename_conflicts)} filename conflicts.</p>"
        "<table><tr><th>Filename</th><th>Hashes</th><th>Occurrences</th></tr>"
        + "\n".join(rows)
        + "</table></body></html>",
        encoding="utf-8",
    )
    write_edit_duplicate_review_sheet(
        sheets_dir / "07_edit_duplicate_matches.html",
        edit_duplicate_matches,
        occurrences,
        out_dir,
    )


def normalize_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    fixed = Path(args.fixed).resolve() if args.fixed else None
    unfixed = Path(args.unfixed).resolve() if args.unfixed else None
    reference = Path(args.reference).resolve() if args.reference else None
    sources = []
    if fixed:
        sources.append(("fixed", fixed))
    if unfixed:
        sources.append(("unfixed", unfixed))
    if reference:
        sources.append(("reference", reference))
    if not sources:
        print("ERROR: provide at least one source folder", file=sys.stderr)
        return 2
    occurrences: list[Occurrence] = []
    next_index = 1
    with tempfile.TemporaryDirectory(prefix="jewelry-normalize-") as tmp:
        tmpdir = Path(tmp)
        for source, base in sources:
            if not base.exists():
                print(f"ERROR: {source} folder does not exist: {base}", file=sys.stderr)
                return 2
            scanned = scan_source(source, base, tmpdir, next_index)
            occurrences.extend(scanned)
            next_index += len(scanned)

    if not occurrences:
        print("ERROR: no image files found", file=sys.stderr)
        return 1

    edit_distance = args.edit_dedup_distance if args.edit_dedup else None
    assets, occurrence_to_asset, edit_duplicate_matches = normalize_assets(occurrences, edit_distance)
    filename_conflicts = same_filename_different_hash(occurrences)

    write_inventory_csv(out_dir / "image_inventory.csv", occurrences)
    write_json(out_dir / "image_inventory.json", [asdict(occurrence) for occurrence in occurrences])
    write_manifest_csv(out_dir / "manifest.csv", assets)
    write_json(out_dir / "visual_assets.json", {"visual_assets": assets, "occurrence_to_asset": occurrence_to_asset})
    write_json(out_dir / "filename_conflicts.json", filename_conflicts)
    write_json(out_dir / "edit_duplicate_matches.json", edit_duplicate_matches)
    (out_dir / "normalization_report.md").write_text(
        report_markdown(occurrences, assets, filename_conflicts, edit_duplicate_matches),
        encoding="utf-8",
    )
    write_review_sheets(out_dir, assets, filename_conflicts, occurrences, edit_duplicate_matches)

    print(f"Image occurrences: {len(occurrences)}")
    print(f"Visual assets: {len(assets)}")
    print(f"Edit-duplicate matches: {len(edit_duplicate_matches)}")
    print(f"Needs review: {sum(1 for asset in assets if 'needs_review' in asset['flags'])}")
    print(f"Wrote: {out_dir}")
    return 0


def catalog_normalize_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if root.name == "קטלוג" and root.exists():
        catalog_root = root
    else:
        catalog_root = root / "קטלוג" if (root / "קטלוג").exists() else root
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_categories = [item.strip() for item in args.categories.split(",") if item.strip()]
    categories = {category for item in raw_categories if (category := catalog_normalized_category(item)) is not None}
    if not catalog_root.exists():
        print(f"ERROR: catalog root does not exist: {catalog_root}", file=sys.stderr)
        return 2
    if not categories:
        print("ERROR: no valid categories selected", file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="jewelry-catalog-normalize-") as tmp:
            occurrences = scan_catalog(catalog_root, categories, Path(tmp))
        if not occurrences:
            print("ERROR: no catalog image files found", file=sys.stderr)
            return 1
        assets, occurrence_to_asset = normalize_catalog_assets(occurrences)
        removed_assets = []
        if args.exclude_ambiguous_assets:
            removed_assets = [asset for asset in assets if catalog_asset_is_ambiguous(asset)]
            assets = [asset for asset in assets if not catalog_asset_is_ambiguous(asset)]
            kept_ids = {asset["asset_id"] for asset in assets}
            occurrence_to_asset = {
                occurrence_id: asset_id
                for occurrence_id, asset_id in occurrence_to_asset.items()
                if asset_id in kept_ids
            }
        write_json(out_dir / "catalog_occurrences.json", occurrences)
        write_json(out_dir / "catalog_visual_assets.json", {"visual_assets": assets, "occurrence_to_asset": occurrence_to_asset})
        write_json(out_dir / "removed_catalog_assets.json", removed_assets)
        write_catalog_manifest_csv(out_dir / "manifest.csv", assets)
        (out_dir / "catalog_normalization_report.md").write_text(catalog_report_markdown(occurrences, assets), encoding="utf-8")
        if removed_assets:
            (out_dir / "removed_catalog_assets.md").write_text(catalog_removed_report_markdown(removed_assets), encoding="utf-8")
        write_catalog_review_sheets(out_dir, assets)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Catalog root: {catalog_root}")
    print(f"Image occurrences: {len(occurrences)}")
    print(f"Visual assets: {len(assets)}")
    print(f"Removed ambiguous assets: {len(removed_assets)}")
    print(f"Wrote: {out_dir}")
    return 0


def parse_thresholds(raw: str) -> list[float]:
    thresholds = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not thresholds:
        msg = "provide at least one threshold"
        raise ValueError(msg)
    for threshold in thresholds:
        if threshold < -1 or threshold > 1:
            msg = f"threshold outside cosine similarity range [-1, 1]: {threshold}"
            raise ValueError(msg)
    return sorted(set(thresholds))


def cluster_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        assets = load_manifest(Path(args.manifest).resolve())
        total_assets = len(assets)
        if args.identity_only:
            assets = identity_assets(assets)
        if not assets:
            print("ERROR: manifest has zero assets after filtering", file=sys.stderr)
            return 1
        thresholds = parse_thresholds(args.thresholds)
        provider = build_embedding_provider(args)
        vectors, embedding_records = embed_assets(assets, provider, out_dir)
        missing = [record for record in embedding_records if record["status"] == "missing_image"]
        if len(vectors) < 2:
            print("ERROR: fewer than two assets could be embedded", file=sys.stderr)
            return 1
        scores = all_pair_scores(assets, vectors)
        write_pair_scores_csv(out_dir / "similarity_pairs.csv", scores)
        write_json(out_dir / "similarity_pairs.json", scores)
        sweep = threshold_sweep(assets, scores, thresholds)
        selected_threshold = choose_threshold(sweep, args.min_precision)
        clusters = cluster_from_scores(assets, scores, selected_threshold)
        selected_edges = [row for row in scores if row["score"] >= selected_threshold]
        benchmark = benchmark_clusters(assets, clusters)
        benchmark["provider"] = provider.provider_id
        benchmark["threshold"] = selected_threshold
        benchmark["candidate_threshold"] = args.candidate_threshold or selected_threshold
        benchmark["candidate_top_k"] = args.candidate_top_k
        benchmark["missing_embedding_count"] = len(missing)
        benchmark["missing_embeddings"] = missing
        diagnostics = pair_diagnostics(assets, scores, selected_threshold)
        candidates = candidate_pairs(assets, scores, benchmark["candidate_threshold"], top_k=args.candidate_top_k)
        benchmark["candidate_coverage"] = candidate_coverage_report(candidates, assets)

        write_threshold_sweep_csv(out_dir / "threshold_sweep.csv", sweep)
        write_json(out_dir / "threshold_sweep.json", sweep)
        write_json(out_dir / "similarity_edges.json", selected_edges)
        write_json(out_dir / "predicted_clusters.json", {"threshold": selected_threshold, "clusters": clusters})
        write_json(out_dir / "benchmark_report.json", benchmark)
        (out_dir / "benchmark_report.md").write_text(
            benchmark_report_markdown(provider.provider_id, selected_threshold, sweep, benchmark),
            encoding="utf-8",
        )
        write_clustering_review_sheets(out_dir, clusters, assets, benchmark)
        write_pair_diagnostics(out_dir, diagnostics, assets)
        write_candidate_pairs(out_dir, candidates, assets)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Provider: {provider.provider_id}")
    print(f"Loaded assets: {len(assets)}/{total_assets}")
    print(f"Embedded assets: {len(vectors)}/{len(assets)}")
    print(f"Recommended threshold: {selected_threshold:.4f}")
    print(f"Predicted clusters: {benchmark['cluster_count']}")
    print(f"Precision/recall/F1: {benchmark['precision']:.3f}/{benchmark['recall']:.3f}/{benchmark['f1']:.3f}")
    print(f"Merge disagreements: {benchmark['merge_error_count']}")
    print(f"Split disagreements: {benchmark['split_error_count']}")
    print(f"Wrote: {out_dir}")
    return 0


def ai_adjudicate_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        load_dotenv()
        assets = load_manifest(Path(args.manifest).resolve())
        asset_lookup = asset_by_id(assets)
        pairs = load_candidate_pairs(Path(args.candidates).resolve())
        if args.max_pairs:
            pairs = pairs[: args.max_pairs]
        cache_path = out_dir / "ai_decisions.json"
        decisions = load_ai_decision_cache(cache_path)
        current_decisions = {}
        cache_keys_by_pair = {}
        for pair in pairs:
            left = asset_lookup[pair["source_asset_id"]]
            right = asset_lookup[pair["target_asset_id"]]
            cache_key = ai_decision_cache_key(pair, left, right, args.model, args.max_image_size)
            cache_keys_by_pair[candidate_pair_key(pair)] = cache_key
            if cache_key in decisions:
                current_decisions[cache_key] = decisions[cache_key]
        if args.dry_run or args.from_cache:
            benchmark = benchmark_ai_decisions(pairs, current_decisions)
            write_ai_adjudication_outputs(out_dir, assets, args.model, decisions, benchmark)
            print(f"From-cache candidate pairs: {len(pairs)}")
            print(f"Cached decisions: {benchmark['decided_pair_count']}")
            print(f"Wrote: {out_dir}")
            return 0

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="jewelry-ai-pairs-") as tmp:
            tmpdir = Path(tmp)
            for index, pair in enumerate(pairs, start=1):
                pair_key = candidate_pair_key(pair)
                left = asset_lookup[pair["source_asset_id"]]
                right = asset_lookup[pair["target_asset_id"]]
                cache_key = cache_keys_by_pair[pair_key]
                if cache_key in decisions and not args.rejudge:
                    continue
                print(f"AI judging {index}/{len(pairs)} {pair_key} score={pair['score']:.4f}", flush=True)
                left_url = image_data_url_for_api(Path(left.preferred_path), tmpdir, args.max_image_size)
                right_url = image_data_url_for_api(Path(right.preferred_path), tmpdir, args.max_image_size)
                decision = None
                for attempt in range(1, args.retries + 2):
                    try:
                        decision = call_openai_pair_judge(
                            api_key=api_key,
                            model=args.model,
                            left_image_url=left_url,
                            right_image_url=right_url,
                            timeout=args.timeout,
                        )
                        break
                    except Exception as exc:
                        if attempt == args.retries + 1 or not retryable_api_error(exc):
                            raise
                        delay = min(2 ** attempt, 30)
                        print(f"Retrying {pair_key} after {type(exc).__name__}: {exc} ({delay}s)", flush=True)
                        time.sleep(delay)
                if decision is None:
                    msg = f"failed to judge {pair_key}"
                    raise RuntimeError(msg)
                decision.update(
                    {
                        "cache_key": cache_key,
                        "pair_key": pair_key,
                        "source_asset_id": pair["source_asset_id"],
                        "target_asset_id": pair["target_asset_id"],
                        "score": pair["score"],
                        "benchmark_same_reference": pair.get("benchmark_same_reference"),
                        "model": args.model,
                        "prompt_version": AI_PAIR_PROMPT_VERSION,
                        "max_image_size": args.max_image_size,
                        "source_image_sha256": sha256(Path(left.preferred_path)),
                        "target_image_sha256": sha256(Path(right.preferred_path)),
                    }
                )
                decisions[cache_key] = decision
                write_json(cache_path, decisions)
        current_decisions = {cache_key: decisions[cache_key] for cache_key in cache_keys_by_pair.values() if cache_key in decisions}
        benchmark = benchmark_ai_decisions(pairs, current_decisions)
        write_ai_adjudication_outputs(out_dir, assets, args.model, decisions, benchmark)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Model: {args.model}")
    print(f"Candidate pairs: {benchmark['candidate_pair_count']}")
    print(f"Decided pairs: {benchmark['decided_pair_count']}")
    print(f"Precision/recall/F1: {benchmark['precision']:.3f}/{benchmark['recall']:.3f}/{benchmark['f1']:.3f}")
    print(f"False positives: {benchmark['false_positive']}")
    print(f"False negatives: {benchmark['false_negative']}")
    print(f"Wrote: {out_dir}")
    return 0


def build_clusters_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    try:
        assets = load_manifest(Path(args.manifest).resolve())
        decisions = load_ai_decision_cache(Path(args.decisions).resolve())
        export = build_ai_cluster_export(assets, decisions, allow_design_variants=not args.no_design_variants)
        write_cluster_export_outputs(out_dir, assets, export)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    summary = export["summary"]
    print(f"Assets: {summary['asset_count']}")
    print(f"Approved product clusters: {summary['product_cluster_count']}")
    print(f"Blocked product clusters: {summary['blocked_product_cluster_count']}")
    print(f"Product singletons: {summary['product_singleton_count']}")
    print(f"Design clusters: {summary['design_cluster_count']}")
    print(f"Review queue items: {summary['review_queue_count']}")
    print(f"Wrote: {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jewelry cluster benchmark utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    normalize = subparsers.add_parser("normalize", aliases=["inventory"], help="build normalized visual assets")
    normalize.add_argument("--fixed", help="folder containing fixed/edited images")
    normalize.add_argument("--unfixed", help="folder containing unfixed/original images")
    normalize.add_argument("--reference", help="folder containing reference cluster folders")
    normalize.add_argument("--out", required=True, help="output directory")
    normalize.add_argument(
        "--edit-dedup",
        action="store_true",
        help="collapse fixed/unfixed Photoshop edits using conservative mutual-nearest perceptual matches",
    )
    normalize.add_argument(
        "--edit-dedup-distance",
        type=int,
        default=3,
        help="maximum ahash+dhash distance for edit dedup when --edit-dedup is enabled",
    )
    normalize.set_defaults(func=normalize_command)

    catalog_normalize = subparsers.add_parser("catalog-normalize", help="normalize product-folder catalog images")
    catalog_normalize.add_argument("--root", required=True, help="catalog root folder or folder containing קטלוג")
    catalog_normalize.add_argument("--out", required=True, help="output directory")
    catalog_normalize.add_argument(
        "--categories",
        default="rings,earrings,necklaces",
        help="comma-separated categories: rings, earrings, necklaces, or Hebrew folder names",
    )
    catalog_normalize.add_argument(
        "--exclude-ambiguous-assets",
        action="store_true",
        help="remove assets with missing IDs, folder/filename ID conflicts, or only ambiguous multi-ID folder IDs",
    )
    catalog_normalize.set_defaults(func=catalog_normalize_command)

    image_registry = subparsers.add_parser("image-registry", help="register raw image files without catalog metadata")
    image_registry.add_argument("--input-folder", required=True, help="folder of raw jewelry photos")
    image_registry.add_argument("--out", required=True, help="output directory")
    image_registry.set_defaults(func=image_registry_command)

    materialize_mixed = subparsers.add_parser("materialize-mixed-benchmark", help="copy a labeled mixed benchmark folder for evaluation only")
    materialize_mixed.add_argument("--labels", required=True, help="final labeled manifest CSV; benchmark-only source")
    materialize_mixed.add_argument("--out", required=True, help="output directory")
    materialize_mixed.add_argument("--per-category-products", type=int, default=8, help="identity products to sample per category")
    materialize_mixed.add_argument("--identity-per-product", type=int, default=2, help="identity images to sample per selected product")
    materialize_mixed.add_argument("--supporting-per-category", type=int, default=8, help="supporting images to sample per category")
    materialize_mixed.set_defaults(func=materialize_mixed_benchmark_command)

    image_profile = subparsers.add_parser("image-profile", help="cache visual routing profiles for registered images")
    image_profile.add_argument("--manifest", required=True, help="image_manifest.json from image-registry")
    image_profile.add_argument("--out", required=True, help="output directory")
    image_profile.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    image_profile.add_argument("--max-image-size", type=int, default=1024, help="max image side sent to AI")
    image_profile.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    image_profile.add_argument("--from-cache", action="store_true", help="write profiles from cache without calling AI")
    image_profile.set_defaults(func=image_profile_command)

    generate_evidence = subparsers.add_parser("generate-evidence", help="generate multi-view evidence bundles from image profiles")
    generate_evidence.add_argument("--manifest", required=True, help="image_manifest.json from image-registry")
    generate_evidence.add_argument("--profiles", required=True, help="image_profiles.json from image-profile")
    generate_evidence.add_argument("--out", required=True, help="output directory")
    generate_evidence.add_argument(
        "--detector",
        choices=["profile", "owlv2"],
        default="profile",
        help="use profile boxes only, or run OWLv2 for crop views",
    )
    generate_evidence.add_argument("--owlv2-model", default="google/owlv2-base-patch16-ensemble", help="Hugging Face OWLv2 model id")
    generate_evidence.add_argument("--owlv2-threshold", type=float, default=0.05, help="raw OWLv2 score threshold")
    generate_evidence.add_argument("--device", default="auto", help="OWLv2 device: auto, cpu, mps, or cuda")
    generate_evidence.set_defaults(func=generate_evidence_command)

    product_profile = subparsers.add_parser("product-profile", help="profile one production image as DB-ready JSON for OpenCLAW")
    product_profile.add_argument("--image", required=True, help="source image path")
    product_profile.add_argument("--image-id", required=True, help="stable caller-owned image id")
    product_profile.add_argument("--out", required=True, help="output JSON path")
    product_profile.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    product_profile.add_argument("--max-image-size", type=int, default=1024, help="max image side sent to AI")
    product_profile.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    product_profile.add_argument(
        "--mock-response",
        help="parse a local model-response JSON file instead of calling OpenAI; useful for OpenCLAW plumbing tests",
    )
    product_profile.set_defaults(func=product_profile_command)

    product_embed = subparsers.add_parser("product-embed", help="embed one production image as crop JSON for OpenCLAW")
    product_embed.add_argument("--image", required=True, help="source image path")
    product_embed.add_argument("--image-id", required=True, help="stable caller-owned image id")
    product_embed.add_argument("--out", required=True, help="output JSON path")
    product_embed.add_argument(
        "--provider",
        choices=["fake", "dinov2", "clip", "siglip"],
        default="siglip",
        help="embedding provider; use fake for OpenCLAW plumbing tests and siglip for production",
    )
    product_embed.add_argument("--model-id", help="provider-specific Hugging Face model id for CLIP/SigLIP providers")
    product_embed.add_argument("--dinov2-model", default="dinov2_vits14", help="DINOv2 model name")
    product_embed.add_argument("--device", default="auto", help="embedding/detector device: auto, cpu, mps, or cuda")
    product_embed.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    product_embed.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    product_embed.add_argument("--profile", help="optional single image profile JSON object for crop evidence generation")
    product_embed.add_argument(
        "--detector",
        choices=["profile", "owlv2"],
        default="profile",
        help="crop detector used when --profile is supplied",
    )
    product_embed.add_argument("--owlv2-model", default="google/owlv2-base-patch16-ensemble", help="Hugging Face OWLv2 model id")
    product_embed.add_argument("--owlv2-threshold", type=float, default=0.05, help="raw OWLv2 score threshold")
    product_embed.set_defaults(func=product_embed_command)

    multi_view = subparsers.add_parser("multi-view-retrieve", help="embed evidence views and retrieve candidate image matches")
    multi_view.add_argument("--evidence", required=True, help="evidence_views.json from generate-evidence")
    multi_view.add_argument("--profiles", help="optional image_profiles.json for feature agreement scoring")
    multi_view.add_argument("--out", required=True, help="output directory")
    multi_view.add_argument("--provider", choices=["fake", "dinov2", "clip", "siglip"], default="siglip", help="embedding provider")
    multi_view.add_argument("--top-k", type=int, default=20, help="candidate views/images to retain per query")
    multi_view.add_argument("--model-id", help="provider-specific Hugging Face model id for CLIP/SigLIP providers")
    multi_view.add_argument("--dinov2-model", default="dinov2_vits14", help="DINOv2 model name")
    multi_view.add_argument("--device", default="auto", help="embedding device: auto, cpu, mps, or cuda")
    multi_view.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    multi_view.add_argument("--offline-model-cache", action="store_true", help="load models from local cache only")
    multi_view.set_defaults(func=multi_view_retrieve_command)

    adjudicate_retrieval = subparsers.add_parser("adjudicate-retrieval", help="AI-judge uncertain retrieval candidates before human review")
    adjudicate_retrieval.add_argument("--profiles", required=True, help="image_profiles.json from image-profile")
    adjudicate_retrieval.add_argument("--evidence", required=True, help="evidence_views.json from generate-evidence")
    adjudicate_retrieval.add_argument("--retrieval", required=True, help="retrieval_candidates.json from multi-view-retrieve")
    adjudicate_retrieval.add_argument("--out", required=True, help="output directory")
    adjudicate_retrieval.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    adjudicate_retrieval.add_argument("--low-margin", type=float, default=0.03, help="score margin threshold for adjudication/review")
    adjudicate_retrieval.add_argument("--qa-sample-rate", type=float, default=0.05, help="deterministic human QA sample rate")
    adjudicate_retrieval.add_argument("--auto-accept-confidence", type=float, default=0.90, help="minimum adjudicator confidence for auto matches")
    adjudicate_retrieval.add_argument("--auto-accept-score", type=float, default=0.90, help="minimum retrieval score for auto matches")
    adjudicate_retrieval.add_argument("--auto-accept-margin", type=float, default=0.03, help="minimum retrieval margin for auto matches")
    adjudicate_retrieval.add_argument("--max-image-size", type=int, default=768, help="max image side sent to AI")
    adjudicate_retrieval.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    adjudicate_retrieval.add_argument("--from-cache", action="store_true", help="write reports from cache without calling AI")
    adjudicate_retrieval.set_defaults(func=adjudicate_retrieval_command)

    benchmark_matches = subparsers.add_parser("benchmark-matches", help="benchmark image-match outputs against catalog labels")
    benchmark_matches.add_argument("--registry", required=True, help="image_manifest.json from image-registry")
    benchmark_matches.add_argument("--labels", required=True, help="final labeled manifest CSV; benchmark-only, not production input")
    benchmark_matches.add_argument("--matches", required=True, help="final_matches.json from adjudicate-retrieval")
    benchmark_matches.add_argument("--review-queue", required=True, help="human_review_queue.json from adjudicate-retrieval")
    benchmark_matches.add_argument("--adjudications", help="optional retrieval_adjudications.json for pairwise GPT-efficiency reporting")
    benchmark_matches.add_argument("--out", required=True, help="output directory")
    benchmark_matches.set_defaults(func=benchmark_matches_command)

    benchmark_summary = subparsers.add_parser("benchmark-summary", help="aggregate multiple match_benchmark.json reports")
    benchmark_summary.add_argument("--benchmarks", required=True, nargs="+", help="one or more match_benchmark.json files")
    benchmark_summary.add_argument("--out", required=True, help="output directory")
    benchmark_summary.set_defaults(func=benchmark_summary_command)

    cluster = subparsers.add_parser("cluster", help="cluster normalized visual assets")
    cluster.add_argument("--manifest", required=True, help="normalized manifest.csv")
    cluster.add_argument("--assets", help="reserved for compatibility with visual_assets.json")
    cluster.add_argument("--out", required=True, help="output directory")
    cluster.add_argument(
        "--provider",
        choices=["fake", "dinov2", "clip", "siglip"],
        default="dinov2",
        help="embedding provider. Use fake only for plumbing tests.",
    )
    cluster.add_argument(
        "--model-id",
        help="provider-specific Hugging Face model id for CLIP/SigLIP providers",
    )
    cluster.add_argument(
        "--dinov2-model",
        default="dinov2_vits14",
        help="DINOv2 torch.hub model name, e.g. dinov2_vits14 or dinov2_vitb14",
    )
    cluster.add_argument("--device", default="auto", help="embedding device: auto, cpu, mps, or cuda")
    cluster.add_argument("--image-size", type=int, default=224, help="square padded embedding image size")
    cluster.add_argument(
        "--offline-model-cache",
        action="store_true",
        help="load Hugging Face models from local cache only and avoid network checks",
    )
    cluster.add_argument(
        "--thresholds",
        default="0.70,0.75,0.80,0.83,0.86,0.89,0.92,0.93,0.94,0.95,0.96,0.97,0.98,0.99",
        help="comma-separated cosine similarity thresholds to benchmark",
    )
    cluster.add_argument(
        "--min-precision",
        type=float,
        default=0.98,
        help="minimum precision for conservative threshold selection",
    )
    cluster.add_argument(
        "--candidate-threshold",
        type=float,
        help="optional lower threshold for candidate pairs that should be reviewed or AI-adjudicated",
    )
    cluster.add_argument(
        "--candidate-top-k",
        type=int,
        default=0,
        help="also include each asset's top-K nearest neighbors as candidate pairs",
    )
    cluster.add_argument(
        "--identity-only",
        action="store_true",
        help="cluster only assets that carry identity labels, e.g. identity_eligible catalog rows",
    )
    cluster.set_defaults(func=cluster_command)

    adjudicate = subparsers.add_parser("ai-adjudicate", help="AI-judge candidate pairs and benchmark decisions")
    adjudicate.add_argument("--manifest", required=True, help="normalized manifest.csv")
    adjudicate.add_argument("--candidates", required=True, help="candidate_pairs.json from cluster command")
    adjudicate.add_argument("--out", required=True, help="output directory")
    adjudicate.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    adjudicate.add_argument("--max-pairs", type=int, help="limit pairs for a proof run")
    adjudicate.add_argument("--max-image-size", type=int, default=768, help="max image side sent to AI")
    adjudicate.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    adjudicate.add_argument("--retries", type=int, default=3, help="retry count for retryable OpenAI/API failures")
    adjudicate.add_argument("--rejudge", action="store_true", help="re-run pairs even when cached")
    adjudicate.add_argument("--from-cache", action="store_true", help="write reports from cached decisions without calling AI")
    adjudicate.add_argument("--dry-run", action="store_true", help="deprecated alias for --from-cache")
    adjudicate.set_defaults(func=ai_adjudicate_command)

    crop_probe = subparsers.add_parser("jewelry-crop-probe", help="detect jewelry boxes and write crop review artifacts")
    crop_probe.add_argument("--manifest", required=True, help="final labeled manifest.csv")
    crop_probe.add_argument("--out", required=True, help="output directory")
    crop_probe.add_argument("--limit", type=int, default=3, help="number of lifestyle/supporting images to process; 0 means all")
    crop_probe.add_argument("--category", default="", help="optional category filter: rings, earrings, necklaces, or Hebrew folder name")
    crop_probe.add_argument(
        "--detector",
        choices=["openai", "owlv2"],
        default="openai",
        help="detector backend. owlv2 runs locally with Hugging Face transformers.",
    )
    crop_probe.add_argument("--model", default="gpt-4.1-mini", help="OpenAI vision-capable model")
    crop_probe.add_argument("--owlv2-model", default="google/owlv2-base-patch16-ensemble", help="Hugging Face OWLv2 model id")
    crop_probe.add_argument("--owlv2-threshold", type=float, default=0.05, help="raw OWLv2 score threshold before local filtering")
    crop_probe.add_argument("--device", default="auto", help="OWLv2 device: auto, cpu, mps, or cuda")
    crop_probe.add_argument(
        "--auto-pass-sample-rate",
        type=float,
        default=0.10,
        help="deterministic sample rate for auto-pass QA review",
    )
    crop_probe.add_argument("--max-image-size", type=int, default=1024, help="max image side sent to AI")
    crop_probe.add_argument("--timeout", type=int, default=90, help="OpenAI request timeout seconds")
    crop_probe.add_argument("--max-boxes", type=int, default=5, help="maximum detected boxes to expand per image")
    crop_probe.set_defaults(func=jewelry_crop_probe_command)

    import_review = subparsers.add_parser("import-crop-review-labels", help="import exported crop review CSV into a results directory")
    import_review.add_argument("--csv", required=True, help="exported crop_review_labels.csv")
    import_review.add_argument("--out", required=True, help="crop probe output directory")
    import_review.set_defaults(func=import_crop_review_labels_command)

    review_server = subparsers.add_parser("jewelry-crop-review-server", help="serve crop_review.html and save labels into the results directory")
    review_server.add_argument("--out", required=True, help="crop probe output directory")
    review_server.add_argument("--host", default="127.0.0.1", help="host to bind")
    review_server.add_argument("--port", type=int, default=8765, help="port to bind, or 0 for any free port")
    review_server.set_defaults(func=crop_review_server_command)

    build_clusters = subparsers.add_parser("build-clusters", help="build product/design clusters from AI decisions")
    build_clusters.add_argument("--manifest", required=True, help="normalized manifest.csv")
    build_clusters.add_argument("--decisions", required=True, help="ai_decisions.json from ai-adjudicate")
    build_clusters.add_argument("--out", required=True, help="output directory")
    build_clusters.add_argument(
        "--no-design-variants",
        action="store_true",
        help="treat same_design_variant AI edges as different_design for datasets without design-family labels",
    )
    build_clusters.set_defaults(func=build_clusters_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
