#!/usr/bin/env python3
"""Build a consolidated HTML review page for a clustering benchmark."""

from __future__ import annotations

import argparse
import csv
import html
import json
from itertools import combinations
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def split_labels(raw: str) -> list[str]:
    return [item for item in raw.split("|") if item]


def numeric_label_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def html_escape(value: Any) -> str:
    return html.escape(str(value))


def load_assets(manifest_path: Path) -> dict[str, JsonDict]:
    assets = {}
    with manifest_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            labels = split_labels(row.get("reference_cluster_ids", ""))
            assets[str(row["asset_id"])] = {
                **row,
                "labels": labels,
                "label": labels[0] if labels else "",
            }
    return assets


def cluster_lookup(clusters: list[JsonDict]) -> dict[str, str]:
    return {
        str(asset_id): str(cluster["cluster_id"])
        for cluster in clusters
        for asset_id in cluster.get("asset_ids", [])
    }


def score_lookup(pairs: list[JsonDict]) -> dict[tuple[str, str], float]:
    return {
        tuple(sorted([str(row["source_asset_id"]), str(row["target_asset_id"])])): float(row["score"])
        for row in pairs
    }


def thumb_lookup(benchmark_dir: Path) -> dict[str, str]:
    thumbs_dir = benchmark_dir / "review_sheets" / "thumbs"
    if not thumbs_dir.exists():
        return {}
    return {path.name.split("-", 1)[0]: f"thumbs/{path.name}" for path in thumbs_dir.glob("*.jpg")}


def classify_pairs(
    assets: dict[str, JsonDict],
    clusters: list[JsonDict],
    pairs: list[JsonDict],
) -> dict[str, list[JsonDict]]:
    asset_cluster = cluster_lookup(clusters)
    scores = score_lookup(pairs)
    rows: dict[str, list[JsonDict]] = {
        "correct": [],
        "missed": [],
        "wrong": [],
        "nonmatch": [],
    }
    for left, right in combinations(sorted(assets), 2):
        left_labels = set(assets[left]["labels"])
        right_labels = set(assets[right]["labels"])
        truth_same = bool(left_labels and right_labels and left_labels.intersection(right_labels))
        predicted_same = asset_cluster.get(left) == asset_cluster.get(right)
        if truth_same and predicted_same:
            kind = "correct"
        elif truth_same:
            kind = "missed"
        elif predicted_same:
            kind = "wrong"
        else:
            kind = "nonmatch"
        rows[kind].append(
            {
                "left": left,
                "right": right,
                "score": scores.get(tuple(sorted([left, right])), 0.0),
            }
        )
    rows["correct"].sort(key=lambda row: (-float(row["score"]), str(row["left"]), str(row["right"])))
    rows["missed"].sort(key=lambda row: (numeric_label_key(str(assets[str(row["left"])]["label"])), -float(row["score"])))
    rows["wrong"].sort(key=lambda row: (-float(row["score"]), str(row["left"]), str(row["right"])))
    rows["nonmatch"].sort(key=lambda row: -float(row["score"]))
    return rows


def asset_card(asset_id: str, assets: dict[str, JsonDict], thumbs: dict[str, str]) -> str:
    asset = assets[asset_id]
    image_src = thumbs.get(asset_id, str(asset["preferred_path"]))
    return (
        '<figure class="asset-card">'
        f'<img src="{html_escape(image_src)}" alt="{html_escape(asset_id)}">'
        "<figcaption>"
        f"<b>{html_escape(asset_id)}</b><br>"
        f"folder {html_escape(asset['label'])}<br>"
        f"{html_escape(Path(str(asset['preferred_path'])).name)}<br>"
        f'<a href="{html_escape(asset["preferred_path"])}">original</a>'
        "</figcaption></figure>"
    )


