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
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"}
SOURCE_PRIORITY = {"fixed": 0, "unfixed": 1, "reference": 2}
KIND_REVIEW_PRIORITY = {"web": 0, "png": 1, "print": 2, "other": 3}
KIND_QUALITY_PRIORITY = {"print": 0, "png": 1, "web": 2, "other": 3}
SHOT_KEY_RE = re.compile(
    r"^(?P<date>\d{8})-(?:high res|web res 1500|png 1500)(?:-(?P<num>\d+))?\.(?:jpg|jpeg|png)$",
    re.IGNORECASE,
)


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


def run_sips(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sips", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def image_size(path: Path) -> tuple[int | None, int | None]:
    result = run_sips(["-g", "pixelWidth", "-g", "pixelHeight", str(path)])
    if result.returncode != 0:
        return None, None
    width = None
    height = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    return width, height


def parse_bmp_pixels(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    data = path.read_bytes()
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    raw_height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    if bpp not in (24, 32):
        raise ValueError(f"unsupported BMP bit depth: {bpp}")
    height = abs(raw_height)
    top_down = raw_height < 0
    bytes_per_pixel = bpp // 8
    row_stride = ((width * bytes_per_pixel + 3) // 4) * 4
    pixels: list[tuple[int, int, int]] = []
    for y in range(height):
        source_y = y if top_down else height - 1 - y
        row = offset + source_y * row_stride
        for x in range(width):
            pixel_offset = row + x * bytes_per_pixel
            b, g, r = data[pixel_offset : pixel_offset + 3]
            pixels.append((r, g, b))
    return width, height, pixels


def bmp_thumbnail(path: Path, width: int, height: int, tmpdir: Path) -> Path | None:
    out = tmpdir / f"thumb-{hashlib.md5(str(path).encode()).hexdigest()}-{width}x{height}.bmp"
    result = run_sips(["-z", str(height), str(width), "-s", "format", "bmp", str(path), "--out", str(out)])
    if result.returncode != 0 or not out.exists():
        return None
    return out


def average_hash(path: Path, tmpdir: Path) -> str:
    bmp = bmp_thumbnail(path, 8, 8, tmpdir)
    if bmp is None:
        return ""
    _, _, pixels = parse_bmp_pixels(bmp)
    grays = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    avg = sum(grays) / len(grays)
    bits = ["1" if gray >= avg else "0" for gray in grays]
    return f"{int(''.join(bits), 2):016x}"


def difference_hash(path: Path, tmpdir: Path) -> str:
    bmp = bmp_thumbnail(path, 9, 8, tmpdir)
    if bmp is None:
        return ""
    width, height, pixels = parse_bmp_pixels(bmp)
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


def scan_source(source: str, base: Path, tmpdir: Path, start_index: int) -> list[Occurrence]:
    occurrences: list[Occurrence] = []
    for index, path in enumerate(iter_image_files(base), start=start_index):
        rel = path.relative_to(base)
        if source == "reference":
            reference_cluster_id = rel.parts[0] if len(rel.parts) > 1 else ""
        else:
            reference_cluster_id = ""
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


def edit_duplicate_sort_key(left: Occurrence, right: Occurrence) -> tuple:
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


def edited_duplicate_pairs(occurrences: list[Occurrence], max_distance: int) -> list[dict]:
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
) -> tuple[list[dict], dict[str, str], list[dict]]:
    sha_labels = inherited_reference_labels(occurrences)
    uf = UnionFind([occurrence.occurrence_id for occurrence in occurrences])
    by_id = {occurrence.occurrence_id: occurrence for occurrence in occurrences}

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

    edit_pairs: list[dict] = []
    if edit_dedup_distance is not None:
        edit_pairs = edited_duplicate_pairs(occurrences, edit_dedup_distance)
        for pair in edit_pairs:
            uf.union(pair["source_occurrence_id"], pair["target_occurrence_id"])

    by_root: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_root[uf.find(occurrence.occurrence_id)].append(occurrence)

    assets: list[dict] = []
    occurrence_to_asset: dict[str, str] = {}
    for index, group in enumerate(sorted(by_root.values(), key=asset_sort_key), start=1):
        asset_id = f"A{index:04d}"
        for occurrence in group:
            occurrence_to_asset[occurrence.occurrence_id] = asset_id
        labels = sorted(set().union(*(occurrence_reference_labels(occurrence, sha_labels) for occurrence in group)))
        preferred = choose_preferred(group, KIND_REVIEW_PRIORITY)
        quality = choose_preferred(group, KIND_QUALITY_PRIORITY)
        sources = sorted(set(occurrence.source for occurrence in group))
        kinds = sorted(set(occurrence.kind for occurrence in group))
        shot_keys = sorted(set(occurrence.shot_key for occurrence in group if occurrence.shot_key))
        flags = []
        if len(labels) > 1:
            flags.append("reference_conflict")
        if any(occurrence.is_before_fix for occurrence in group):
            flags.append("contains_before_fix")
        if "fixed" in sources and ("unfixed" in sources or "reference" in sources):
            flags.append("has_fixed_preferred_candidate")
        if len(set(occurrence.filename for occurrence in group)) == 1 and len(set(occurrence.sha256 for occurrence in group)) > 1:
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


def occurrence_sort_key(occurrence: Occurrence) -> tuple:
    return (
        SOURCE_PRIORITY.get(occurrence.source, 99),
        KIND_REVIEW_PRIORITY.get(occurrence.kind, 99),
        occurrence.is_before_fix,
        occurrence.rel_path,
    )


def asset_sort_key(group: list[Occurrence]) -> tuple:
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


def write_manifest_csv(path: Path, assets: list[dict]) -> None:
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


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def load_manifest(path: Path) -> list[VisualAsset]:
    if not path.exists():
        raise FileNotFoundError(f"manifest does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {
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
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manifest missing required columns: {', '.join(sorted(missing))}")
        assets = []
        for row in reader:
            preferred_path = row["preferred_path"]
            if not preferred_path:
                raise ValueError(f"asset {row['asset_id']} has no preferred_path")
            assets.append(
                VisualAsset(
                    asset_id=row["asset_id"],
                    preferred_path=preferred_path,
                    quality_path=row["quality_path"],
                    reference_cluster_ids=split_pipe_list(row["reference_cluster_ids"]),
                    sources=split_pipe_list(row["sources"]),
                    kinds=split_pipe_list(row["kinds"]),
                    shot_keys=split_pipe_list(row["shot_keys"]),
                    confidence=float(row["confidence"] or 0),
                    flags=split_pipe_list(row["flags"]),
                    occurrence_count=int(row["occurrence_count"] or 0),
                )
            )
    if not assets:
        raise ValueError("manifest has zero assets")
    return assets


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
        values = []
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
            raise RuntimeError("DINOv2 provider requires PyTorch. Install torch first.") from exc

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

    def load_model(self):
        try:
            from transformers import AutoModel
        except ImportError:
            return self.torch.hub.load("facebookresearch/dinov2", self.model_name)

        self.backend = "transformers"
        repo_id = {
            "dinov2_vits14": "facebook/dinov2-small",
            "dinov2_vitb14": "facebook/dinov2-base",
            "dinov2_vitl14": "facebook/dinov2-large",
            "dinov2_vitg14": "facebook/dinov2-giant",
        }.get(self.model_name, self.model_name)
        return AutoModel.from_pretrained(repo_id, local_files_only=self.local_files_only)

    def resolve_device(self, device: str) -> str:
        if device != "auto":
            return device
        torch = self.torch
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def image_tensor(self, image_path: Path, tmpdir: Path):
        torch = self.torch
        resized = tmpdir / f"{hashlib.md5(str(image_path).encode()).hexdigest()}-{self.image_size}.bmp"
        result = run_sips(["-Z", str(self.image_size), "-s", "format", "bmp", str(image_path), "--out", str(resized)])
        if result.returncode != 0 or not resized.exists():
            raise RuntimeError(f"sips failed to prepare image for embedding: {image_path}")
        width, height, pixels = parse_bmp_pixels(resized)
        image = torch.tensor(pixels, dtype=torch.float32).view(height, width, 3).permute(2, 0, 1) / 255.0
        canvas = torch.ones((3, self.image_size, self.image_size), dtype=torch.float32)
        top = max((self.image_size - height) // 2, 0)
        left = max((self.image_size - width) // 2, 0)
        canvas[:, top : top + height, left : left + width] = image[:, : self.image_size, : self.image_size]
        mean = torch.tensor([0.485, 0.456, 0.406], dtype=torch.float32).view(3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225], dtype=torch.float32).view(3, 1, 1)
        return ((canvas - mean) / std).unsqueeze(0).to(self.device)

    def embed(self, image_path: Path) -> list[float]:
        torch = self.torch
        with tempfile.TemporaryDirectory(prefix="jewelry-dinov2-") as tmp:
            tensor = self.image_tensor(image_path, Path(tmp))
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
            raise RuntimeError(f"{provider_name} provider requires torch and transformers.") from exc

        self.torch = torch
        self.provider_name = provider_name
        self.model_id = model_id
        self.image_size = image_size
        self.local_files_only = local_files_only
        self.device = self.resolve_device(device)
        safe_model_id = model_id.replace("/", "_")
        self.provider_id = f"{provider_name}-{safe_model_id}-{self.device}-s{image_size}"
        self.model = AutoModel.from_pretrained(model_id, local_files_only=local_files_only)
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

    def image_tensor(self, image_path: Path, tmpdir: Path):
        if self.provider_name == "clip":
            mean = [0.48145466, 0.4578275, 0.40821073]
            std = [0.26862954, 0.26130258, 0.27577711]
        else:
            mean = [0.5, 0.5, 0.5]
            std = [0.5, 0.5, 0.5]
        return image_tensor_from_sips(image_path, tmpdir, self.image_size, self.torch, self.device, mean, std)

    def embed(self, image_path: Path) -> list[float]:
        torch = self.torch
        with tempfile.TemporaryDirectory(prefix=f"jewelry-{self.provider_name}-") as tmp:
            tensor = self.image_tensor(image_path, Path(tmp))
            with torch.no_grad():
                if hasattr(self.model, "get_image_features"):
                    vector = self.model.get_image_features(pixel_values=tensor)
                else:
                    output = self.model(pixel_values=tensor)
                    vector = output.pooler_output if getattr(output, "pooler_output", None) is not None else output.last_hidden_state[:, 0]
                vector = vector.squeeze(0).detach().cpu().float().tolist()
        return normalize_vector([float(value) for value in vector])


def image_tensor_from_sips(
    image_path: Path,
    tmpdir: Path,
    image_size: int,
    torch,
    device: str,
    mean_values: list[float],
    std_values: list[float],
):
    resized = tmpdir / f"{hashlib.md5(str(image_path).encode()).hexdigest()}-{image_size}.bmp"
    result = run_sips(["-Z", str(image_size), "-s", "format", "bmp", str(image_path), "--out", str(resized)])
    if result.returncode != 0 or not resized.exists():
        raise RuntimeError(f"sips failed to prepare image for embedding: {image_path}")
    width, height, pixels = parse_bmp_pixels(resized)
    image = torch.tensor(pixels, dtype=torch.float32).view(height, width, 3).permute(2, 0, 1) / 255.0
    canvas = torch.ones((3, image_size, image_size), dtype=torch.float32)
    top = max((image_size - height) // 2, 0)
    left = max((image_size - width) // 2, 0)
    canvas[:, top : top + height, left : left + width] = image[:, :image_size, :image_size]
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
    raise ValueError(f"unknown provider: {args.provider}")


def load_embedding_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def embedding_cache_key(provider: EmbeddingProvider, image_path: Path, view: str) -> tuple[str, str]:
    image_hash = sha256(image_path)
    return f"{provider.provider_id}|{view}|{image_hash}", image_hash


def embed_assets(
    assets: list[VisualAsset],
    provider: EmbeddingProvider,
    out_dir: Path,
) -> tuple[dict[str, list[float]], list[dict]]:
    embeddings_dir = out_dir / "embeddings"
    embeddings_dir.mkdir(parents=True, exist_ok=True)
    cache_path = embeddings_dir / "embedding_cache.json"
    cache = load_embedding_cache(cache_path)
    vectors: dict[str, list[float]] = {}
    records: list[dict] = []

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


def all_pair_scores(assets: list[VisualAsset], vectors: dict[str, list[float]]) -> list[dict]:
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


def write_pair_scores_csv(path: Path, rows: list[dict]) -> None:
    fields = ["source_asset_id", "target_asset_id", "score", "source_view", "target_view"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "score": f"{row['score']:.6f}"})


def cluster_from_scores(assets: list[VisualAsset], scores: list[dict], threshold: float) -> list[dict]:
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


def cluster_lookup(clusters: list[dict]) -> dict[str, str]:
    lookup = {}
    for cluster in clusters:
        for asset_id in cluster["asset_ids"]:
            lookup[asset_id] = cluster["cluster_id"]
    return lookup


def benchmark_clusters(assets: list[VisualAsset], clusters: list[dict]) -> dict:
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


def merge_error_rows(clusters: list[dict], labels: dict[str, set[str]]) -> list[dict]:
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


def split_error_rows(clusters: list[dict], labels: dict[str, set[str]]) -> list[dict]:
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


def threshold_sweep(assets: list[VisualAsset], scores: list[dict], thresholds: list[float]) -> list[dict]:
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


def choose_threshold(rows: list[dict], min_precision: float) -> float:
    eligible = [row for row in rows if row["precision"] >= min_precision]
    if eligible:
        best = sorted(eligible, key=lambda row: (row["recall"], row["f1"], row["threshold"]), reverse=True)[0]
    else:
        useful = [row for row in rows if row.get("predicted_positive", 0) > 0]
        candidates = useful or rows
        best = sorted(candidates, key=lambda row: (row["precision"], row["recall"], row["f1"], row["threshold"]), reverse=True)[0]
    return float(best["threshold"])


def write_threshold_sweep_csv(path: Path, rows: list[dict]) -> None:
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


def benchmark_report_markdown(provider_id: str, threshold: float, sweep: list[dict], benchmark: dict) -> str:
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


def write_cluster_review_sheet(path: Path, title: str, clusters: list[dict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    cards = []
    for cluster in clusters:
        figures = []
        for asset_id in cluster["asset_ids"]:
            asset = lookup[asset_id]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset_id}-{hashlib.md5(asset.preferred_path.encode()).hexdigest()}.jpg"
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


def write_clustering_review_sheets(out_dir: Path, clusters: list[dict], assets: list[VisualAsset], benchmark: dict) -> None:
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


def pair_diagnostics(assets: list[VisualAsset], scores: list[dict], threshold: float, limit: int = 80) -> dict[str, list[dict]]:
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


def product_same_decision(label: str) -> bool:
    return label in PRODUCT_SAME_DECISIONS


def non_product_same_decision(label: str) -> bool:
    return label in DESIGN_SAME_DECISIONS or label in DIFFERENT_DECISIONS or label == "unsure"


def decision_lookup_by_pair(decisions: dict[str, dict]) -> dict[str, dict]:
    lookup = {}
    for key, decision in decisions.items():
        pair_key = decision.get("pair_key") or key
        if "--" not in pair_key:
            continue
        lookup[pair_key] = decision
    return lookup


def candidate_pairs(assets: list[VisualAsset], scores: list[dict], threshold: float, top_k: int = 0) -> list[dict]:
    labels = asset_label_map(assets)
    by_key: dict[str, dict] = {}
    neighbors: dict[str, list[dict]] = defaultdict(list)
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
        for asset_id, rows in neighbors.items():
            for row in sorted(rows, key=lambda item: item["score"], reverse=True)[:top_k]:
                add_candidate_pair(by_key, row, labels, threshold, [f"top_{top_k}_neighbor"])

    return sorted(by_key.values(), key=lambda item: item["score"], reverse=True)


def add_candidate_pair(
    by_key: dict[str, dict],
    row: dict,
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


def candidate_coverage_report(pairs: list[dict], assets: list[VisualAsset]) -> dict:
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


def write_pair_review_sheet(path: Path, title: str, pairs: list[dict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    rows = []
    for pair in pairs:
        figures = []
        for key in ("source_asset_id", "target_asset_id"):
            asset = lookup[pair[key]]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset.asset_id}-{hashlib.md5(asset.preferred_path.encode()).hexdigest()}.jpg"
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


def write_pair_diagnostics(out_dir: Path, diagnostics: dict[str, list[dict]], assets: list[VisualAsset]) -> None:
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


def write_candidate_pairs(out_dir: Path, pairs: list[dict], assets: list[VisualAsset]) -> None:
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


def candidate_pair_key(pair: dict) -> str:
    return "--".join(sorted([pair["source_asset_id"], pair["target_asset_id"]]))


def load_candidate_pairs(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"candidate pair file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("candidate pair file must contain a JSON list")
    return payload


def load_ai_decision_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def benchmark_ai_decisions(pairs: list[dict], decisions: dict[str, dict]) -> dict:
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


def ai_benchmark_markdown(model: str, benchmark: dict) -> str:
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


def image_data_url_for_api(path: Path, tmpdir: Path, max_size: int) -> str:
    out = tmpdir / f"{hashlib.md5(str(path).encode()).hexdigest()}-{max_size}.jpg"
    result = run_sips(["-Z", str(max_size), "-s", "format", "jpeg", str(path), "--out", str(out)])
    if result.returncode != 0 or not out.exists():
        raise RuntimeError(f"failed to prepare API image: {path}")
    encoded = base64.b64encode(out.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def ai_decision_cache_key(pair: dict, left: VisualAsset, right: VisualAsset, model: str, max_image_size: int) -> str:
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
    if isinstance(exc, (urllib.error.URLError, TimeoutError)):
        return True
    return False


def parse_ai_decision_text(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    payload = json.loads(cleaned)
    decision = payload.get("decision")
    if decision not in VALID_AI_DECISIONS:
        raise ValueError(f"invalid AI decision: {decision}")
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
) -> dict:
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
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if retryable_api_error(exc):
            raise
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {message}") from exc
    text = payload["choices"][0]["message"]["content"]
    parsed = parse_ai_decision_text(text)
    parsed["raw_response"] = payload
    return parsed


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


def write_ai_review_sheet(path: Path, title: str, rows: list[dict], assets: list[VisualAsset], out_dir: Path) -> None:
    thumbs_dir = out_dir / "review_sheets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    lookup = asset_by_id(assets)
    cards = []
    for row in rows:
        figures = []
        for key in ("source_asset_id", "target_asset_id"):
            asset = lookup[row[key]]
            source = Path(asset.preferred_path)
            thumb = thumbs_dir / f"{asset.asset_id}-{hashlib.md5(asset.preferred_path.encode()).hexdigest()}.jpg"
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


def write_ai_adjudication_outputs(out_dir: Path, assets: list[VisualAsset], model: str, decisions: dict[str, dict], benchmark: dict) -> None:
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


def decision_asset_ids(decision: dict) -> tuple[str, str] | None:
    left = decision.get("source_asset_id")
    right = decision.get("target_asset_id")
    if not left or not right:
        pair_key = decision.get("pair_key", "")
        if "--" in pair_key:
            left, right = pair_key.split("--", 1)
    if not left or not right:
        return None
    return str(left), str(right)


def pair_edge_record(decision: dict) -> dict:
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


def build_ai_cluster_export(assets: list[VisualAsset], decisions: dict[str, dict], allow_design_variants: bool = True) -> dict:
    asset_ids = [asset.asset_id for asset in assets]
    product_uf = UnionFind(asset_ids)
    usable_decisions = []
    for key, decision in decisions.items():
        asset_pair = decision_asset_ids({**decision, "pair_key": decision.get("pair_key", key)})
        if not asset_pair:
            continue
        left, right = asset_pair
        if left not in product_uf.parent or right not in product_uf.parent:
            continue
        label = decision.get("decision")
        record = {**decision, "pair_key": key, "source_asset_id": left, "target_asset_id": right}
        usable_decisions.append(record)
        if product_same_decision(label):
            product_uf.union(left, right)

    by_root: dict[str, list[str]] = defaultdict(list)
    for asset_id in asset_ids:
        by_root[product_uf.find(asset_id)].append(asset_id)

    product_clusters = []
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

    product_by_id = {cluster["cluster_id"]: cluster for cluster in product_clusters}
    design_uf = UnionFind([cluster["cluster_id"] for cluster in product_clusters])
    product_edges = []
    design_edges = []
    negative_edges = []
    uncertain_edges = []
    cross_product_edges = []

    for decision in usable_decisions:
        left, right = decision["source_asset_id"], decision["target_asset_id"]
        left_product = asset_to_product[left]
        right_product = asset_to_product[right]
        label = decision.get("decision", "missing")
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
        flags = []
        inside_labels = {edge["decision"] for edge in cluster["non_product_edges_inside"]}
        if inside_labels.intersection(DIFFERENT_DECISIONS):
            flags.append("negative_edge_inside_product_cluster")
        if inside_labels.intersection(DESIGN_SAME_DECISIONS):
            flags.append("design_variant_edge_inside_product_cluster")
        if "unsure" in inside_labels:
            flags.append("unsure_edge_inside_product_cluster")
        if any(edge.get("review_required") for edge in cluster["positive_edges"] + cluster["non_product_edges_inside"]):
            flags.append("ai_review_required_edge")
        cluster["review_flags"] = flags
        cluster["review_required"] = bool(flags)

    design_roots: dict[str, list[str]] = defaultdict(list)
    for cluster in product_clusters:
        design_roots[design_uf.find(cluster["cluster_id"])].append(cluster["cluster_id"])

    design_clusters = []
    product_to_design = {}
    for index, product_ids in enumerate(sorted((sorted(ids) for ids in design_roots.values()), key=lambda ids: ids[0]), start=1):
        cluster_id = f"D{index:04d}"
        for product_id in product_ids:
            product_to_design[product_id] = cluster_id
        cluster_asset_ids = sorted(asset_id for product_id in product_ids for asset_id in product_by_id[product_id]["asset_ids"])
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


def write_assignments_csv(path: Path, assignments: list[dict]) -> None:
    fields = ["asset_id", "product_cluster_id", "design_cluster_id", "preferred_path", "quality_path", "reference_cluster_ids"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in assignments:
            writer.writerow({**row, "reference_cluster_ids": "|".join(row["reference_cluster_ids"])})


def cluster_export_markdown(export: dict) -> str:
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


def write_review_queue_sheet(path: Path, review_queue: list[dict], assets: list[VisualAsset], out_dir: Path) -> None:
    clusters = [
        {"cluster_id": item["cluster_id"], "asset_ids": item["asset_ids"], "size": len(item["asset_ids"])}
        for item in review_queue
    ]
    write_cluster_review_sheet(path, "AI Cluster Review Queue", clusters, assets, out_dir)


def write_cluster_export_outputs(out_dir: Path, assets: list[VisualAsset], export: dict) -> None:
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


def same_filename_different_hash(occurrences: list[Occurrence]) -> list[dict]:
    by_name: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_name[occurrence.filename].append(occurrence)
    rows = []
    for filename, group in sorted(by_name.items()):
        hashes = sorted(set(occurrence.sha256 for occurrence in group))
        sources = sorted(set(occurrence.source for occurrence in group))
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
    assets: list[dict],
    filename_conflicts: list[dict],
    edit_duplicate_matches: list[dict] | None = None,
) -> str:
    source_counts = Counter(occurrence.source for occurrence in occurrences)
    kind_counts = Counter((occurrence.source, occurrence.kind) for occurrence in occurrences)
    preferred_source_counts = Counter(asset["occurrences"][0]["source"] for asset in assets)
    flag_counts = Counter(flag for asset in assets for flag in asset["flags"])
    group_sizes = Counter(len(asset["occurrences"]) for asset in assets)
    reference_clusters = sorted(set(label for asset in assets for label in asset["reference_cluster_ids"]))
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
    preferred_by_source = Counter()
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = run_sips(["-Z", "220", "-s", "format", "jpeg", str(source), "--out", str(destination)])
    return result.returncode == 0 and destination.exists()


def thumbnail_name(occurrence: dict) -> str:
    digest = hashlib.md5(occurrence["path"].encode("utf-8")).hexdigest()
    return f"{occurrence['occurrence_id']}-{digest}.jpg"


def write_review_sheet(path: Path, title: str, assets: list[dict], out_dir: Path, max_assets: int = 120) -> None:
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
    edit_duplicate_matches: list[dict],
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
    assets: list[dict],
    filename_conflicts: list[dict],
    occurrences: list[Occurrence],
    edit_duplicate_matches: list[dict],
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
    if shutil.which("sips") is None:
        print("ERROR: sips is required on PATH for image metadata and hashes", file=sys.stderr)
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


def parse_thresholds(raw: str) -> list[float]:
    thresholds = [float(item.strip()) for item in raw.split(",") if item.strip()]
    if not thresholds:
        raise ValueError("provide at least one threshold")
    for threshold in thresholds:
        if threshold < -1 or threshold > 1:
            raise ValueError(f"threshold outside cosine similarity range [-1, 1]: {threshold}")
    return sorted(set(thresholds))


def cluster_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        assets = load_manifest(Path(args.manifest).resolve())
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
                    raise RuntimeError(f"failed to judge {pair_key}")
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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
