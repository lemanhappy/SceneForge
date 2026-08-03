"""Tests for the TTS voiceover pipeline: provider, ffmpeg mux graph, and the
VoiceoverService facade.

No network or real ffmpeg is exercised — the TTS provider and the ffmpeg runner
are injected with fakes so the placement/command logic is tested in isolation.
The feature is designed to degrade to None (un-voiced video) rather than raise.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from audio import (VoiceoverService, build_audio_filter, build_audio_inputs, build_provider_from_section,
                   build_voiceover_filter, extract_sound_effects, mux_audio, mux_voiceover, resolve_bgm_path,
                   resolve_sfx_file)
from audio.models import AudioMixSpec, VoiceClip
from subtitles.models import SubtitleLine, SubtitleTrack


class _FakeProvider:
    """Writes a dummy audio file per line and records what it was asked to say."""

    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def synthesize(self, text, out_path, voice=None):
        self.calls.append((text, voice))
        if text in self.fail_for:
            return None
        with open(out_path, "wb") as f:
            f.write(b"\x00\x01")  # non-empty dummy bytes
        return out_path


def _ok_runner(captured):
    def run(cmd, **kwargs):
        captured.append(cmd)
        # mux_voiceover only writes when ffmpeg succeeds; emulate that here.
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"video")
        return SimpleNamespace(returncode=0, stderr="")
    return run


def _fail_runner(cmd, **kwargs):
    return SimpleNamespace(returncode=1, stderr="boom")


def _track(*lines):
    return SubtitleTrack(lines=list(lines))


class TestVoiceByLanguage(unittest.TestCase):
    def test_default_voice_for_language(self):
        from audio.voice_catalog import default_voice_for
        self.assertEqual(default_voice_for("minimax", "zh-CN"), "male-qn-jingying")  # 中文音色
        self.assertEqual(default_voice_for("openai", "en"), "alloy")                 # 多语种
        self.assertEqual(default_voice_for("openai", "zh-CN"), "alloy")              # 多语种读中文
        self.assertEqual(default_voice_for("nope", "en"), "")                        # 未知 provider


class TestFitToSpeech(unittest.TestCase):
    def test_compute_targets_extends_only_when_speech_longer(self):
        idxs = [0, 1, 2]
        orig = [5.0, 5.0, 5.0]
        spoken = {0: 3.0, 1: 7.0, 2: 0.0}  # shot1 needs more than 5s
        targets, need = VoiceoverService._compute_fit_targets(idxs, orig, spoken, tail_pad=0.4, max_extend=6.0)
        self.assertTrue(need)
        self.assertEqual(targets[0], 5.0)          # 3.0+0.4 < 5 -> stays
        self.assertEqual(targets[1], 7.4)          # 7.0+0.4
        self.assertEqual(targets[2], 5.0)          # no speech -> stays

    def test_compute_targets_caps_extension(self):
        targets, need = VoiceoverService._compute_fit_targets([0], [5.0], {0: 30.0}, tail_pad=0.4, max_extend=6.0)
        self.assertTrue(need)
        self.assertEqual(targets[0], 11.0)         # capped at orig+max_extend

    def test_no_fit_needed_returns_original_starts(self):
        svc = VoiceoverService(provider=_FakeProvider())
        shots = [SimpleNamespace(idx=0), SimpleNamespace(idx=1)]
        clips = [VoiceClip(path="a.mp3", start=0, shot_idx=0, duration=2.0),
                 VoiceClip(path="b.mp3", start=0, shot_idx=1, duration=2.0)]
        starts, fitted = svc._maybe_fit_video("v.mp4", shots, ["0.mp4", "1.mp4"], clips,
                                              ".", None, duration_provider=lambda p: 5.0, runner=None)
        self.assertIsNone(fitted)                  # speech (2s) < clip (5s) -> no padding
        self.assertEqual(starts, {0: 0.0, 1: 5.0}) # cumulative original durations

    def test_fit_disabled_skips(self):
        svc = VoiceoverService(provider=_FakeProvider(), fit_shot_to_speech=False)
        shots = [SimpleNamespace(idx=0)]
        clips = [VoiceClip(path="a.mp3", start=0, shot_idx=0, duration=30.0)]
        starts, fitted = svc._maybe_fit_video("v.mp4", shots, ["0.mp4"], clips,
                                              ".", None, duration_provider=lambda p: 5.0, runner=None)
        self.assertIsNone(fitted)
        self.assertEqual(starts, {0: 0.0})

    def test_from_config_fit_defaults(self):
        svc = VoiceoverService.from_config({"audio": {"tts": {"enabled": True, "api_key": "k"}}})
        self.assertTrue(svc.fit_shot_to_speech)
        self.assertEqual(svc.fit_tail_pad, 0.4)
        self.assertEqual(svc.max_shot_extend, 6.0)


class TestFilterGraph(unittest.TestCase):
    def test_replace_audio_graph(self):
        clips = [VoiceClip(path="a.mp3", start=0.0), VoiceClip(path="b.mp3", start=2.5)]
        graph = build_voiceover_filter(clips, mix_with_original=False)
        self.assertIn("[1:a]adelay=0|0[a1]", graph)
        self.assertIn("[2:a]adelay=2500|2500[a2]", graph)
        self.assertIn("[a1][a2]amix=inputs=2:normalize=0", graph)
        self.assertNotIn("[0:a]", graph)  # original audio dropped
        self.assertIn("apad[mix]", graph)  # preserve video after the final line

    def test_mix_with_original_includes_input_zero(self):
        clips = [VoiceClip(path="a.mp3", start=1.0)]
        graph = build_voiceover_filter(clips, mix_with_original=True)
        self.assertIn("[0:a][a1]amix=inputs=2", graph)


class TestMux(unittest.TestCase):
    def test_command_structure_and_success(self):
        captured = []
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "voiced.mp4")
            clips = [VoiceClip(path=os.path.join(d, "a.mp3"), start=0.0)]
            result = mux_voiceover(os.path.join(d, "v.mp4"), clips, out,
                                   runner=_ok_runner(captured), ffmpeg="ffmpeg")
            self.assertEqual(result, out)
            cmd = captured[0]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-filter_complex", cmd)
            self.assertIn("[mix]", cmd)
            self.assertIn("0:v", cmd)
            self.assertIn("copy", cmd)  # video stream-copied

    def test_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            clips = [VoiceClip(path=os.path.join(d, "a.mp3"), start=0.0)]
            result = mux_voiceover(os.path.join(d, "v.mp4"), clips, os.path.join(d, "o.mp4"),
                                   runner=_fail_runner, ffmpeg="ffmpeg")
            self.assertIsNone(result)

    def test_no_clips_returns_none(self):
        self.assertIsNone(mux_voiceover("v.mp4", [], "o.mp4", ffmpeg="ffmpeg"))


class TestFromConfig(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(VoiceoverService.from_config({}))
        self.assertIsNone(VoiceoverService.from_config({"audio": {"tts": {"enabled": False}}}))

    def test_enabled_with_key_builds_service(self):
        svc = VoiceoverService.from_config({"audio": {"tts": {"enabled": True, "api_key": "k", "voice": "nova"}}})
        self.assertIsNotNone(svc)
        self.assertEqual(svc.provider.voice, "nova")  # voice baked into provider
        self.assertTrue(svc.provider.available)

    def test_minimax_provider_selected(self):
        from audio import MiniMaxTTSProvider
        svc = VoiceoverService.from_config({"audio": {"tts": {
            "enabled": True, "api_key": "k", "provider": "minimax",
            "model": "speech-2.6-hd", "voice_id": "male-qn-badao", "base_url": "https://yunwu.ai/v1",
        }}})
        self.assertIsInstance(svc.provider, MiniMaxTTSProvider)
        self.assertEqual(svc.provider.voice_id, "male-qn-badao")
        self.assertEqual(svc.provider.endpoint, "https://yunwu.ai/minimax/v1/t2a_v2")

    def test_enabled_without_key_has_no_provider(self):
        # No api_key in section and no env fallback -> provider is None, but the
        # service still exists so add_voiceover safely degrades to None.
        saved = {k: os.environ.pop(k) for k in ("SCENEFORGE_TTS_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY") if k in os.environ}
        try:
            svc = VoiceoverService.from_config({"audio": {"tts": {"enabled": True}}})
            self.assertIsNotNone(svc)
            self.assertIsNone(svc.provider)
        finally:
            os.environ.update(saved)

    def test_build_provider_requires_key(self):
        saved = {k: os.environ.pop(k) for k in ("SCENEFORGE_TTS_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY") if k in os.environ}
        try:
            self.assertIsNone(build_provider_from_section({}))
            self.assertIsNotNone(build_provider_from_section({"api_key": "k"}))
        finally:
            os.environ.update(saved)


class TestAddVoiceover(unittest.TestCase):
    def test_synthesizes_and_muxes(self):
        provider = _FakeProvider()
        svc = VoiceoverService(provider=provider, mix_with_original=False)
        track = _track(
            SubtitleLine(text="你好", start=0.0, end=1.0, shot_idx=0),
            SubtitleLine(text="再见", start=1.0, end=2.0, shot_idx=1),
        )
        captured = []
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "final.mp4")
            with open(video, "wb") as f:
                f.write(b"v")
            result = svc.add_voiceover(video, track, d, runner=_ok_runner(captured),
                                       audio_duration_provider=lambda p: 1.0)
            self.assertEqual(result, os.path.join(d, "final_video_voiced.mp4"))
            self.assertEqual([c[0] for c in provider.calls], ["你好", "再见"])
            # one ffmpeg invocation with both synthesized clips as inputs
            self.assertEqual(captured[0].count("-i"), 3)  # 1 video + 2 audio

    def test_no_provider_returns_none(self):
        svc = VoiceoverService(provider=None)
        track = _track(SubtitleLine(text="x", start=0.0, end=1.0))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(svc.add_voiceover(os.path.join(d, "v.mp4"), track, d))

    def test_empty_track_returns_none(self):
        svc = VoiceoverService(provider=_FakeProvider())
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(svc.add_voiceover(os.path.join(d, "v.mp4"), _track(), d))

    def test_all_synthesis_failing_returns_none(self):
        provider = _FakeProvider(fail_for=["你好"])
        svc = VoiceoverService(provider=provider)
        track = _track(SubtitleLine(text="你好", start=0.0, end=1.0))
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "v.mp4")
            with open(video, "wb") as f:
                f.write(b"v")
            self.assertIsNone(svc.add_voiceover(video, track, d, runner=_ok_runner([])))


class TestAudioMixGraph(unittest.TestCase):
    def test_voiceover_sfx_bgm_loudnorm_graph(self):
        spec = AudioMixSpec(
            voiceover=[VoiceClip(path="v.mp3", start=0.0)],
            sfx=[VoiceClip(path="s.mp3", start=3.0)],
            bgm_path="bgm.mp3", bgm_volume=0.15, sfx_volume=0.7,
            loudnorm=True,
        )
        graph = build_audio_filter(spec)
        self.assertIn("[1:a]adelay=0|0[v1]", graph)            # voiceover
        self.assertIn("[2:a]adelay=3000|3000,volume=0.7[x2]", graph)  # sfx with volume
        self.assertIn("[3:a]volume=0.15[bg]", graph)           # bgm bed
        self.assertIn("amix=inputs=3:normalize=0", graph)
        self.assertIn("[premix]loudnorm=I=-16.0:TP=-1.5:LRA=11.0,apad[mix]", graph)

    def test_voiceover_sidechain_ducks_bgm(self):
        spec = AudioMixSpec(
            voiceover=[VoiceClip(path="v1.mp3", start=0.0),
                       VoiceClip(path="v2.mp3", start=2.0)],
            bgm_path="bgm.mp3",
            bgm_ducking=True,
            bgm_duck_threshold=0.02,
            bgm_duck_ratio=8.0,
            bgm_duck_attack_ms=20,
            bgm_duck_release_ms=350,
            loudnorm=False,
        )
        graph = build_audio_filter(spec)
        self.assertIn("[v1][v2]amix=inputs=2", graph)
        self.assertIn("[voicebus]asplit=2[voice][duckkey]", graph)
        self.assertIn(
            "[bgraw][duckkey]sidechaincompress=threshold=0.02:ratio=8.0:attack=20.0:release=350.0[bg]",
            graph,
        )
        self.assertIn("[voice][bg]amix=inputs=2", graph)

    def test_ducking_without_voiceover_keeps_plain_bgm(self):
        graph = build_audio_filter(AudioMixSpec(
            bgm_path="bgm.mp3", bgm_ducking=True, loudnorm=False,
        ))
        self.assertIn("volume=0.2[bg]", graph)
        self.assertNotIn("sidechaincompress", graph)

    def test_no_loudnorm_mixes_straight_to_mix(self):
        spec = AudioMixSpec(voiceover=[VoiceClip(path="v.mp3", start=0.0)], loudnorm=False)
        graph = build_audio_filter(spec)
        self.assertTrue(graph.rstrip().endswith("[mix]"))
        self.assertNotIn("loudnorm", graph)

    def test_inputs_order_and_bgm_loops(self):
        spec = AudioMixSpec(
            voiceover=[VoiceClip(path="v.mp3", start=0.0)],
            sfx=[VoiceClip(path="s.mp3", start=1.0)],
            bgm_path="bgm.mp3",
        )
        inputs = build_audio_inputs(spec)
        self.assertEqual([p for p, _ in inputs], ["v.mp3", "s.mp3", "bgm.mp3"])
        self.assertEqual([loop for _, loop in inputs], [False, False, True])  # bgm loops

    def test_has_added_audio(self):
        self.assertFalse(AudioMixSpec().has_added_audio)
        self.assertTrue(AudioMixSpec(bgm_path="b.mp3").has_added_audio)


class TestMuxAudio(unittest.TestCase):
    def test_stream_loop_before_bgm_input(self):
        captured = []
        with tempfile.TemporaryDirectory() as d:
            spec = AudioMixSpec(bgm_path=os.path.join(d, "bgm.mp3"), loudnorm=False)
            out = os.path.join(d, "o.mp4")
            result = mux_audio(os.path.join(d, "v.mp4"), spec, out,
                               runner=_ok_runner(captured), ffmpeg="ffmpeg",
                               duration_provider=lambda _path: 8.25)
            self.assertEqual(result, out)
            cmd = captured[0]
            # -stream_loop -1 immediately precedes the bgm -i
            i = cmd.index("-stream_loop")
            self.assertEqual(cmd[i + 1], "-1")
            self.assertEqual(cmd[i + 2], "-i")
            self.assertEqual(cmd[cmd.index("-t") + 1], "8.250")

    def test_nothing_to_add_returns_none(self):
        self.assertIsNone(mux_audio("v.mp4", AudioMixSpec(), "o.mp4", ffmpeg="ffmpeg"))


class TestSfx(unittest.TestCase):
    def test_extract_sound_effects(self):
        shot = SimpleNamespace(audio_desc="[Sound Effect] thunder rumbling\n[Speaker] A: hi\n[Sound Effect] door creak")
        self.assertEqual(extract_sound_effects(shot), ["thunder rumbling", "door creak"])

    def test_extract_sfx_from_packed_line(self):
        # sfx text must not swallow the trailing speaker segment on the same line
        shot = SimpleNamespace(audio_desc='[Sound Effect] Deep exhale. [王云宝] (firm): "不，这只是开始。"')
        self.assertEqual(extract_sound_effects(shot), ["Deep exhale."])

    def test_resolve_sfx_file_keyword_match(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("thunder_clap.mp3", "rain_loop.wav", "notes.txt"):
                open(os.path.join(d, name), "w").close()
            self.assertEqual(resolve_sfx_file("loud thunder in distance", d), os.path.join(d, "thunder_clap.mp3"))
            self.assertIsNone(resolve_sfx_file("spaceship hum", d))

    def test_resolve_sfx_no_library(self):
        self.assertIsNone(resolve_sfx_file("thunder", "/no/such/dir"))


class TestBgmResolution(unittest.TestCase):
    def test_explicit_path(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "song.mp3")
            open(f, "w").close()
            self.assertEqual(resolve_bgm_path({"path": f}), f)

    def test_dir_picks_first_audio_file(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "b.mp3"), "w").close()
            open(os.path.join(d, "a.wav"), "w").close()
            open(os.path.join(d, "readme.txt"), "w").close()
            self.assertEqual(resolve_bgm_path({"dir": d}), os.path.join(d, "a.wav"))  # sorted

    def test_missing_returns_none(self):
        self.assertIsNone(resolve_bgm_path({}))
        self.assertIsNone(resolve_bgm_path({"path": "/no/file.mp3"}))


class TestAddAudioCombined(unittest.TestCase):
    def test_bgm_only_no_dialogue(self):
        # BGM applies even with no voiceover provider and no spoken lines.
        with tempfile.TemporaryDirectory() as d:
            bgm = os.path.join(d, "bgm.mp3")
            open(bgm, "w").close()
            svc = VoiceoverService(provider=None, bgm_path=bgm, loudnorm=False)
            video = os.path.join(d, "v.mp4")
            open(video, "w").close()
            captured = []
            result = svc.add_audio(video, _track(), working_dir=d, runner=_ok_runner(captured))
            self.assertEqual(result, os.path.join(d, "final_video_audio.mp4"))

    def test_sfx_placed_at_shot_start(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "boom.mp3"), "w").close()
            svc = VoiceoverService(provider=None, sfx_library=d, loudnorm=False)
            shots = [SimpleNamespace(idx=0, audio_desc=""),
                     SimpleNamespace(idx=1, audio_desc="[Sound Effect] boom")]
            # shot 0 is 2s long, so the shot-1 effect starts at t=2.0
            clips = svc.resolve_sfx_clips(shots, ["v0.mp4", "v1.mp4"], duration_provider=lambda p: 2.0)
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].start, 2.0)

    def test_nothing_configured_returns_none(self):
        svc = VoiceoverService(provider=None)  # no tts, no bgm, no sfx
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "v.mp4"), "w").close()
            self.assertIsNone(svc.add_audio(os.path.join(d, "v.mp4"), _track(), working_dir=d))

    def test_from_config_bgm_only_enables_service(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "song.mp3")
            open(f, "w").close()
            svc = VoiceoverService.from_config({"audio": {"bgm": {"enabled": True, "path": f, "volume": 0.1}}})
            self.assertIsNotNone(svc)
            self.assertIsNone(svc.provider)  # no tts
            self.assertEqual(svc.bgm_path, f)
            self.assertEqual(svc.bgm_volume, 0.1)

    def test_from_config_reads_bgm_ducking_profile(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "song.mp3")
            open(f, "w").close()
            svc = VoiceoverService.from_config({"audio": {
                "bgm": {"enabled": True, "path": f, "ducking": {
                    "enabled": True, "strength": "strong", "attack_ms": 15,
                    "release_ms": 420,
                }}
            }})
            self.assertTrue(svc.bgm_ducking)
            self.assertEqual(svc.bgm_ducking_strength, "strong")
            self.assertEqual(svc.bgm_duck_attack_ms, 15)
            self.assertEqual(svc.bgm_duck_release_ms, 420)


class TestMiniMaxProvider(unittest.TestCase):
    def test_endpoint_derivation_strips_v1(self):
        from audio import MiniMaxTTSProvider
        p = MiniMaxTTSProvider(api_key="k", base_url="https://gw.example/v1")
        self.assertEqual(p.endpoint, "https://gw.example/minimax/v1/t2a_v2")

    def test_synthesize_decodes_hex(self):
        from unittest import mock

        from audio import MiniMaxTTSProvider

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"audio": b"ID3hello".hex()}, "base_resp": {"status_code": 0}}

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"], captured["json"] = url, json
            return _Resp()

        with mock.patch("requests.post", side_effect=fake_post):
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "x.mp3")
                p = MiniMaxTTSProvider(api_key="k", base_url="https://gw/v1",
                                       model="speech-2.6-hd", voice_id="male-qn-badao")
                res = p.synthesize("你好世界", out)
                self.assertEqual(res, out)
                self.assertEqual(open(out, "rb").read(), b"ID3hello")
                self.assertEqual(captured["url"], "https://gw/minimax/v1/t2a_v2")
                self.assertEqual(captured["json"]["voice_setting"]["voice_id"], "male-qn-badao")
                self.assertEqual(captured["json"]["model"], "speech-2.6-hd")


class TestOpenAITTSProvider(unittest.TestCase):
    def test_retries_html_200_before_writing_audio(self):
        from unittest import mock

        from audio import OpenAITTSProvider

        html = SimpleNamespace(
            content=b"<!doctype html><html></html>",
            headers={"Content-Type": "text/html"},
            raise_for_status=lambda: None,
        )
        audio = SimpleNamespace(
            content=b"ID3valid-audio",
            headers={"Content-Type": "audio/mpeg"},
            raise_for_status=lambda: None,
        )
        with mock.patch("requests.post", side_effect=[html, audio]) as post, \
                mock.patch("audio.tts_provider.time.sleep"):
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "line.mp3")
                provider = OpenAITTSProvider("k", "tts-1", "https://gw/v1")
                self.assertEqual(provider.synthesize("hello", out), out)
                self.assertEqual(open(out, "rb").read(), b"ID3valid-audio")
                self.assertEqual(post.call_count, 2)

    def test_does_not_write_persistent_html_response(self):
        from unittest import mock

        from audio import OpenAITTSProvider

        html = SimpleNamespace(
            content=b"<!doctype html><html></html>",
            headers={"Content-Type": "text/html"},
            raise_for_status=lambda: None,
        )
        with mock.patch("requests.post", return_value=html), \
                mock.patch("audio.tts_provider.time.sleep"):
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "line.mp3")
                provider = OpenAITTSProvider("k", "tts-1", "https://gw/v1", attempts=2)
                self.assertIsNone(provider.synthesize("hello", out))
                self.assertFalse(os.path.exists(out))


class TestRetiming(unittest.TestCase):
    def test_shot_starts_cumulative(self):
        svc = VoiceoverService(provider=None)
        shots = [SimpleNamespace(idx=0), SimpleNamespace(idx=1), SimpleNamespace(idx=2)]
        starts = svc._shot_starts(shots, ["a", "b", "c"], duration_provider=lambda p: 3.0)
        self.assertEqual(starts, {0: 0.0, 1: 3.0, 2: 6.0})

    def test_retime_track_uses_tts_durations_within_shot(self):
        svc = VoiceoverService(provider=None)
        track = _track(
            SubtitleLine(text="A", start=0.0, end=9.9, shot_idx=0),
            SubtitleLine(text="B", start=0.0, end=9.9, shot_idx=0),  # second line same shot
            SubtitleLine(text="C", start=0.0, end=9.9, shot_idx=1),
        )
        clips = [
            VoiceClip(path="0.mp3", duration=1.5, shot_idx=0),
            VoiceClip(path="1.mp3", duration=2.0, shot_idx=0),
            VoiceClip(path="2.mp3", duration=1.0, shot_idx=1),
        ]
        retimed = svc.retime_track(track, clips, shot_starts={0: 0.0, 1: 5.0})
        # shot 0: A at 0..1.5, B sequentially at 1.5..3.5; shot 1: C anchored at 5.0..6.0
        self.assertEqual((retimed.lines[0].start, retimed.lines[0].end), (0.0, 1.5))
        self.assertEqual((retimed.lines[1].start, retimed.lines[1].end), (1.5, 3.5))
        self.assertEqual((retimed.lines[2].start, retimed.lines[2].end), (5.0, 6.0))
        # clips' placement starts are updated to match
        self.assertEqual([c.start for c in clips], [0.0, 1.5, 5.0])

    def test_synthesize_measures_duration(self):
        provider = _FakeProvider()
        svc = VoiceoverService(provider=provider)
        track = _track(SubtitleLine(text="hi", start=0.0, end=1.0, shot_idx=0))
        with tempfile.TemporaryDirectory() as d:
            clips = svc.synthesize_track(track, d, audio_duration_provider=lambda p: 2.7)
            self.assertEqual(clips[0].duration, 2.7)

    def test_render_audio_returns_retimed_track(self):
        provider = _FakeProvider()
        svc = VoiceoverService(provider=provider, loudnorm=False)
        track = _track(
            SubtitleLine(text="你好", start=0.0, end=5.0, shot_idx=0),
            SubtitleLine(text="再见", start=0.0, end=5.0, shot_idx=1),
        )
        shots = [SimpleNamespace(idx=0, audio_desc=""), SimpleNamespace(idx=1, audio_desc="")]
        captured = []
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "v.mp4")
            open(video, "w").close()
            processed, retimed = svc.render_audio(
                video, track, shot_descriptions=shots, video_paths=["v0.mp4", "v1.mp4"],
                working_dir=d, runner=_ok_runner(captured),
                duration_provider=lambda p: 4.0, audio_duration_provider=lambda p: 1.2,
            )
            self.assertEqual(processed, os.path.join(d, "final_video_audio.mp4"))
            # line 0 in shot 0 -> 0..1.2 ; line 1 in shot 1 (starts at 4.0) -> 4.0..5.2
            self.assertEqual((retimed.lines[0].start, retimed.lines[0].end), (0.0, 1.2))
            self.assertEqual((retimed.lines[1].start, retimed.lines[1].end), (4.0, 5.2))


if __name__ == "__main__":
    unittest.main()