def pair_card(row: JsonDict, kind: str, assets: dict[str, JsonDict], thumbs: dict[str, str], threshold: float) -> str:
    labels = {
        "correct": ("Correct match", "ok", "Same folder and clustered together."),
        "missed": ("Missed match", "miss", "Same folder, but split into different clusters."),
        "wrong": ("Wrong merge", "bad", "Different folders, but clustered together."),
        "nonmatch": ("Correct non-match", "neutral", "Different folders and kept separate."),
    }
    title, css_class, explanation = labels[kind]
    left = str(row["left"])
    right = str(row["right"])
    return (
        f'<article class="pair {css_class}">'
        '<div class="head">'
        f"<b>{html_escape(title)}</b>"
        f"<span>similarity {float(row['score']):.4f}</span>"
        f"<span>threshold {threshold:.4f}</span>"
        "</div>"
        f'<div class="imgs">{asset_card(left, assets, thumbs)}{asset_card(right, assets, thumbs)}</div>'
        f"<p>{html_escape(explanation)}</p>"
        "</article>"
    )


def section(
    title: str,
    description: str,
    rows: list[JsonDict],
    kind: str,
    empty: str,
    assets: dict[str, JsonDict],
    thumbs: dict[str, str],
    threshold: float,
) -> str:
    body = "".join(pair_card(row, kind, assets, thumbs, threshold) for row in rows) if rows else f'<p class="empty">{html_escape(empty)}</p>'
    return (
        f"<section><h2>{html_escape(title)} <span>{len(rows)}</span></h2>"
        f"<p>{html_escape(description)}</p>"
        f'<div class="grid">{body}</div></section>'
    )


def label_summary(assets: dict[str, JsonDict]) -> str:
    by_label: dict[str, int] = {}
    for asset in assets.values():
        label = str(asset["label"])
        by_label[label] = by_label.get(label, 0) + 1
    items = sorted(by_label.items(), key=lambda item: numeric_label_key(item[0]))
    return "".join(
        f"<li><b>{html_escape(label)}</b><span>{count} image{'s' if count != 1 else ''}</span></li>"
        for label, count in items
    )


