"""Tests for the production feature-settings service (toggles persisted to YAML)."""

import asyncio
import os
import tempfile
import unittest

import yaml

from server.features_service import FeaturesService, _get_path, _set_path
from server.features_api import FeaturesAPI


class TestPathHelpers(unittest.TestCase):
    def test_set_and_get_nested(self):
        d = {}
        _set_path(d, "video.transition.type", "crossfade")
        self.assertEqual(d, {"video": {"transition": {"type": "crossfade"}}})
        self.assertEqual(_get_path(d, "video.transition.type"), "crossfade")
        self.assertEqual(_get_path(d, "video.missing", "x"), "x")


class TestFeaturesService(unittest.TestCase):
    def _svc(self, d):
        cfg = os.path.join(d, "c.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            yaml.safe_dump({"video": {"transition": {"type": "none"}}}, f)
        return FeaturesService(config_paths=[cfg]), cfg

    def test_get_returns_groups_with_values(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _ = self._svc(d)
            state = svc.get()
            titles = [g["title"] for g in state["groups"]]
            self.assertIn("转场", titles)
            self.assertIn("AIGC 合规标识", titles)
            # current value reflected
            trans = next(f for g in state["groups"] for f in g["fields"] if f["path"] == "video.transition.type")
            self.assertEqual(trans["value"], "none")

    def test_get_exposes_categories_and_field_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _ = self._svc(d)
            state = svc.get()
            # categories listed + every group tagged with one
            self.assertEqual(state["categories"], ["画面", "文案与字幕", "合规与质量", "扩展"])
            self.assertTrue(all(g.get("category") for g in state["groups"]))
            # bounded numeric field carries slider bounds + direction labels + hint
            thr = next(f for g in state["groups"] for f in g["fields"]
                       if f["path"] == "quality.consistency.threshold")
            self.assertEqual((thr["min"], thr["max"]), (0.0, 1.0))
            self.assertEqual(thr["min_label"], "宽松")
            self.assertEqual(thr["max_label"], "严格")
            self.assertIn("越接近 1 越严格", thr["hint"])

    def test_update_writes_typed_values(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg = self._svc(d)
            svc.update({
                "video.transition.type": "crossfade",
                "video.transition.duration": "0.8",
                "subtitle.burn_in": True,
                "subtitle.style.font_size": "48",
                "compliance.aigc_label.enabled": False,
            })
            data = yaml.safe_load(open(cfg, encoding="utf-8"))
            self.assertEqual(data["video"]["transition"]["type"], "crossfade")
            self.assertEqual(data["video"]["transition"]["duration"], 0.8)
            self.assertIs(data["subtitle"]["burn_in"], True)
            self.assertEqual(data["subtitle"]["style"]["font_size"], 48)  # coerced to int
            self.assertIs(data["compliance"]["aigc_label"]["enabled"], False)

    def test_keywords_list_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg = self._svc(d)
            svc.update({"moderation.keywords": "涉政, 暴恐、赌博"})
            data = yaml.safe_load(open(cfg, encoding="utf-8"))
            self.assertEqual(data["moderation"]["keywords"], ["涉政", "暴恐", "赌博"])
            # get() shows them joined for display
            kw = next(f for g in svc.get()["groups"] for f in g["fields"] if f["path"] == "moderation.keywords")
            self.assertEqual(kw["value"], "涉政、暴恐、赌博")

    def test_unknown_path_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg = self._svc(d)
            svc.update({"evil.path": "x", "video.transition.type": "fade"})
            data = yaml.safe_load(open(cfg, encoding="utf-8"))
            self.assertNotIn("evil", data)
            self.assertEqual(data["video"]["transition"]["type"], "fade")


class TestApi(unittest.TestCase):
    def test_get_and_put(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.yaml")
            open(cfg, "w").close()
            api = FeaturesAPI(FeaturesService(config_paths=[cfg]))
            status, body = asyncio.run(api.handle("GET", "/api/features", {}))
            self.assertEqual(status, 200)
            self.assertIn("groups", body)
            status, _ = asyncio.run(api.handle("PUT", "/api/features", {"video.transition.type": "fade"}))
            self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
