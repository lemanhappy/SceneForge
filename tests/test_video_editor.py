"""Tests for imported-video post-production (library + apply orchestration).

ASR and real ffmpeg are not exercised; apply() reuses subtitle/audio building
blocks which have their own tests, so here we verify library safety + that
apply() wires the right operations (with mux/burn stubbed via monkeypatch)."""

import asyncio
import base64
import os
import tempfile
import unittest
from unittest import mock

from editing.video_editor import VideoEditService, _safe_name


class TestLibrary(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(_safe_name("My Clip!.mp4"), "My_Clip.mp4")
        self.assertTrue(_safe_name("x.exe").endswith(".mp4"))  # non-video ext forced

    def test_upload_and_list(self):
        with tempfile.TemporaryDirectory() as d:
            svc = VideoEditService(imports_dir=os.path.join(d, "imports"))
            svc.upload("clip a.mp4", base64.b64encode(b"video-bytes").decode())
            self.assertIn("clip_a.mp4", svc.list_videos())

    def test_resolve_missing_raises(self):
        with tempfile.TemporaryDirectory() as d:
            svc = VideoEditService(imports_dir=os.path.join(d, "imports"))
            with self.assertRaises(FileNotFoundError):
                svc._resolve("nope.mp4")

    def test_upload_empty_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                VideoEditService(imports_dir=os.path.join(d, "imports")).upload("x.mp4", "")


class TestApply(unittest.IsolatedAsyncioTestCase):
    async def test_apply_label_only_burns(self):
        with tempfile.TemporaryDirectory() as d:
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            video = os.path.join(imports, "v.mp4"); open(video, "wb").write(b"v")
            svc = VideoEditService(imports_dir=imports)
            captured = {}
            def fake_burn(cur, ass, out, metadata=None):
                captured["ass"], captured["metadata"] = ass, metadata
                open(out, "wb").write(b"burned"); return out
            with mock.patch("subtitles.renderer.burn_in", side_effect=fake_burn):
                res = await svc.apply("v.mp4", {"aigc_label": {"text": "AI生成", "position": "bottom_right"},
                                                "subtitles": [], "voiceover": []})
            self.assertTrue(res["output"].endswith("_edited.mp4"))
            self.assertEqual(captured["metadata"]["AIGC"], "true")

    async def test_apply_noop_returns_note(self):
        with tempfile.TemporaryDirectory() as d:
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            open(os.path.join(imports, "v.mp4"), "wb").write(b"v")
            svc = VideoEditService(imports_dir=imports)
            res = await svc.apply("v.mp4", {"subtitles": [], "voiceover": [], "aigc_label": None})
            self.assertIsNone(res["output"])
            self.assertIn("未指定", res["note"])

    async def test_apply_voiceover_muxes(self):
        with tempfile.TemporaryDirectory() as d:
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            video = os.path.join(imports, "v.mp4"); open(video, "wb").write(b"v")
            svc = VideoEditService(imports_dir=imports)

            async def fake_synth(lines, voice_cfg, out_dir):
                from audio.models import VoiceClip
                return [VoiceClip(path="x.mp3", start=float(l["start"])) for l in lines]
            calls = {}
            def fake_mux(video_path, spec, out):
                calls["clips"] = len(spec.voiceover); open(out, "wb").write(b"m"); return out
            with mock.patch.object(svc, "_synth_voiceover", side_effect=fake_synth), \
                 mock.patch("audio.mixer.mux_audio", side_effect=fake_mux):
                res = await svc.apply("v.mp4", {"voiceover": [{"start": 0, "text": "hi"}],
                                                "subtitles": [], "aigc_label": None, "replace_audio": True})
            self.assertEqual(calls["clips"], 1)
            self.assertTrue(res["output"].endswith("_audio.mp4"))


if __name__ == "__main__":
    unittest.main()
