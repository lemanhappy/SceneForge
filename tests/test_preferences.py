"""Tests for preference/template memory."""

import asyncio
import json
import os
import tempfile
import unittest

from server.preference_service import PreferenceService
from server.templates_api import TemplatesAPI


class TestPreferenceService(unittest.TestCase):
    def test_remember_and_get(self):
        with tempfile.TemporaryDirectory() as d:
            svc = PreferenceService(os.path.join(d, "p.json"))
            self.assertEqual(svc.get()["last_used"], {})
            svc.remember("电影感", "解说+独白")
            self.assertEqual(svc.get()["last_used"], {"style": "电影感", "user_requirement": "解说+独白"})

    def test_save_and_delete_template(self):
        with tempfile.TemporaryDirectory() as d:
            svc = PreferenceService(os.path.join(d, "p.json"))
            svc.save_template("修仙短剧", "国风玄幻", "前3秒钩子", note="ep series")
            tpls = svc.get()["templates"]
            self.assertEqual(len(tpls), 1)
            self.assertEqual(tpls[0]["name"], "修仙短剧")
            self.assertEqual(tpls[0]["style"], "国风玄幻")
            svc.delete_template("修仙短剧")
            self.assertEqual(svc.get()["templates"], [])

    def test_save_requires_name(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                PreferenceService(os.path.join(d, "p.json")).save_template("", "s", "r")

    def test_persisted_to_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p.json")
            PreferenceService(p).save_template("t1", "s1", "r1")
            # fresh instance reads from disk
            self.assertEqual(PreferenceService(p).get()["templates"][0]["name"], "t1")
            self.assertIn("t1", json.loads(open(p, encoding="utf-8").read())["templates"])


class TestApi(unittest.TestCase):
    def _api(self, d):
        return TemplatesAPI(PreferenceService(os.path.join(d, "p.json")))

    def test_routes(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            self.assertEqual(asyncio.run(api.handle("GET", "/api/templates", {}))[0], 200)
            s, b = asyncio.run(api.handle("POST", "/api/templates", {"name": "t", "style": "x"}))
            self.assertEqual(s, 200)
            self.assertEqual(b["templates"][0]["name"], "t")
            s, _ = asyncio.run(api.handle("POST", "/api/templates/remember", {"style": "y"}))
            self.assertEqual(s, 200)
            s, b = asyncio.run(api.handle("DELETE", "/api/templates/t", {}))
            self.assertEqual(s, 200)
            self.assertEqual(b["templates"], [])


if __name__ == "__main__":
    unittest.main()
