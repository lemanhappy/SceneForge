"""Tests for the SFX library settings service."""

import asyncio
import base64
import os
import tempfile
import unittest

import yaml

from server.sfx_service import SfxService
from server.sfx_api import SfxAPI


def _svc(d):
    lib = os.path.join(d, "sfx"); os.makedirs(lib)
    cfg = os.path.join(d, "c.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.safe_dump({"audio": {}}, f)
    return SfxService(library_dir=lib, config_paths=[cfg]), cfg, lib


class TestSfxService(unittest.TestCase):
    def test_update_writes_audio_sfx(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg, lib = _svc(d)
            state = svc.update(enabled=True, volume=0.6)
            self.assertTrue(state["enabled"]); self.assertEqual(state["volume"], 0.6)
            sfx = yaml.safe_load(open(cfg, encoding="utf-8"))["audio"]["sfx"]
            self.assertTrue(sfx["enabled"]); self.assertEqual(sfx["volume"], 0.6)
            self.assertEqual(sfx["library"], lib)

    def test_upload_lists_file(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _, _ = _svc(d)
            svc.upload("thunder clap.mp3", base64.b64encode(b"x").decode())
            self.assertIn("thunder_clap.mp3", svc.get()["files"])

    def test_api_routes(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _, _ = _svc(d)
            api = SfxAPI(svc)
            self.assertEqual(asyncio.run(api.handle("GET", "/api/sfx", {}))[0], 200)
            self.assertEqual(asyncio.run(api.handle("PUT", "/api/sfx", {"enabled": True, "volume": 0.5}))[0], 200)
            self.assertEqual(asyncio.run(api.handle("GET", "/api/sfx/nope", {}))[0], 404)


if __name__ == "__main__":
    unittest.main()
