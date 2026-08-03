"""Tests for TTS voice selection: settings service (persist into audio.tts),
preview, and the HTTP API."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

import yaml

from server.voice_service import VoiceService
from server.voice_api import VoiceAPI


def _cfg(path, tts=None):
    data = {"audio": {"tts": tts}} if tts is not None else {"audio": {}}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)


class TestService(unittest.TestCase):
    def test_get_reports_minimax_voice_id(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.yaml")
            _cfg(cfg, {"enabled": True, "provider": "minimax", "model": "speech-2.6-hd", "voice_id": "male-qn-badao"})
            s = VoiceService(config_paths=[cfg]).get()
            self.assertEqual(s["provider"], "minimax")
            self.assertEqual(s["voice"], "male-qn-badao")
            self.assertIn("minimax", s["catalog"])

    def test_update_minimax_writes_voice_id(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.yaml")
            _cfg(cfg, {"enabled": False})
            svc = VoiceService(config_paths=[cfg])
            svc.update(enabled=True, provider="minimax", model="speech-2.6-hd", voice="male-qn-jingying")
            tts = yaml.safe_load(open(cfg, encoding="utf-8"))["audio"]["tts"]
            self.assertEqual(tts["provider"], "minimax")
            self.assertEqual(tts["voice_id"], "male-qn-jingying")
            self.assertTrue(tts["enabled"])

    def test_update_openai_writes_voice(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.yaml")
            _cfg(cfg, {})
            VoiceService(config_paths=[cfg]).update(enabled=True, provider="openai", model="tts-1", voice="onyx")
            tts = yaml.safe_load(open(cfg, encoding="utf-8"))["audio"]["tts"]
            self.assertEqual(tts["voice"], "onyx")
            self.assertEqual(tts["provider"], "openai")

    def test_preview_without_key_raises(self):
        saved = {k: os.environ.pop(k) for k in ("SCENEFORGE_TTS_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY") if k in os.environ}
        try:
            with tempfile.TemporaryDirectory() as d:
                svc = VoiceService(config_paths=[os.path.join(d, "c.yaml")], workspace_root=d)
                with self.assertRaises(ValueError):
                    svc.preview("minimax", "speech-2.6-hd", "male-qn-jingying")
        finally:
            os.environ.update(saved)

    def test_preview_returns_base64(self):
        class _FakeProvider:
            def synthesize(self, text, out_path, voice=None):
                with open(out_path, "wb") as f:
                    f.write(b"AUDIODATA")
                return out_path
        with tempfile.TemporaryDirectory() as d:
            svc = VoiceService(config_paths=[os.path.join(d, "c.yaml")], workspace_root=d)
            with mock.patch("audio.service.make_provider", return_value=_FakeProvider()), \
                 mock.patch("agent_runtime.config.tts_api_key", return_value="k"), \
                 mock.patch("agent_runtime.config.tts_base_url", return_value="https://yunwu.ai/v1"):
                out = svc.preview("minimax", "speech-2.6-hd", "male-qn-jingying")
                import base64
                self.assertEqual(base64.b64decode(out["audio_b64"]), b"AUDIODATA")
                self.assertEqual(out["format"], "mp3")

    def test_preview_caches_default_sample(self):
        calls = {"n": 0}
        class _CountingProvider:
            def synthesize(self, text, out_path, voice=None):
                calls["n"] += 1
                with open(out_path, "wb") as f:
                    f.write(b"AUDIO")
                return out_path
        with tempfile.TemporaryDirectory() as d:
            svc = VoiceService(config_paths=[os.path.join(d, "c.yaml")], workspace_root=d)
            with mock.patch("audio.service.make_provider", return_value=_CountingProvider()), \
                 mock.patch("agent_runtime.config.tts_api_key", return_value="k"), \
                 mock.patch("agent_runtime.config.tts_base_url", return_value="https://yunwu.ai/v1"):
                first = svc.preview("openai", "tts-1", "alloy")          # synthesizes + caches
                second = svc.preview("openai", "tts-1", "alloy")         # served from cache
            self.assertEqual(calls["n"], 1)                              # provider hit only once
            self.assertTrue(second.get("cached"))
            self.assertEqual(first["audio_b64"], second["audio_b64"])


class TestApi(unittest.TestCase):
    def _api(self, d):
        cfg = os.path.join(d, "c.yaml")
        _cfg(cfg, {"enabled": False, "provider": "openai"})
        return VoiceAPI(VoiceService(config_paths=[cfg], workspace_root=d))

    def test_get_and_put(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            status, body = asyncio.run(api.handle("GET", "/api/voice", {}))
            self.assertEqual(status, 200)
            self.assertIn("catalog", body)
            status, body = asyncio.run(api.handle("PUT", "/api/voice",
                                       {"enabled": True, "provider": "minimax", "model": "speech-2.6-hd", "voice": "male-qn-badao"}))
            self.assertEqual(status, 200)
            self.assertEqual(body["voice"], "male-qn-badao")

    def test_unknown_route(self):
        with tempfile.TemporaryDirectory() as d:
            status, _ = asyncio.run(self._api(d).handle("GET", "/api/voice/nope", {}))
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
