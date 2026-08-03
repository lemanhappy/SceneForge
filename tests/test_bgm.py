"""Tests for the background-music feature: library (list/upload/safety),
settings service (persist selection into pipeline configs), and the HTTP API.
"""

import asyncio
import base64
import os
import tempfile
import unittest

import yaml

from audio.bgm_library import BgmLibrary
from server.bgm_service import BgmService
from server.bgm_api import BgmAPI


class TestLibrary(unittest.TestCase):
    def test_list_filters_and_sorts(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("b.mp3", "a.wav", "notes.txt", "c.flac"):
                open(os.path.join(d, n), "w").close()
            self.assertEqual(BgmLibrary(d).list_tracks(), ["a.wav", "b.mp3", "c.flac"])

    def test_add_track_sanitizes_and_avoids_collision(self):
        with tempfile.TemporaryDirectory() as d:
            lib = BgmLibrary(d)
            n1 = lib.add_track("My Song!.mp3", b"x")
            n2 = lib.add_track("My Song!.mp3", b"y")
            self.assertEqual(n1, "My_Song.mp3")
            self.assertEqual(n2, "My_Song-1.mp3")
            self.assertEqual(set(lib.list_tracks()), {"My_Song.mp3", "My_Song-1.mp3"})

    def test_add_track_forces_audio_ext(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(BgmLibrary(d).add_track("evil.exe", b"x").endswith(".mp3"))

    def test_track_path_rejects_traversal_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            lib = BgmLibrary(d)
            lib.add_track("ok.mp3", b"x")
            self.assertIsNotNone(lib.track_path("ok.mp3"))
            self.assertIsNone(lib.track_path("../secret.mp3"))
            self.assertIsNone(lib.track_path("missing.mp3"))


def _write_cfg(path, bgm=None):
    data = {"audio": {"bgm": bgm}} if bgm is not None else {"audio": {}}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)


class TestService(unittest.TestCase):
    def _setup(self, d):
        lib_dir = os.path.join(d, "bgm")
        os.makedirs(lib_dir)
        open(os.path.join(lib_dir, "epic.mp3"), "w").close()
        cfg = os.path.join(d, "script2video.yaml")
        _write_cfg(cfg, {"enabled": False, "volume": 0.2})
        return BgmService(library_dir=lib_dir, config_paths=[cfg]), cfg, lib_dir

    def test_get_lists_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _, _ = self._setup(d)
            state = svc.get()
            self.assertEqual(state["tracks"], ["epic.mp3"])
            self.assertFalse(state["enabled"])
            self.assertIsNone(state["track"])

    def test_update_writes_path_into_config(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg, lib_dir = self._setup(d)
            state = svc.update(enabled=True, track="epic.mp3", volume=0.3,
                               ducking_enabled=True, ducking_strength="strong")
            self.assertTrue(state["enabled"])
            self.assertEqual(state["track"], "epic.mp3")
            self.assertEqual(state["volume"], 0.3)
            written = yaml.safe_load(open(cfg, encoding="utf-8"))["audio"]["bgm"]
            self.assertTrue(written["enabled"])
            self.assertEqual(written["volume"], 0.3)
            self.assertEqual(os.path.basename(written["path"]), "epic.mp3")
            self.assertTrue(written["ducking"]["enabled"])
            self.assertEqual(written["ducking"]["strength"], "strong")

    def test_update_no_track_clears_path(self):
        with tempfile.TemporaryDirectory() as d:
            svc, cfg, _ = self._setup(d)
            svc.update(enabled=True, track="epic.mp3", volume=0.2)
            svc.update(enabled=False, track=None, volume=0.2)
            written = yaml.safe_load(open(cfg, encoding="utf-8"))["audio"]["bgm"]
            self.assertIsNone(written["path"])
            self.assertFalse(written["enabled"])

    def test_upload_adds_to_library(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _, lib_dir = self._setup(d)
            b64 = base64.b64encode(b"music-bytes").decode("ascii")
            state = svc.upload("custom song.mp3", b64)
            self.assertIn("custom_song.mp3", state["tracks"])
            self.assertEqual(open(os.path.join(lib_dir, "custom_song.mp3"), "rb").read(), b"music-bytes")

    def test_upload_empty_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            svc, _, _ = self._setup(d)
            with self.assertRaises(ValueError):
                svc.upload("x.mp3", "")


class TestApi(unittest.TestCase):
    def _api(self, d):
        lib_dir = os.path.join(d, "bgm")
        os.makedirs(lib_dir)
        open(os.path.join(lib_dir, "epic.mp3"), "w").close()
        cfg = os.path.join(d, "c.yaml")
        _write_cfg(cfg, {"enabled": False, "volume": 0.2})
        return BgmAPI(BgmService(library_dir=lib_dir, config_paths=[cfg]))

    def test_get(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            status, body = asyncio.run(api.handle("GET", "/api/bgm", {}))
            self.assertEqual(status, 200)
            self.assertEqual(body["tracks"], ["epic.mp3"])

    def test_put_select(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            status, body = asyncio.run(api.handle("PUT", "/api/bgm",
                                       {"enabled": True, "track": "epic.mp3", "volume": 0.4,
                                        "ducking_enabled": True, "ducking_strength": "gentle"}))
            self.assertEqual(status, 200)
            self.assertTrue(body["enabled"])
            self.assertEqual(body["volume"], 0.4)
            self.assertTrue(body["ducking_enabled"])
            self.assertEqual(body["ducking_strength"], "gentle")

    def test_upload_route(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            b64 = base64.b64encode(b"abc").decode("ascii")
            status, body = asyncio.run(api.handle("POST", "/api/bgm/upload",
                                       {"filename": "new.mp3", "data_b64": b64}))
            self.assertEqual(status, 200)
            self.assertIn("new.mp3", body["tracks"])

    def test_unknown_route(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            status, _ = asyncio.run(api.handle("GET", "/api/bgm/nope", {}))
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
