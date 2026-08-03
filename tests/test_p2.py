"""Tests for P2: cost estimate + disk housekeeping."""

import os
import tempfile
import unittest
from pathlib import Path

from services.cost import CostEstimator
from services.housekeeping import HousekeepingService


def _make_session(d, scenes=1, shots=2, with_final=True):
    """Build a fake working dir: scene_N/shots/<i>/{first_frame.png,video.mp4} +
    audio/line_*.mp3 + final_video*.mp4 + poster.jpg + script.json."""
    root = Path(d)
    for s in range(scenes):
        base = root / (f"scene_{s}" if scenes > 1 else "")
        for i in range(shots):
            shot = base / "shots" / str(i)
            shot.mkdir(parents=True, exist_ok=True)
            (shot / "first_frame.png").write_bytes(b"x" * 100)
            (shot / "last_frame.png").write_bytes(b"x" * 100)
            (shot / "video.mp4").write_bytes(b"v" * 1000)
        audio = base / "audio"
        audio.mkdir(parents=True, exist_ok=True)
        (audio / "line_000.mp3").write_bytes(b"a" * 50)
        (audio / "line_001.mp3").write_bytes(b"a" * 50)
    (root / "script.json").write_text("{}", encoding="utf-8")
    (root / "poster.jpg").write_bytes(b"p" * 200)
    if with_final:
        (root / "final_video_with_subtitles.mp4").write_bytes(b"f" * 5000)


class TestCost(unittest.TestCase):
    def test_counts_and_total(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, scenes=1, shots=2)
            est = CostEstimator(unit_prices={"video_clip": 3, "image": 0.1, "tts_line": 0.03, "llm_flat": 0.3})
            r = est.estimate(d)
            self.assertEqual(r["counts"]["video_clips"], 2)
            self.assertEqual(r["counts"]["images"], 4)
            self.assertEqual(r["counts"]["tts_lines"], 2)
            # 2*3 + 4*0.1 + 2*0.03 + 1*0.3 = 6 + 0.4 + 0.06 + 0.3 = 6.76
            self.assertAlmostEqual(r["estimated_total"], 6.76, places=2)

    def test_multi_scene_counts(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, scenes=2, shots=2)
            r = CostEstimator().estimate(d)
            self.assertEqual(r["counts"]["video_clips"], 4)
            self.assertEqual(r["counts"]["scenes"], 2)

    def test_from_config(self):
        est = CostEstimator.from_config({"cost": {"currency": "$", "unit_prices": {"video_clip": 0.5}}})
        self.assertEqual(est.currency, "$")
        self.assertEqual(est.unit_prices["video_clip"], 0.5)

    def test_missing_dir(self):
        self.assertEqual(CostEstimator().estimate("/no/such/dir")["estimated_total"], 0)

    def test_estimate_plan_from_counts(self):
        est = CostEstimator(unit_prices={"video_clip": 3, "image": 0.1, "tts_line": 0.03, "llm_flat": 0.3})
        r = est.estimate_plan(scenes=2, shots=6)
        # Balanced renders two candidates for each first/last frame.
        # 6*3 + 6*2*2*0.1 + 2*0.3 = 18 + 2.4 + 0.6 = 21.0
        self.assertEqual(r["estimated_total"], 21.0)
        self.assertEqual(r["shots"], 6)
        self.assertEqual(r["request_counts"]["planned_image_generations"], 24)

    def test_estimate_plan_respects_quality_tier_and_returns_range(self):
        est = CostEstimator(unit_prices={"video_clip": 1, "image": 0, "llm_flat": 0})
        economy = est.estimate_plan(scenes=1, shots=10, quality_tier="economy")
        quality = est.estimate_plan(scenes=1, shots=10, quality_tier="quality")
        self.assertEqual(economy["estimated_total"], 7.0)
        self.assertEqual(quality["estimated_total"], 32.0)
        self.assertEqual(quality["request_counts"]["planned_video_generations"], 20)
        self.assertLess(economy["estimated_min"], economy["estimated_max"])

    def test_plan_summary_text(self):
        s = CostEstimator(currency="¥").plan_summary(2, 6)
        self.assertIn("2 场景", s)
        self.assertIn("6 镜", s)
        self.assertIn("¥", s)


class TestHousekeeping(unittest.TestCase):
    def test_preview_reports_freeable(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, shots=2)
            p = HousekeepingService().preview(d)
            self.assertTrue(p["has_final"])
            self.assertGreater(p["freeable_bytes"], 0)
            # 2 video.mp4 (1000) + 4 png (100) = 2400 bytes
            self.assertEqual(p["freeable_bytes"], 2 * 1000 + 4 * 100)

    def test_cleanup_removes_intermediates_keeps_deliverables(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, shots=2)
            r = HousekeepingService().cleanup(d)
            self.assertTrue(r["ok"])
            self.assertGreater(r["freed_bytes"], 0)
            root = Path(d)
            # deliverables preserved
            self.assertTrue((root / "final_video_with_subtitles.mp4").exists())
            self.assertTrue((root / "poster.jpg").exists())
            self.assertTrue((root / "script.json").exists())
            # intermediates gone
            self.assertFalse((root / "shots" / "0" / "video.mp4").exists())
            self.assertFalse((root / "shots" / "0" / "first_frame.png").exists())

    def test_cleanup_refuses_without_final(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, shots=1, with_final=False)
            r = HousekeepingService().cleanup(d)
            self.assertFalse(r["ok"])
            self.assertTrue((Path(d) / "shots" / "0" / "video.mp4").exists())  # nothing deleted

    def test_cleanup_removes_archive_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            _make_session(d, shots=1)
            arch = Path(d) / "shots" / "0" / "_archive" / "v1"
            arch.mkdir(parents=True)
            (arch / "video.mp4").write_bytes(b"old" * 100)
            HousekeepingService().cleanup(d)
            self.assertFalse((Path(d) / "shots" / "0" / "_archive").exists())


if __name__ == "__main__":
    unittest.main()
