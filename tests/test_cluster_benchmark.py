import csv
import json
import tempfile
import unittest
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from tools import build_benchmark_review as bbr
from tools import jewelry_cluster_benchmark as jcb


def asset(asset_id: str, labels: list[str]) -> jcb.VisualAsset:
    return jcb.VisualAsset(
        asset_id=asset_id,
        preferred_path="/tmp/missing.jpg",
        quality_path="/tmp/missing.jpg",
        reference_cluster_ids=labels,
        sources=["reference"],
        kinds=["web"],
        shot_keys=[],
        confidence=1.0,
        flags=[],
        occurrence_count=1,
    )


def occurrence(occurrence_id: str, source: str, kind: str, ahash: str, dhash: str) -> jcb.Occurrence:
    return jcb.Occurrence(
        occurrence_id=occurrence_id,
        source=source,
        path=f"/tmp/{occurrence_id}.jpg",
        rel_path=f"{occurrence_id}.jpg",
        filename=f"{occurrence_id}.jpg",
        extension=".jpg",
        kind=kind,
        reference_cluster_id="",
        is_before_fix=False,
        size_bytes=100,
        width=1500,
        height=1500,
        sha256=occurrence_id,
        ahash=ahash,
        dhash=dhash,
        shot_key="",
    )


class ClusterBenchmarkTests(unittest.TestCase):
    def test_connected_assets_become_cluster(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        scores = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "score": 0.91},
            {"source_asset_id": "A1", "target_asset_id": "A3", "score": 0.20},
            {"source_asset_id": "A2", "target_asset_id": "A3", "score": 0.25},
        ]

        clusters = jcb.cluster_from_scores(assets, scores, threshold=0.9)
        cluster_sizes = sorted(cluster["size"] for cluster in clusters)

        assert cluster_sizes == [1, 2]

    def test_benchmark_counts_precision_and_recall(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        clusters = [
            {"cluster_id": "C1", "asset_ids": ["A1", "A2"], "size": 2},
            {"cluster_id": "C2", "asset_ids": ["A3"], "size": 1},
        ]

        benchmark = jcb.benchmark_clusters(assets, clusters)

        assert benchmark["true_positive"] == 1
        assert benchmark["false_positive"] == 0
        assert benchmark["false_negative"] == 0
        assert benchmark["precision"] == 1.0
        assert benchmark["recall"] == 1.0

    def test_build_benchmark_review_counts_pair_classes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = base / "manifest.csv"
            image_paths = {}
            for asset_id in ("A1", "A2", "A3"):
                image_path = base / f"{asset_id}.jpg"
                Image.new("RGB", (16, 16), "white").save(image_path)
                image_paths[asset_id] = image_path
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
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
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "asset_id": "A1",
                        "preferred_path": image_paths["A1"],
                        "quality_path": image_paths["A1"],
                        "reference_cluster_ids": "1",
                        "sources": "reference",
                        "kinds": "web",
                        "shot_keys": "",
                        "confidence": "1",
                        "flags": "",
                        "occurrence_count": "1",
                    }
                )
                writer.writerow(
                    {
                        "asset_id": "A2",
                        "preferred_path": image_paths["A2"],
                        "quality_path": image_paths["A2"],
                        "reference_cluster_ids": "1",
                        "sources": "reference",
                        "kinds": "web",
                        "shot_keys": "",
                        "confidence": "1",
                        "flags": "",
                        "occurrence_count": "1",
                    }
                )
                writer.writerow(
                    {
                        "asset_id": "A3",
                        "preferred_path": image_paths["A3"],
                        "quality_path": image_paths["A3"],
                        "reference_cluster_ids": "2",
                        "sources": "reference",
                        "kinds": "web",
                        "shot_keys": "",
                        "confidence": "1",
                        "flags": "",
                        "occurrence_count": "1",
                    }
                )
            benchmark_dir = base / "benchmark"
            benchmark_dir.mkdir()
            (benchmark_dir / "benchmark_report.json").write_text(
                json.dumps(
                    {
                        "asset_count": 3,
                        "precision": 1.0,
                        "recall": 1.0,
                        "f1": 1.0,
                    }
                ),
                encoding="utf-8",
            )
            (benchmark_dir / "predicted_clusters.json").write_text(
                json.dumps(
                    {
                        "threshold": 0.89,
                        "clusters": [
                            {"cluster_id": "C1", "asset_ids": ["A1", "A2"]},
                            {"cluster_id": "C2", "asset_ids": ["A3"]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (benchmark_dir / "similarity_pairs.json").write_text(
                json.dumps(
                    [
                        {"source_asset_id": "A1", "target_asset_id": "A2", "score": 0.91},
                        {"source_asset_id": "A1", "target_asset_id": "A3", "score": 0.20},
                        {"source_asset_id": "A2", "target_asset_id": "A3", "score": 0.30},
                    ]
                ),
                encoding="utf-8",
            )

            output = benchmark_dir / "review_sheets" / "00_truth_mistakes_overview.html"
            summary = bbr.build_review_html(manifest, benchmark_dir, output, closest_nonmatches=2)

            html = output.read_text(encoding="utf-8")
            assert summary["correct"] == 1
            assert summary["missed"] == 0
            assert summary["wrong"] == 0
            assert summary["nonmatches_shown"] == 2
            assert "Correct match" in html
            assert "Wrong cross-folder merges" in html

    def test_conservative_threshold_prefers_precision_then_recall(self) -> None:
        rows = [
            {"threshold": 0.80, "precision": 0.90, "recall": 0.95, "f1": 0.92, "predicted_positive": 20},
            {"threshold": 0.86, "precision": 0.99, "recall": 0.55, "f1": 0.70, "predicted_positive": 10},
            {"threshold": 0.92, "precision": 1.00, "recall": 0.50, "f1": 0.66, "predicted_positive": 8},
        ]

        assert jcb.choose_threshold(rows, min_precision=0.98) == 0.86

    def test_threshold_fallback_still_prefers_precision(self) -> None:
        rows = [
            {"threshold": 0.80, "precision": 0.60, "recall": 0.95, "f1": 0.73, "predicted_positive": 20},
            {"threshold": 0.92, "precision": 0.90, "recall": 0.40, "f1": 0.55, "predicted_positive": 6},
        ]

        assert jcb.choose_threshold(rows, min_precision=0.98) == 0.92

    def test_parse_thresholds_sorts_and_dedupes(self) -> None:
        assert jcb.parse_thresholds("0.9,0.8,0.9") == [0.8, 0.9]

    def test_ai_benchmark_counts_unsure_same_as_missed_recall(self) -> None:
        pairs = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "benchmark_same_reference": True},
            {"source_asset_id": "A1", "target_asset_id": "A3", "benchmark_same_reference": False},
            {"source_asset_id": "A2", "target_asset_id": "A3", "benchmark_same_reference": True},
        ]
        decisions = {
            "A1--A2": {"decision": "same_physical_product"},
            "A1--A3": {"decision": "different_design"},
            "A2--A3": {"decision": "unsure"},
        }

        benchmark = jcb.benchmark_ai_decisions(pairs, decisions)

        assert benchmark["true_positive"] == 1
        assert benchmark["false_positive"] == 0
        assert benchmark["false_negative"] == 1
        assert benchmark["true_negative"] == 1
        assert benchmark["precision"] == 1.0
        assert benchmark["recall"] == 0.5

    def test_parse_ai_decision_text_validates_label(self) -> None:
        parsed = jcb.parse_ai_decision_text('{"decision":"same_physical_product","confidence":0.8,"reason":"same stone"}')

        assert parsed["decision"] == "same_physical_product"
        assert parsed["confidence"] == 0.8

    def test_same_design_variant_is_not_product_positive(self) -> None:
        pairs = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "benchmark_same_reference": True},
            {"source_asset_id": "A1", "target_asset_id": "A3", "benchmark_same_reference": False},
        ]
        decisions = {
            "A1--A2": {"decision": "same_design_variant"},
            "A1--A3": {"decision": "same_design_variant"},
        }

        benchmark = jcb.benchmark_ai_decisions(pairs, decisions)

        assert benchmark["true_positive"] == 0
        assert benchmark["false_positive"] == 0
        assert benchmark["false_negative"] == 1
        assert benchmark["true_negative"] == 1
        assert benchmark["design_variant"] == 2

    def test_candidate_pairs_include_top_k_neighbors(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        scores = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "score": 0.50, "source_view": "full", "target_view": "full"},
            {"source_asset_id": "A1", "target_asset_id": "A3", "score": 0.40, "source_view": "full", "target_view": "full"},
            {"source_asset_id": "A2", "target_asset_id": "A3", "score": 0.30, "source_view": "full", "target_view": "full"},
        ]

        candidates = jcb.candidate_pairs(assets, scores, threshold=0.9, top_k=1)
        keys = {jcb.candidate_pair_key(pair) for pair in candidates}

        assert "A1--A2" in keys
        assert "A1--A3" in keys

    def test_edit_duplicate_pairs_require_mutual_nearest(self) -> None:
        fixed = occurrence("F1", "fixed", "web", "0000000000000000", "0000000000000000")
        best = occurrence("U1", "unfixed", "web", "0000000000000001", "0000000000000000")
        farther = occurrence("U2", "unfixed", "web", "000000000000000f", "0000000000000000")

        pairs = jcb.edited_duplicate_pairs([fixed, best, farther], max_distance=3)

        assert len(pairs) == 1
        assert pairs[0]["source_occurrence_id"] == "F1"
        assert pairs[0]["target_occurrence_id"] == "U1"

    def test_normalize_assets_can_collapse_edit_duplicates(self) -> None:
        fixed = occurrence("F1", "fixed", "web", "0000000000000000", "0000000000000000")
        unfixed = occurrence("U1", "unfixed", "web", "0000000000000001", "0000000000000000")

        assets, occurrence_to_asset, edit_pairs = jcb.normalize_assets([fixed, unfixed], edit_dedup_distance=3)

        assert len(assets) == 1
        assert occurrence_to_asset["F1"] == occurrence_to_asset["U1"]
        assert len(edit_pairs) == 1

    def test_build_ai_cluster_export_separates_product_and_design(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"]), asset("A4", ["R3"])]
        decisions = {
            "A1--A2": {
                "source_asset_id": "A1",
                "target_asset_id": "A2",
                "decision": "same_physical_product",
                "confidence": 0.95,
            },
            "A2--A3": {
                "source_asset_id": "A2",
                "target_asset_id": "A3",
                "decision": "same_design_variant",
                "confidence": 0.9,
            },
            "A1--A4": {
                "source_asset_id": "A1",
                "target_asset_id": "A4",
                "decision": "different_design",
                "confidence": 0.99,
            },
        }

        export = jcb.build_ai_cluster_export(assets, decisions)

        product_sizes = sorted(cluster["size"] for cluster in export["product_clusters"])
        design_sizes = sorted(cluster["size"] for cluster in export["design_clusters"])
        assert product_sizes == [1, 1, 2]
        assert design_sizes == [1, 3]
        assert export["summary"]["review_queue_count"] == 0

    def test_build_ai_cluster_export_can_disable_design_variants(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R2"])]
        decisions = {
            "A1--A2": {
                "source_asset_id": "A1",
                "target_asset_id": "A2",
                "decision": "same_design_variant",
                "confidence": 0.95,
            }
        }

        export = jcb.build_ai_cluster_export(assets, decisions, allow_design_variants=False)

        assert len(export["design_clusters"]) == 2
        assert export["summary"]["design_edge_count"] == 0
        assert export["summary"]["negative_edge_count"] == 1

    def test_build_ai_cluster_export_flags_transitive_contradiction(self) -> None:
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R1"])]
        decisions = {
            "A1--A2": {"source_asset_id": "A1", "target_asset_id": "A2", "decision": "same_physical_product"},
            "A2--A3": {"source_asset_id": "A2", "target_asset_id": "A3", "decision": "same_physical_product"},
            "A1--A3": {"source_asset_id": "A1", "target_asset_id": "A3", "decision": "different_design"},
        }

        export = jcb.build_ai_cluster_export(assets, decisions)

        assert len(export["product_clusters"]) == 0
        assert len(export["blocked_product_clusters"]) == 1
        assert export["summary"]["review_queue_count"] == 1
        assert "negative_edge_inside_product_cluster" in export["blocked_product_clusters"][0]["review_flags"]

    def test_ai_cache_key_changes_with_prompt_and_image_inputs(self) -> None:
        left = asset("A1", ["R1"])
        right = asset("A2", ["R1"])
        pair = {"source_asset_id": "A1", "target_asset_id": "A2"}
        left.preferred_path = __file__
        right.preferred_path = __file__

        key_small = jcb.ai_decision_cache_key(pair, left, right, "gpt-4.1-mini", 512)
        key_large = jcb.ai_decision_cache_key(pair, left, right, "gpt-4.1-mini", 768)

        assert jcb.AI_PAIR_PROMPT_VERSION in key_small
        assert key_small != key_large

    def test_decision_lookup_by_pair_supports_versioned_cache_keys(self) -> None:
        decisions = {
            "A1--A2|model|prompt|hash": {"pair_key": "A1--A2", "decision": "same_physical_product"},
        }

        lookup = jcb.decision_lookup_by_pair(decisions)

        assert lookup["A1--A2"]["decision"] == "same_physical_product"

    def test_parse_jewelry_box_response_clamps_and_normalizes_items(self) -> None:
        parsed = jcb.parse_jewelry_box_response(
            '{"items":[{"type":"Ring","box":[-5,10,30,40],"confidence":1.2,"visibility":"partial","notes":"on hand"},'
            '{"type":"watch","box":[90,90,50,50],"confidence":0.5}]}',
            image_width=100,
            image_height=120,
        )

        assert parsed[0]["type"] == "ring"
        assert parsed[0]["box"] == [0, 10, 25, 40]
        assert parsed[0]["confidence"] == 1.0
        assert parsed[1]["type"] == "unknown_jewelry"
        assert parsed[1]["box"] == [90, 90, 10, 30]

    def test_expand_crop_box_keeps_padded_crop_inside_image(self) -> None:
        expanded = jcb.expand_crop_box((90, 90, 20, 20), image_width=100, image_height=100, mode="square_padded")

        assert expanded == (40, 40, 60, 60)

    def test_catalog_product_ids_extracts_ids(self) -> None:
        assert jcb.catalog_product_ids("R047-R050_ring_arabesque E136") == ["E136", "R047", "R050"]
        assert jcb.catalog_product_ids("R012-R014_elisheva-l_stack_front_01.jpg") == ["R012", "R013", "R014"]

    def test_catalog_shot_key_groups_export_variants(self) -> None:
        jpg = Path("E101_louvre_white_front_02.jpg")
        png = Path("E101_louvre_white_front_02.png")
        high = Path("E101_louvre_white_front_02_print.jpg")

        assert jcb.catalog_shot_key(jpg) == jcb.catalog_shot_key(png)
        assert jcb.catalog_shot_key(jpg) == jcb.catalog_shot_key(high)

    def test_catalog_asset_is_ambiguous(self) -> None:
        clean = {"flags": [], "filename_product_ids": ["R001"]}
        missing = {"flags": ["missing_product_id"], "filename_product_ids": []}
        ambiguous_multi = {"flags": ["multiple_product_ids"], "filename_product_ids": []}
        exact_multi = {"flags": ["multiple_product_ids"], "filename_product_ids": ["R001", "R002"]}

        assert not jcb.catalog_asset_is_ambiguous(clean)
        assert jcb.catalog_asset_is_ambiguous(missing)
        assert jcb.catalog_asset_is_ambiguous(ambiguous_multi)
        assert not jcb.catalog_asset_is_ambiguous(exact_multi)

    def test_catalog_normalize_keeps_combined_folder_filename_ids_separate(self) -> None:
        base = {
            "category": "טבעות",
            "product_folder": "R004-R005_ring_orion",
            "product_folder_path": "טבעות/R004-R005_ring_orion",
            "folder_product_ids": ["R004", "R005"],
            "reference_cluster_id": "טבעות/R004-R005_ring_orion",
            "extension": ".jpg",
            "export_kind": "web",
            "image_role": "angled",
            "shot_key": "orion-ruby-yellow-angled-01",
            "size_bytes": 100,
            "width": 1500,
            "height": 1500,
            "ahash": "0",
            "dhash": "0",
        }
        r004 = {
            **base,
            "occurrence_id": "CO1",
            "path": "/tmp/R004_orion_ruby_yellow_angled_01.jpg",
            "rel_path": "טבעות/R004-R005_ring_orion/web/R004_orion_ruby_yellow_angled_01.jpg",
            "filename": "R004_orion_ruby_yellow_angled_01.jpg",
            "filename_product_ids": ["R004"],
            "product_ids": ["R004"],
            "sha256": "r004",
        }
        r005 = {
            **base,
            "occurrence_id": "CO2",
            "path": "/tmp/R005_orion_ruby_yellow_angled_01.jpg",
            "rel_path": "טבעות/R004-R005_ring_orion/web/R005_orion_ruby_yellow_angled_01.jpg",
            "filename": "R005_orion_ruby_yellow_angled_01.jpg",
            "filename_product_ids": ["R005"],
            "product_ids": ["R005"],
            "sha256": "r005",
        }

        assets, _ = jcb.normalize_catalog_assets([r004, r005])

        assert len(assets) == 2
        assert sorted(tuple(asset["product_ids"]) for asset in assets) == [("R004",), ("R005",)]

    def test_catalog_normalize_does_not_merge_loose_category_root_copy_into_product_asset(self) -> None:
        product = {
            "occurrence_id": "CO1",
            "category": "טבעות",
            "category_code": "R",
            "path": "/tmp/R035_gold-crown_yellow_angled_03.png",
            "rel_path": "טבעות/R035_ring_gold-crown/web/png/R035_gold-crown_yellow_angled_03.png",
            "product_folder": "R035_ring_gold-crown",
            "product_folder_path": "טבעות/R035_ring_gold-crown",
            "folder_product_ids": ["R035"],
            "filename_product_ids": ["R035"],
            "product_ids": ["R035"],
            "reference_cluster_id": "טבעות/R035_ring_gold-crown",
            "filename": "R035_gold-crown_yellow_angled_03.png",
            "extension": ".png",
            "export_kind": "png",
            "image_role": "angled",
            "shot_key": "gold-crown-yellow-angled-03",
            "size_bytes": 100,
            "width": 1500,
            "height": 1500,
            "sha256": "same-file",
            "ahash": "0",
            "dhash": "0",
        }
        loose_copy = {
            **product,
            "occurrence_id": "CO2",
            "path": "/tmp/עותק של 20230202-web-res-1500-37-new.png",
            "rel_path": "טבעות/עותק של 20230202-web-res-1500-37-new.png",
            "product_folder": "",
            "product_folder_path": "טבעות",
            "folder_product_ids": [],
            "filename_product_ids": [],
            "product_ids": [],
            "reference_cluster_id": "טבעות",
            "filename": "עותק של 20230202-web-res-1500-37-new.png",
            "image_role": "unknown",
            "shot_key": "20230202-37-new",
        }

        product_only_occurrences = [occurrence for occurrence in [product, loose_copy] if occurrence["product_folder"]]
        assets, _ = jcb.normalize_catalog_assets(product_only_occurrences)

        assert len(assets) == 1
        assert assets[0]["occurrence_count"] == 1
        assert assets[0]["occurrences"][0]["rel_path"] == product["rel_path"]

    def test_load_manifest_supports_final_catalog_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "final_labeled_manifest.csv"
            fields = [
                "asset_id",
                "category",
                "preferred_path",
                "quality_path",
                "export_kinds",
                "shot_keys",
                "flags",
                "occurrence_count",
                "final_product_ids",
                "identity_eligible",
            ]
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "asset_id": "CA1",
                        "category": "rings",
                        "preferred_path": "/tmp/identity.jpg",
                        "quality_path": "/tmp/identity.jpg",
                        "export_kinds": "web|print",
                        "shot_keys": "front",
                        "flags": "",
                        "occurrence_count": "1",
                        "final_product_ids": "R001",
                        "identity_eligible": "true",
                    }
                )
                writer.writerow(
                    {
                        "asset_id": "CA2",
                        "category": "rings",
                        "preferred_path": "/tmp/supporting.jpg",
                        "quality_path": "/tmp/supporting.jpg",
                        "export_kinds": "web",
                        "shot_keys": "lifestyle",
                        "flags": "",
                        "occurrence_count": "1",
                        "final_product_ids": "R001",
                        "identity_eligible": "false",
                    }
                )

            assets = jcb.load_manifest(manifest)

        assert assets[0].reference_cluster_ids == ["R001"]
        assert assets[0].sources == ["catalog"]
        assert assets[0].kinds == ["web", "print"]
        assert assets[1].reference_cluster_ids == []
        assert [asset.asset_id for asset in jcb.identity_assets(assets)] == ["CA1"]

    def test_selected_lifestyle_manifest_limit_zero_means_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.jpg"
            image.write_text("stub", encoding="utf-8")
            manifest = Path(tmp) / "manifest.csv"
            fields = ["asset_id", "preferred_path", "image_roles", "media_role"]
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(3):
                    writer.writerow(
                        {
                            "asset_id": f"A{index}",
                            "preferred_path": str(image),
                            "image_roles": "model_or_lifestyle",
                            "media_role": "",
                        }
                    )

            rows = jcb.selected_lifestyle_manifest_rows(manifest, limit=0)

        assert [row["asset_id"] for row in rows] == ["A0", "A1", "A2"]

    def test_selected_lifestyle_manifest_filters_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.jpg"
            image.write_text("stub", encoding="utf-8")
            manifest = Path(tmp) / "manifest.csv"
            fields = ["asset_id", "category", "preferred_path", "image_roles", "media_role"]
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "asset_id": "R1",
                        "category": "טבעות",
                        "preferred_path": str(image),
                        "image_roles": "model_or_lifestyle",
                        "media_role": "",
                    }
                )
                writer.writerow(
                    {
                        "asset_id": "E1",
                        "category": "עגילים",
                        "preferred_path": str(image),
                        "image_roles": "model_or_lifestyle",
                        "media_role": "",
                    }
                )

            rows = jcb.selected_lifestyle_manifest_rows(manifest, limit=0, category="rings")

        assert [row["asset_id"] for row in rows] == ["R1"]

    def test_filter_owlv2_detections_drops_noise_and_dedupes(self) -> None:
        raw = [
            {"box_id": "O1", "label": "ring on finger", "box": [10, 10, 90, 90], "score": 0.30},
            {"box_id": "O2", "label": "gold ring", "box": [12, 12, 84, 84], "score": 0.28},
            {"box_id": "O3", "label": "gold ring", "box": [0, 0, 800, 800], "score": 0.90},
            {"box_id": "O4", "label": "ring", "box": [1, 1, 1, 1], "score": 0.90},
        ]

        filtered, flags = jcb.filter_owlv2_detections(raw, image_width=1000, image_height=1000)

        assert [item["box_id"] for item in filtered] == ["O2"]
        assert flags == []

    def test_build_crop_review_queue_includes_exceptions_before_sample(self) -> None:
        labels = {
            "A1": {"auto_review_required": True, "auto_review_flags": ["low_detector_score"]},
            "A2": {"auto_review_required": False, "auto_review_flags": []},
        }

        queue = jcb.build_crop_review_queue(labels, sample_rate=0)

        assert queue == [{"asset_id": "A1", "review_flags": ["low_detector_score"], "sampled_auto_pass": False}]

    def test_asset_is_live_shot_requires_model_or_lifestyle_role(self) -> None:
        assert jcb.asset_is_live_shot({"image_roles": "model_or_lifestyle"})
        assert jcb.asset_is_live_shot({"image_roles": "angled|model_or_lifestyle"})
        assert not jcb.asset_is_live_shot({"image_roles": "front", "media_role": "shared_supporting"})
        assert not jcb.asset_is_live_shot({"image_roles": "unknown", "media_role": "supporting"})

    def test_crop_localization_summary_reports_review_rates(self) -> None:
        assets = [{"asset_id": "A1"}, {"asset_id": "A2"}]
        auto_labels = {
            "A1": {"verdict": "pass_good", "best_crop_id": "A1_owlv2_padded", "auto_review_required": False},
            "A2": {"verdict": "pass_usable", "best_crop_id": "A2_full_image", "auto_review_required": True},
        }
        queue = [{"asset_id": "A2", "review_flags": ["low_detector_score"]}]

        summary = jcb.crop_localization_summary(assets, auto_labels, queue, {})

        assert summary["usable_localization_rate"] == 1.0
        assert summary["manual_review_rate"] == 0.5
        assert summary["best_candidate_distribution"] == {"owlv2_padded": 1, "full_image": 1}

    def test_crop_localization_summary_overlays_review_labels_on_auto_labels(self) -> None:
        assets = [{"asset_id": "A1"}, {"asset_id": "A2"}]
        auto_labels = {
            "A1": {"verdict": "pass_good", "best_crop_id": "A1_owlv2_padded", "auto_review_required": False},
            "A2": {"verdict": "pass_good", "best_crop_id": "A2_owlv2_padded", "auto_review_required": True},
        }
        final_labels = {
            "A2": {"verdict": "fail_too_tight", "best_crop_id": "A2_owlv2_base"},
        }

        summary = jcb.crop_localization_summary(assets, auto_labels, [{"asset_id": "A2"}], final_labels)

        assert summary["pass_good"] == 1
        assert summary["fail_too_tight"] == 1
        assert summary["usable_localization_rate"] == 0.5

    def test_foreground_product_box_finds_isolated_product_on_white_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "ring.jpg"
            image = Image.new("RGB", (200, 200), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((50, 50, 150, 150), outline=(170, 120, 40), width=18)
            image.save(image_path)

            foreground = jcb.foreground_product_box(image_path, image_width=200, image_height=200)

        assert foreground is not None
        assert foreground["box_area_ratio"] > 0.20
        assert min(foreground["background_rgb"]) >= jcb.FOREGROUND_MIN_BACKGROUND_RGB
        assert foreground["pixel_area_ratio"] <= jcb.FOREGROUND_MAX_PIXEL_AREA_RATIO

    def test_image_registry_dedupes_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            image = Image.new("RGB", (20, 20), "white")
            image.save(folder / "a.jpg")
            image.save(folder / "b.jpg")

            records = jcb.build_image_registry(folder)

        assert len(records) == 2
        assert records[0]["image_id"] == records[1]["image_id"]
        assert records[0]["status"] == "ready"
        assert records[1]["status"] == "duplicate"

    def test_profile_cache_key_reuses_duplicate_hashes(self) -> None:
        record = {"sha256": "abc123"}

        first = jcb.profile_cache_key(record, "gpt-4.1-mini", 1024)
        second = jcb.profile_cache_key({**record, "image_id": "other"}, "gpt-4.1-mini", 1024)
        changed_size = jcb.profile_cache_key(record, "gpt-4.1-mini", 768)

        assert first == second
        assert first != changed_size
        assert jcb.EVIDENCE_PROFILE_PROMPT_VERSION in first

    def test_parse_image_profile_text_rejects_invalid_json_shape(self) -> None:
        with pytest.raises(ValueError, match="invalid scene_type"):
            jcb.parse_image_profile_text(
                '{"scene_type":"portrait","recommended_evidence_policy":"full_only","jewelry_items":[]}',
                "I1",
                100,
                100,
            )

    def test_generate_evidence_never_reads_catalog_labels_and_keeps_clean_product_full_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            image_path = base / "product.jpg"
            Image.new("RGB", (100, 100), "white").save(image_path)
            manifest = [
                {
                    "image_id": "I1",
                    "source_path": str(image_path),
                    "filename": "product.jpg",
                    "width": 100,
                    "height": 100,
                    "sha256": jcb.sha256(image_path),
                    "status": "ready",
                    "image_roles": "model_or_lifestyle",
                    "final_product_ids": "R001",
                    "identity_eligible": "true",
                }
            ]
            profiles = {
                "I1": {
                    "image_id": "I1",
                    "scene_type": "clean_product",
                    "has_hand": False,
                    "has_person": False,
                    "background_type": "white_studio",
                    "jewelry_items": [
                        {
                            "type": "ring",
                            "dominance": "dominant",
                            "object_completeness": "complete",
                            "box": [20, 20, 60, 60],
                            "confidence": 0.9,
                            "identity_features": ["gold ring"],
                        }
                    ],
                    "quality_flags": [],
                    "recommended_evidence_policy": "full_only",
                }
            }

            views = jcb.generate_evidence_views(manifest, profiles, base / "evidence")

        assert [view["view_type"] for view in views] == ["full_image"]

    def test_generate_evidence_model_lifestyle_produces_crop_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            image_path = base / "lifestyle.jpg"
            Image.new("RGB", (120, 120), "white").save(image_path)
            manifest = [
                {
                    "image_id": "I1",
                    "source_path": str(image_path),
                    "filename": "lifestyle.jpg",
                    "width": 120,
                    "height": 120,
                    "sha256": jcb.sha256(image_path),
                    "status": "ready",
                }
            ]
            profiles = {
                "I1": {
                    "image_id": "I1",
                    "scene_type": "model_lifestyle",
                    "has_hand": True,
                    "has_person": False,
                    "background_type": "lifestyle",
                    "jewelry_items": [
                        {
                            "type": "ring",
                            "dominance": "small",
                            "object_completeness": "complete",
                            "box": [40, 40, 20, 20],
                            "confidence": 0.8,
                            "identity_features": ["green stone"],
                        }
                    ],
                    "quality_flags": [],
                    "recommended_evidence_policy": "crop_heavy",
                }
            }

            views = jcb.generate_evidence_views(manifest, profiles, base / "evidence")

        assert {view["view_type"] for view in views} == {"full_image", "vlm_context", "owlv2_padded", "owlv2_context"}
        assert len(views) == 4

    def test_generate_evidence_uncertain_profile_marks_review_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            image_path = base / "uncertain.jpg"
            Image.new("RGB", (120, 120), "white").save(image_path)
            manifest = [
                {
                    "image_id": "I1",
                    "source_path": str(image_path),
                    "filename": "uncertain.jpg",
                    "width": 120,
                    "height": 120,
                    "sha256": jcb.sha256(image_path),
                    "status": "ready",
                }
            ]
            profiles = {
                "I1": {
                    "image_id": "I1",
                    "scene_type": "uncertain",
                    "has_hand": False,
                    "has_person": False,
                    "background_type": "uncertain",
                    "jewelry_items": [],
                    "quality_flags": ["low_confidence"],
                    "recommended_evidence_policy": "review",
                }
            }

            views = jcb.generate_evidence_views(manifest, profiles, base / "evidence")

        risky = [flag for view in views for flag in view.get("risk_flags", [])]
        assert "profile_review_risk" in risky
        assert "low_confidence" in risky

    def test_retrieval_groups_matches_by_parent_image(self) -> None:
        views = [
            {"view_id": "A_full_image", "image_id": "A", "view_type": "full_image", "usable_for_retrieval": True},
            {"view_id": "A_vlm_context", "image_id": "A", "view_type": "vlm_context", "usable_for_retrieval": True},
            {"view_id": "B_full_image", "image_id": "B", "view_type": "full_image", "usable_for_retrieval": True},
            {"view_id": "B_vlm_context", "image_id": "B", "view_type": "vlm_context", "usable_for_retrieval": True},
            {"view_id": "C_full_image", "image_id": "C", "view_type": "full_image", "usable_for_retrieval": True},
        ]
        vectors = {
            "A_full_image": [1.0, 0.0],
            "A_vlm_context": [0.9, 0.1],
            "B_full_image": [1.0, 0.0],
            "B_vlm_context": [0.9, 0.1],
            "C_full_image": [0.0, 1.0],
        }

        candidates = jcb.build_retrieval_candidates(views, vectors, top_k=4)
        top_for_a = next(item for item in candidates if item["query_image_id"] == "A" and item["rank"] == 1)

        assert top_for_a["candidate_image_id"] == "B"
        assert top_for_a["view_match_count"] >= 2
        assert top_for_a["score"] > 0.9

    def test_adjudication_queue_includes_low_margin_candidates(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "rank": 1, "score": 0.91, "best_view_similarity": 0.93, "second_view_agreement": 0.92},
            {"query_image_id": "A", "candidate_image_id": "C", "rank": 2, "score": 0.90, "best_view_similarity": 0.92, "second_view_agreement": 0.91},
            {"query_image_id": "A", "candidate_image_id": "D", "rank": 4, "score": 0.50, "best_view_similarity": 0.51, "second_view_agreement": 0.50},
        ]

        queue = jcb.adjudication_queue(candidates, {}, low_margin=0.03)

        low_margin_pairs = {(item["query_image_id"], item["candidate_image_id"]) for item in queue if "low_score_margin" in item["adjudication_reasons"]}
        assert ("A", "B") in low_margin_pairs
        assert ("A", "C") in low_margin_pairs
        assert all(item["candidate_image_id"] != "D" for item in queue)

    def test_adjudication_queue_dedupes_reciprocal_pairs(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "rank": 1, "score": 0.91, "best_view_similarity": 0.93, "second_view_agreement": 0.92},
            {"query_image_id": "B", "candidate_image_id": "A", "rank": 1, "score": 0.93, "best_view_similarity": 0.94, "second_view_agreement": 0.93},
        ]

        queue = jcb.adjudication_queue(candidates, {}, low_margin=0.03)

        assert len(queue) == 1
        assert queue[0]["query_image_id"] == "B"

    def test_human_review_queue_groups_unresolved_positive_by_image(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "score": 0.92, "score_margin": 0.01, "adjudication_reasons": ["low_score_margin"]},
            {"query_image_id": "A", "candidate_image_id": "C", "score": 0.91, "score_margin": 0.01, "adjudication_reasons": ["low_score_margin"]},
        ]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
            "A-C": {"query_image_id": "A", "candidate_id": "C", "decision": "same_design_variant", "confidence": 0.95, "review_required": False},
        }

        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0, min_margin=0.03)

        assert len(queue) == 1
        assert queue[0]["query_image_id"] == "A"
        assert queue[0]["candidate_ids"] == ["B", "C"]
        assert "positive_below_auto_accept_band" in queue[0]["review_flags"]

    def test_final_matches_require_auto_accept_bands_and_dedupe_pairs(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "score": 0.92, "score_margin": 0.04},
            {"query_image_id": "B", "candidate_image_id": "A", "score": 0.91, "score_margin": 0.04},
            {"query_image_id": "A", "candidate_image_id": "C", "score": 0.92, "score_margin": 0.01},
        ]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
            "B-A": {"query_image_id": "B", "candidate_id": "A", "decision": "same_product", "confidence": 0.95, "review_required": False},
            "A-C": {"query_image_id": "A", "candidate_id": "C", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, min_margin=0.03)

        assert len(matches) == 1
        assert matches[0]["query_image_id"] == "A"
        assert matches[0]["candidate_id"] == "B"

    def test_benchmark_match_outputs_reports_precision_and_review_rate(self) -> None:
        benchmark = jcb.benchmark_match_outputs(
            registry_asset_map={"I1": "CA1", "I2": "CA2", "I3": "CA3"},
            label_map={"CA1": {"R1"}, "CA2": {"R1"}, "CA3": {"R2"}},
            matches=[
                {"query_image_id": "I1", "candidate_id": "I2", "decision": "same_product", "confidence": 0.95},
                {"query_image_id": "I1", "candidate_id": "I3", "decision": "same_product", "confidence": 0.95},
                {"query_image_id": "I2", "candidate_id": "I3", "decision": "same_design_variant", "confidence": 0.95},
            ],
            review_queue=[{"query_image_id": "I1", "candidate_ids": ["I3"]}],
            adjudication_count=2,
        )

        assert benchmark["auto_same_product_count"] == 2
        assert benchmark["auto_same_product_true_positive"] == 1
        assert benchmark["auto_same_product_false_positive"] == 1
        assert benchmark["auto_same_product_precision"] == 0.5
        assert benchmark["human_review_image_rate"] == 1 / 3
        assert benchmark["human_review_reduction_factor_vs_all_images"] == 3.0
        assert benchmark["naive_pairwise_candidate_count"] == 3
        assert benchmark["ai_adjudication_reduction_factor_vs_pairwise"] == 1.5
        assert not benchmark["target_10x_human_review_efficiency_met"]

    def test_benchmark_match_outputs_reports_10x_efficiency_when_review_is_small(self) -> None:
        registry_asset_map = {f"I{index}": f"CA{index}" for index in range(20)}

        benchmark = jcb.benchmark_match_outputs(
            registry_asset_map=registry_asset_map,
            label_map={},
            matches=[],
            review_queue=[{"query_image_id": "I1", "candidate_ids": ["I2"]}],
            adjudication_count=10,
        )

        assert benchmark["human_review_image_rate"] == 0.05
        assert benchmark["human_review_reduction_factor_vs_all_images"] == 20.0
        assert benchmark["ai_adjudication_reduction_factor_vs_pairwise"] == 19.0
        assert benchmark["target_10x_human_review_efficiency_met"]
        assert benchmark["target_10x_ai_pairwise_efficiency_met"]

    def test_summarize_match_benchmarks_aggregates_precision_and_efficiency(self) -> None:
        summary = jcb.summarize_match_benchmarks(
            [
                {
                    "image_count": 20,
                    "auto_match_count": 3,
                    "auto_same_product_count": 2,
                    "auto_same_product_true_positive": 2,
                    "auto_same_product_false_positive": 0,
                    "human_review_image_count": 1,
                    "naive_pairwise_candidate_count": 190,
                    "ai_adjudicated_pair_count": 10,
                },
                {
                    "image_count": 30,
                    "auto_match_count": 4,
                    "auto_same_product_count": 3,
                    "auto_same_product_true_positive": 2,
                    "auto_same_product_false_positive": 1,
                    "human_review_image_count": 4,
                    "naive_pairwise_candidate_count": 435,
                    "ai_adjudicated_pair_count": 20,
                },
            ]
        )

        assert summary["benchmark_run_count"] == 2
        assert summary["image_count"] == 50
        assert summary["auto_same_product_precision"] == 0.8
        assert summary["human_review_image_rate"] == 0.1
        assert summary["human_review_reduction_factor_vs_all_images"] == 10.0
        assert summary["ai_adjudication_reduction_factor_vs_pairwise"] == 625 / 30
        assert not summary["target_same_product_precision_met"]
        assert summary["target_10x_human_review_efficiency_met"]

    def test_select_mixed_benchmark_rows_balances_categories_and_media_roles(self) -> None:
        rows = [
            {"asset_id": "A1", "category": "rings", "final_product_ids": "R1", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "A2", "category": "rings", "final_product_ids": "R1", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "A3", "category": "rings", "final_product_ids": "R2", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "A4", "category": "rings", "final_product_ids": "R2", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "A5", "category": "rings", "final_product_ids": "R3", "identity_eligible": "false", "media_role": "supporting"},
            {"asset_id": "B1", "category": "earrings", "final_product_ids": "E1", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "B2", "category": "earrings", "final_product_ids": "E1", "identity_eligible": "true", "media_role": "identity"},
            {"asset_id": "B3", "category": "earrings", "final_product_ids": "E2", "identity_eligible": "false", "media_role": "shared_supporting"},
        ]

        selected = jcb.select_mixed_benchmark_rows(rows, per_category_products=1, identity_per_product=2, supporting_per_category=1)

        assert [row["asset_id"] for row in selected] == ["B1", "B2", "B3", "A1", "A2", "A5"]

    def test_weak_tiny_same_product_demotes_to_design_variant(self) -> None:
        profiles = {
            "A": {"jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "identity_features": []}]},
            "B": {"jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "identity_features": []}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.95, "score_margin": 0.05, "full_image_similarity": 0.90}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)
        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0, profiles=profiles)

        assert matches[0]["decision"] == "same_design_variant"
        assert matches[0]["demoted_from_same_product"]
        assert queue == []

    def test_visible_identity_evidence_allows_same_product_auto_accept(self) -> None:
        profiles = {
            "A": {"jewelry_items": [{"dominance": "medium", "object_completeness": "complete", "identity_features": ["green oval stone", "braided band"]}]},
            "B": {"jewelry_items": [{"dominance": "medium", "object_completeness": "complete", "identity_features": ["green oval stone", "braided band"]}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.95, "score_margin": 0.05, "full_image_similarity": 0.90}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)

        assert matches[0]["decision"] == "same_product"
        assert not matches[0]["demoted_from_same_product"]

    def test_large_box_identity_evidence_allows_same_product_even_if_dominance_label_is_tiny(self) -> None:
        profiles = {
            "A": {"image_width": 1000, "image_height": 1000, "jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "box": [100, 100, 600, 600], "identity_features": []}]},
            "B": {"image_width": 1000, "image_height": 1000, "jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "box": [120, 120, 580, 580], "identity_features": []}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.95, "score_margin": 0.05}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)

        assert matches[0]["decision"] == "same_product"

    def test_strong_identity_evidence_allows_lower_retrieval_score_auto_accept(self) -> None:
        profiles = {
            "A": {"image_width": 1000, "image_height": 1000, "jewelry_items": [{"dominance": "medium", "object_completeness": "complete", "box": [100, 100, 500, 500], "identity_features": ["oval center stone"]}]},
            "B": {"image_width": 1000, "image_height": 1000, "jewelry_items": [{"dominance": "medium", "object_completeness": "complete", "box": [120, 120, 480, 480], "identity_features": ["oval center stone"]}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.82, "score_margin": 0.05}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)
        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0, profiles=profiles)

        assert matches[0]["decision"] == "same_product"
        assert queue == []

    def test_lower_score_auto_accept_does_not_apply_to_weak_identity_evidence(self) -> None:
        profiles = {
            "A": {"jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "identity_features": []}]},
            "B": {"jewelry_items": [{"dominance": "tiny", "object_completeness": "complete", "identity_features": []}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.82, "score_margin": 0.05}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)
        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0, profiles=profiles)

        assert matches == []
        assert queue[0]["query_image_id"] == "A"
        assert "positive_below_auto_accept_band" in queue[0]["review_flags"]

    def test_isolated_positive_auto_accepts_low_margin_match(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "score": 0.89, "score_margin": 0.005},
            {"query_image_id": "A", "candidate_image_id": "C", "score": 0.885, "score_margin": 0.005},
        ]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
            "A-C": {"query_image_id": "A", "candidate_id": "C", "decision": "different", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates)
        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0)

        assert matches[0]["candidate_id"] == "B"
        assert queue == []

    def test_competing_positive_low_margin_stays_in_review(self) -> None:
        candidates = [
            {"query_image_id": "A", "candidate_image_id": "B", "score": 0.89, "score_margin": 0.005},
            {"query_image_id": "A", "candidate_image_id": "C", "score": 0.885, "score_margin": 0.005},
        ]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
            "A-C": {"query_image_id": "A", "candidate_id": "C", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates)
        queue = jcb.human_review_queue(decisions, candidates, sample_rate=0)

        assert matches == []
        assert queue[0]["candidate_ids"] == ["B", "C"]
        assert "positive_below_auto_accept_band" in queue[0]["review_flags"]

    def test_multi_item_same_product_demotes_to_design_variant(self) -> None:
        profiles = {
            "A": {"scene_type": "multi_item", "jewelry_items": [{"dominance": "medium"}, {"dominance": "medium"}]},
            "B": {"scene_type": "clean_product", "jewelry_items": [{"dominance": "medium"}, {"dominance": "medium"}]},
        }
        candidates = [{"query_image_id": "A", "candidate_image_id": "B", "score": 0.95, "score_margin": 0.05}]
        decisions = {
            "A-B": {"query_image_id": "A", "candidate_id": "B", "decision": "same_product", "confidence": 0.95, "review_required": False},
        }

        matches = jcb.final_matches_from_decisions(decisions, candidates, profiles=profiles)

        assert matches[0]["decision"] == "same_design_variant"

    def test_benchmark_precision_is_safe_when_no_auto_same_product_matches(self) -> None:
        benchmark = jcb.benchmark_match_outputs(
            registry_asset_map={"I1": "CA1", "I2": "CA2"},
            label_map={"CA1": {"R1"}, "CA2": {"R2"}},
            matches=[{"query_image_id": "I1", "candidate_id": "I2", "decision": "same_design_variant", "confidence": 0.95}],
            review_queue=[],
        )

        assert benchmark["auto_same_product_count"] == 0
        assert benchmark["auto_same_product_precision"] == 1.0
        assert benchmark["target_same_product_precision_met"]


if __name__ == "__main__":
    unittest.main()