def build_review_html(
    manifest_path: Path,
    benchmark_dir: Path,
    output_path: Path,
    closest_nonmatches: int,
) -> JsonDict:
    assets = load_assets(manifest_path)
    benchmark = load_json(benchmark_dir / "benchmark_report.json")
    predicted = load_json(benchmark_dir / "predicted_clusters.json")
    pairs = load_json(benchmark_dir / "similarity_pairs.json")
    threshold = float(predicted["threshold"])
    classified = classify_pairs(assets, predicted["clusters"], pairs)
    nonmatches = classified["nonmatch"][:closest_nonmatches]
    thumbs = thumb_lookup(benchmark_dir)

    sections = "".join(
        [
            section(
                "Correct same-product matches",
                "Pairs from the same numbered folder that clustered together.",
                classified["correct"],
                "correct",
                "No correct matches.",
                assets,
                thumbs,
                threshold,
            ),
            section(
                "Missed same-product matches",
                "Pairs from the same numbered folder that should have matched but were split.",
                classified["missed"],
                "missed",
                "No missed matches.",
                assets,
                thumbs,
                threshold,
            ),
            section(
                "Wrong cross-folder merges",
                "Different numbered folders incorrectly clustered together.",
                classified["wrong"],
                "wrong",
                "None. No wrong merges at this threshold.",
                assets,
                thumbs,
                threshold,
            ),
            section(
                "Closest correct non-matches",
                "Highest-similarity different-folder pairs that still stayed separate.",
                nonmatches,
                "nonmatch",
                "No non-matches to show.",
                assets,
                thumbs,
                threshold,
            ),
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jewelry Benchmark Review</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#fbfbf8;color:#17211b}}header{{position:sticky;top:0;background:white;border-bottom:1px solid #d9ded8;padding:24px 32px;z-index:2}}h1{{margin:0 0 14px;font-size:28px;letter-spacing:0}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;max-width:1100px}}.metric{{border:1px solid #d9ded8;border-radius:8px;padding:10px;background:white}}.metric b{{display:block;font-size:22px}}.metric span,section>p,figcaption,.head span{{color:#5d6a63}}main{{padding:24px 32px 48px}}.labels ul{{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:6px;list-style:none;padding:0;max-width:1100px}}.labels li{{border:1px solid #d9ded8;background:white;border-radius:6px;padding:8px;display:flex;justify-content:space-between}}section{{margin:32px 0}}h2{{margin:0 0 4px;font-size:22px}}h2 span{{color:#5d6a63;font-weight:500}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(430px,1fr));gap:14px}}.pair{{background:white;border:1px solid #d9ded8;border-left:5px solid #7b8580;border-radius:8px;padding:12px}}.pair.ok{{border-left-color:#176b42}}.pair.miss{{border-left-color:#9a5b00}}.pair.bad{{border-left-color:#9f2323}}.head{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;font-size:13px}}.head b{{color:white;background:#46524b;border-radius:999px;padding:3px 9px}}.ok .head b{{background:#176b42}}.miss .head b{{background:#9a5b00}}.bad .head b{{background:#9f2323}}.imgs{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}figure{{margin:0}}img{{width:100%;aspect-ratio:1/1;object-fit:contain;background:#eef3ee;border:1px solid #d9ded8;border-radius:6px}}figcaption{{font-size:12px;line-height:1.35;overflow-wrap:anywhere;padding-top:5px}}figcaption b{{color:#17211b}}a{{color:#155c9c}}.empty{{border:1px dashed #d9ded8;border-radius:8px;background:white;padding:18px;color:#5d6a63}}@media(max-width:720px){{header,main{{padding-left:14px;padding-right:14px}}.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<header>
<h1>Jewelry Benchmark Review</h1>
<div class="metrics">
<div class="metric"><b>{benchmark['asset_count']}</b><span>images</span></div>
<div class="metric"><b>{len({str(asset['label']) for asset in assets.values()})}</b><span>folders</span></div>
<div class="metric"><b>{threshold:.2f}</b><span>threshold</span></div>
<div class="metric"><b>{float(benchmark['precision']):.3f}</b><span>precision</span></div>
<div class="metric"><b>{float(benchmark['recall']):.3f}</b><span>recall</span></div>
<div class="metric"><b>{float(benchmark['f1']):.3f}</b><span>F1</span></div>
<div class="metric"><b>{len(classified['correct'])}</b><span>correct matches</span></div>
<div class="metric"><b>{len(classified['missed'])}</b><span>missed matches</span></div>
<div class="metric"><b>{len(classified['wrong'])}</b><span>wrong merges</span></div>
</div>
</header>
<main>
<div class="labels"><h2>Ground Truth Folders</h2><ul>{label_summary(assets)}</ul></div>
{sections}
</main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return {
        "output": str(output_path),
        "correct": len(classified["correct"]),
        "missed": len(classified["missed"]),
        "wrong": len(classified["wrong"]),
        "nonmatches_shown": len(nonmatches),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build consolidated benchmark review HTML")
    parser.add_argument("--manifest", required=True, help="normalized manifest.csv")
    parser.add_argument("--benchmark", required=True, help="benchmark output directory")
    parser.add_argument("--out", required=True, help="HTML output path")
    parser.add_argument("--closest-nonmatches", type=int, default=40, help="number of high-similarity true non-matches to show")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = build_review_html(
        manifest_path=Path(args.manifest).resolve(),
        benchmark_dir=Path(args.benchmark).resolve(),
        output_path=Path(args.out).resolve(),
        closest_nonmatches=args.closest_nonmatches,
    )
    print(f"Review HTML: {summary['output']}")
    print(f"Correct matches: {summary['correct']}")
    print(f"Missed matches: {summary['missed']}")
    print(f"Wrong merges: {summary['wrong']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
