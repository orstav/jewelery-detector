import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "jewelry_cluster_benchmark.py"
SPEC = importlib.util.spec_from_file_location("jewelry_cluster_benchmark", MODULE_PATH)
jcb = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = jcb
SPEC.loader.exec_module(jcb)


def asset(asset_id, labels):
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


def occurrence(occurrence_id, source, kind, ahash, dhash):
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
    def test_connected_assets_become_cluster(self):
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        scores = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "score": 0.91},
            {"source_asset_id": "A1", "target_asset_id": "A3", "score": 0.20},
            {"source_asset_id": "A2", "target_asset_id": "A3", "score": 0.25},
        ]

        clusters = jcb.cluster_from_scores(assets, scores, threshold=0.9)
        cluster_sizes = sorted(cluster["size"] for cluster in clusters)

        self.assertEqual(cluster_sizes, [1, 2])

    def test_benchmark_counts_precision_and_recall(self):
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        clusters = [
            {"cluster_id": "C1", "asset_ids": ["A1", "A2"], "size": 2},
            {"cluster_id": "C2", "asset_ids": ["A3"], "size": 1},
        ]

        benchmark = jcb.benchmark_clusters(assets, clusters)

        self.assertEqual(benchmark["true_positive"], 1)
        self.assertEqual(benchmark["false_positive"], 0)
        self.assertEqual(benchmark["false_negative"], 0)
        self.assertEqual(benchmark["precision"], 1.0)
        self.assertEqual(benchmark["recall"], 1.0)

    def test_conservative_threshold_prefers_precision_then_recall(self):
        rows = [
            {"threshold": 0.80, "precision": 0.90, "recall": 0.95, "f1": 0.92, "predicted_positive": 20},
            {"threshold": 0.86, "precision": 0.99, "recall": 0.55, "f1": 0.70, "predicted_positive": 10},
            {"threshold": 0.92, "precision": 1.00, "recall": 0.50, "f1": 0.66, "predicted_positive": 8},
        ]

        self.assertEqual(jcb.choose_threshold(rows, min_precision=0.98), 0.86)

    def test_threshold_fallback_still_prefers_precision(self):
        rows = [
            {"threshold": 0.80, "precision": 0.60, "recall": 0.95, "f1": 0.73, "predicted_positive": 20},
            {"threshold": 0.92, "precision": 0.90, "recall": 0.40, "f1": 0.55, "predicted_positive": 6},
        ]

        self.assertEqual(jcb.choose_threshold(rows, min_precision=0.98), 0.92)

    def test_parse_thresholds_sorts_and_dedupes(self):
        self.assertEqual(jcb.parse_thresholds("0.9,0.8,0.9"), [0.8, 0.9])

    def test_ai_benchmark_counts_unsure_same_as_missed_recall(self):
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

        self.assertEqual(benchmark["true_positive"], 1)
        self.assertEqual(benchmark["false_positive"], 0)
        self.assertEqual(benchmark["false_negative"], 1)
        self.assertEqual(benchmark["true_negative"], 1)
        self.assertEqual(benchmark["precision"], 1.0)
        self.assertEqual(benchmark["recall"], 0.5)

    def test_parse_ai_decision_text_validates_label(self):
        parsed = jcb.parse_ai_decision_text('{"decision":"same_physical_product","confidence":0.8,"reason":"same stone"}')

        self.assertEqual(parsed["decision"], "same_physical_product")
        self.assertEqual(parsed["confidence"], 0.8)

    def test_same_design_variant_is_not_product_positive(self):
        pairs = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "benchmark_same_reference": True},
            {"source_asset_id": "A1", "target_asset_id": "A3", "benchmark_same_reference": False},
        ]
        decisions = {
            "A1--A2": {"decision": "same_design_variant"},
            "A1--A3": {"decision": "same_design_variant"},
        }

        benchmark = jcb.benchmark_ai_decisions(pairs, decisions)

        self.assertEqual(benchmark["true_positive"], 0)
        self.assertEqual(benchmark["false_positive"], 0)
        self.assertEqual(benchmark["false_negative"], 1)
        self.assertEqual(benchmark["true_negative"], 1)
        self.assertEqual(benchmark["design_variant"], 2)

    def test_candidate_pairs_include_top_k_neighbors(self):
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R2"])]
        scores = [
            {"source_asset_id": "A1", "target_asset_id": "A2", "score": 0.50, "source_view": "full", "target_view": "full"},
            {"source_asset_id": "A1", "target_asset_id": "A3", "score": 0.40, "source_view": "full", "target_view": "full"},
            {"source_asset_id": "A2", "target_asset_id": "A3", "score": 0.30, "source_view": "full", "target_view": "full"},
        ]

        candidates = jcb.candidate_pairs(assets, scores, threshold=0.9, top_k=1)
        keys = {jcb.candidate_pair_key(pair) for pair in candidates}

        self.assertIn("A1--A2", keys)
        self.assertIn("A1--A3", keys)

    def test_edit_duplicate_pairs_require_mutual_nearest(self):
        fixed = occurrence("F1", "fixed", "web", "0000000000000000", "0000000000000000")
        best = occurrence("U1", "unfixed", "web", "0000000000000001", "0000000000000000")
        farther = occurrence("U2", "unfixed", "web", "000000000000000f", "0000000000000000")

        pairs = jcb.edited_duplicate_pairs([fixed, best, farther], max_distance=3)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["source_occurrence_id"], "F1")
        self.assertEqual(pairs[0]["target_occurrence_id"], "U1")

    def test_normalize_assets_can_collapse_edit_duplicates(self):
        fixed = occurrence("F1", "fixed", "web", "0000000000000000", "0000000000000000")
        unfixed = occurrence("U1", "unfixed", "web", "0000000000000001", "0000000000000000")

        assets, occurrence_to_asset, edit_pairs = jcb.normalize_assets([fixed, unfixed], edit_dedup_distance=3)

        self.assertEqual(len(assets), 1)
        self.assertEqual(occurrence_to_asset["F1"], occurrence_to_asset["U1"])
        self.assertEqual(len(edit_pairs), 1)

    def test_build_ai_cluster_export_separates_product_and_design(self):
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
        self.assertEqual(product_sizes, [1, 1, 2])
        self.assertEqual(design_sizes, [1, 3])
        self.assertEqual(export["summary"]["review_queue_count"], 0)

    def test_build_ai_cluster_export_can_disable_design_variants(self):
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

        self.assertEqual(len(export["design_clusters"]), 2)
        self.assertEqual(export["summary"]["design_edge_count"], 0)
        self.assertEqual(export["summary"]["negative_edge_count"], 1)

    def test_build_ai_cluster_export_flags_transitive_contradiction(self):
        assets = [asset("A1", ["R1"]), asset("A2", ["R1"]), asset("A3", ["R1"])]
        decisions = {
            "A1--A2": {"source_asset_id": "A1", "target_asset_id": "A2", "decision": "same_physical_product"},
            "A2--A3": {"source_asset_id": "A2", "target_asset_id": "A3", "decision": "same_physical_product"},
            "A1--A3": {"source_asset_id": "A1", "target_asset_id": "A3", "decision": "different_design"},
        }

        export = jcb.build_ai_cluster_export(assets, decisions)

        self.assertEqual(len(export["product_clusters"]), 0)
        self.assertEqual(len(export["blocked_product_clusters"]), 1)
        self.assertEqual(export["summary"]["review_queue_count"], 1)
        self.assertIn("negative_edge_inside_product_cluster", export["blocked_product_clusters"][0]["review_flags"])

    def test_ai_cache_key_changes_with_prompt_and_image_inputs(self):
        left = asset("A1", ["R1"])
        right = asset("A2", ["R1"])
        pair = {"source_asset_id": "A1", "target_asset_id": "A2"}
        left.preferred_path = __file__
        right.preferred_path = __file__

        key_small = jcb.ai_decision_cache_key(pair, left, right, "gpt-4.1-mini", 512)
        key_large = jcb.ai_decision_cache_key(pair, left, right, "gpt-4.1-mini", 768)

        self.assertIn(jcb.AI_PAIR_PROMPT_VERSION, key_small)
        self.assertNotEqual(key_small, key_large)

    def test_decision_lookup_by_pair_supports_versioned_cache_keys(self):
        decisions = {
            "A1--A2|model|prompt|hash": {"pair_key": "A1--A2", "decision": "same_physical_product"},
        }

        lookup = jcb.decision_lookup_by_pair(decisions)

        self.assertEqual(lookup["A1--A2"]["decision"], "same_physical_product")


if __name__ == "__main__":
    unittest.main()
