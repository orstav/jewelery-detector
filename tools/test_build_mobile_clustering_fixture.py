from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("build_mobile_clustering_fixture.py")
SPEC = importlib.util.spec_from_file_location("build_mobile_clustering_fixture", MODULE_PATH)
assert SPEC and SPEC.loader and hasattr(SPEC.loader, "exec_module")
fixture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fixture
SPEC.loader.exec_module(fixture)


class MobileClusteringFixtureTests(unittest.TestCase):
    def test_canonical_stage_drives_mobile_actionability(self) -> None:
        self.assertTrue(
            fixture.is_mobile_actionable(
                {"source_funnel": "raw_intake", "canonical_stage": "identity_classified", "status": "pending_review"}
            )
        )
        self.assertFalse(
            fixture.is_mobile_actionable(
                {"source_funnel": "raw_intake", "canonical_stage": "facts_needed", "status": "waiting_on_parents"}
            )
        )
        self.assertFalse(
            fixture.is_mobile_actionable(
                {"source_funnel": "dropbox_coverage_repair", "canonical_stage": "identity_classified", "status": "dropbox_possible_duplicate_visual_confirm"}
            )
        )

    def test_legacy_status_fallback_keeps_old_staging_safe(self) -> None:
        self.assertTrue(fixture.is_mobile_actionable({"status": "pending_review"}))
        self.assertFalse(fixture.is_mobile_actionable({"status": "published_active_validated"}))


if __name__ == "__main__":
    unittest.main()
