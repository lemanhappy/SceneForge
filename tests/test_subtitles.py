"""Tests for the subtitle service: extraction, timeline, and .ass/.srt rendering.

ffmpeg burn-in is intentionally not exercised here (environment dependent); it is
designed to degrade to None rather than raise.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from prompting import chinese_runtime_instruction, is_chinese_mode
from subtitles import (
    SubtitleService,
    SubtitleStyle,
    build_timeline,
    extract_spoken_content,
    render_ass,
    render_srt,
)
from subtitles.models import SubtitleLine, SubtitleTrack


def _shot(idx, audio_desc="", motion_desc=""):
    return SimpleNamespace(idx=idx, audio_desc=audio_desc, motion_desc=motion_desc)


class TestExtractor(unittest.TestCase):
    def test_speaker_lines_parsed_sound_effects_skipped(self):
        shot = _shot(
            1,
            audio_desc="[Sound Effect] wind\n[Speaker] 林老师 (温和): 同学们好。\n[Speaker] 小明 (兴奋): 老师好！",
        )
        lines = extract_spoken_content(shot)
        self.assertEqual([l.text for l in lines], ["同学们好。", "老师好！"])
        self.assertEqual([l.speaker for l in lines], ["林老师", "小明"])
        self.assertTrue(all(l.shot_idx == 1 for l in lines))

    def test_speaker_without_emotion(self):
        lines = extract_spoken_content(_shot(0, audio_desc="[Speaker] Alice: Hello"))
        self.assertEqual(lines[0].text, "Hello")
        self.assertEqual(lines[0].speaker, "Alice")

    def test_fallback_to_quoted_motion_desc(self):
        shot = _shot(2, audio_desc="[Sound Effect] only", motion_desc='Narration: "夜色降临。" then she turns.')
        lines = extract_spoken_content(shot)
        self.assertEqual([l.text for l in lines], ["夜色降临。"])

    def test_no_speech_returns_empty(self):
        self.assertEqual(extract_spoken_content(_shot(3, audio_desc="[Sound Effect] rain")), [])

    def test_packed_sfx_and_named_speaker_one_line(self):
        # The model often packs sfx + a [name]-tagged line together on one line.
        shot = _shot(5, audio_desc='[Sound Effect] Deep exhale. [王云宝] (firm): "不，这只是开始。"')
        lines = extract_spoken_content(shot)
        self.assertEqual([l.text for l in lines], ["不，这只是开始。"])
        self.assertEqual(lines[0].speaker, "王云宝")

    def test_narrator_tag_with_and_without_colon(self):
        self.assertEqual([l.text for l in extract_spoken_content(_shot(6, audio_desc='[Narrator]: "他归来了。"'))],
                         ["他归来了。"])
        self.assertEqual([l.text for l in extract_spoken_content(_shot(7, audio_desc='[Narrator] "三年后。"'))],
                         ["三年后。"])

    def test_multiple_speakers_one_shot(self):
        shot = _shot(9, audio_desc='[王云宝] (淡然): "只是开始。" [Narrator]: "他眼中的光从未熄灭。"')
        lines = extract_spoken_content(shot)
        self.assertEqual([l.speaker for l in lines], ["王云宝", "Narrator"])


class TestTimeline(unittest.TestCase):
    def test_lines_confined_to_shot_windows_and_proportional(self):
        durations = {"a.mp4": 4.0, "b.mp4": 6.0}
        shots = [
            ("a.mp4", [SubtitleLine(text="22", shot_idx=0), SubtitleLine(text="22", shot_idx=0)]),
            ("b.mp4", [SubtitleLine(text="hello", shot_idx=1)]),
        ]
        track = build_timeline(shots, duration_provider=lambda p: durations[p])
        # shot a: 2 equal-length lines split 4s -> [0,2],[2,4]
        self.assertAlmostEqual(track.lines[0].start, 0.0)
        self.assertAlmostEqual(track.lines[0].end, 2.0)
        self.assertAlmostEqual(track.lines[1].start, 2.0)
        self.assertAlmostEqual(track.lines[1].end, 4.0)
        # shot b starts at cursor=4 and spans its full 6s
        self.assertAlmostEqual(track.lines[2].start, 4.0)
        self.assertAlmostEqual(track.lines[2].end, 10.0)

    def test_empty_shot_advances_cursor(self):
        shots = [("a.mp4", []), ("b.mp4", [SubtitleLine(text="x", shot_idx=1)])]
        track = build_timeline(shots, duration_provider=lambda p: 3.0)
        self.assertEqual(len(track), 1)
        self.assertAlmostEqual(track.lines[0].start, 3.0)
        self.assertAlmostEqual(track.lines[0].end, 6.0)


class TestRenderers(unittest.TestCase):
    def _track(self):
        return SubtitleTrack(lines=[
            SubtitleLine(text="同学们好。", start=0.0, end=2.5),
            SubtitleLine(text="老师好！", start=2.5, end=4.0),
        ])

    def test_render_ass_structure(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            render_ass(self._track(), path, style=SubtitleStyle(position="bottom", font_size=40))
            content = open(path, encoding="utf-8").read()
            self.assertIn("[Script Info]", content)
            self.assertIn("[V4+ Styles]", content)
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:02.50,Default,,0,0,0,,同学们好。", content)
            self.assertIn("0:00:04.00", content)

    def test_render_srt_structure(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.srt")
            render_srt(self._track(), path)
            content = open(path, encoding="utf-8").read()
            self.assertIn("1\n00:00:00,000 --> 00:00:02,500\n同学们好。", content)
            self.assertIn("2\n00:00:02,500 --> 00:00:04,000\n老师好！", content)

    def test_hook_overlay_event_and_style(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            render_ass(self._track(), path,
                       hook={"text": "三年后，他归来", "seconds": 3, "color": "#FFD24A", "font_size": 80})
            content = open(path, encoding="utf-8").read()
            self.assertIn("Style: Hook,", content)
            # hook is a layer-1 event spanning 0 -> seconds, using the Hook style
            self.assertIn("Dialogue: 1,0:00:00.00,0:00:03.00,Hook,,0,0,0,,三年后，他归来", content)
            # dialogue subtitles still present
            self.assertIn(",Default,,0,0,0,,同学们好。", content)

    def test_aigc_label_persistent_corner(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            render_ass(self._track(), path, label={"text": "AI生成", "position": "bottom_right", "font_size": 30})
            content = open(path, encoding="utf-8").read()
            self.assertIn("Style: Label,", content)
            # layer-2 event from 0 spanning (effectively) the whole video, bottom-right (align 3)
            self.assertIn(",Label,,0,0,0,,AI生成", content)
            self.assertIn("Dialogue: 2,0:00:00.00,", content)
            self.assertIn(",3,", content)  # alignment 3 = bottom-right in the Label style

    def test_label_and_hook_and_subtitles_coexist(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            render_ass(self._track(), path, hook={"text": "钩子", "seconds": 3}, label={"text": "AI生成"})
            content = open(path, encoding="utf-8").read()
            self.assertIn("Style: Hook,", content)
            self.assertIn("Style: Label,", content)
            self.assertIn(",Default,,0,0,0,,同学们好。", content)

    def test_no_hook_when_text_empty(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            render_ass(self._track(), path, hook={"text": "  ", "seconds": 3})
            content = open(path, encoding="utf-8").read()
            self.assertNotIn("Style: Hook,", content)

    def test_hex_color_and_margin(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "final.ass")
            style = SubtitleStyle(primary_color="#FFCC00", margin_v=80)
            render_ass(self._track(), path, style=style)
            content = open(path, encoding="utf-8").read()
            # #RRGGBB -> ASS &HAABBGGRR (alpha 00, then BB GG RR)
            self.assertIn("&H0000CCFF", content)
            # margin_v lands in the MarginV slot of the Style line
            self.assertIn(",20,20,80,1", content)


class TestBurnInCommand(unittest.TestCase):
    """Lock in the Windows-safe ffmpeg invocation: cwd=subtitle dir + basename in
    the filtergraph, so a drive-letter ':' is never parsed as a filter option."""

    def test_uses_cwd_and_basename_not_full_path(self):
        from unittest.mock import patch
        from subtitles import renderer

        with tempfile.TemporaryDirectory() as root:
            sub_dir = os.path.join(root, "subtitles")
            os.makedirs(sub_dir)
            sub = os.path.join(sub_dir, "final.ass")
            open(sub, "w", encoding="utf-8").write("[Events]\n")
            video = os.path.join(root, "v.mp4")
            open(video, "w").write("x")
            output = os.path.join(root, "v_sub.mp4")
            captured = {}

            class _Proc:
                returncode = 0
                stderr = ""

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["cwd"] = kwargs.get("cwd")
                open(output, "w").write("burned")  # satisfy existence check
                return _Proc()

            with patch.object(renderer, "_ffmpeg_exe", lambda: "ffmpeg"), \
                 patch.object(renderer.subprocess, "run", fake_run):
                result = renderer.burn_in(video, sub, output)

            self.assertEqual(result, output)
            self.assertEqual(captured["cwd"], sub_dir)
            vf = captured["cmd"][captured["cmd"].index("-vf") + 1]
            self.assertEqual(vf, "ass=final.ass")  # basename only, no path/colon


class TestSubtitleServiceConfig(unittest.TestCase):
    def test_from_config_disabled(self):
        self.assertIsNone(SubtitleService.from_config({}))
        self.assertIsNone(SubtitleService.from_config({"subtitle": {"enabled": False}}))

    def test_from_config_enabled_reads_style(self):
        svc = SubtitleService.from_config({
            "subtitle": {"enabled": True, "burn_in": False, "style": {"font_size": 50, "position": "top"}}
        })
        self.assertIsNotNone(svc)
        self.assertEqual(svc.style.font_size, 50)
        self.assertEqual(svc.style.position, "top")
        self.assertFalse(svc.burn_in_enabled)
        # burn_in short-circuits to None when disabled
        self.assertIsNone(svc.burn_in("v.mp4", "s.ass", "o.mp4"))

    def test_build_track_end_to_end(self):
        svc = SubtitleService.from_config({"subtitle": {"enabled": True}})
        shots = [_shot(0, audio_desc="[Speaker] A: hi"), _shot(1, audio_desc="[Sound Effect] x")]
        track = svc.build_track(shots, ["a.mp4", "b.mp4"], duration_provider=lambda p: 2.0)
        self.assertEqual(len(track), 1)
        self.assertEqual(track.lines[0].text, "hi")


class _FakeService:
    def __init__(self, track, burn_ok=True):
        self._track = track
        self._burn_ok = burn_ok
        self.built = None

    def build_track(self, shots, video_paths):
        self.built = (list(shots), list(video_paths))
        return self._track

    def render_ass(self, track, path, hook=None, label=None, screen_texts=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write("ass")
        return path

    def render_srt(self, track, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write("srt")
        return path

    def burn_in(self, video, ass, out, metadata=None):
        if not self._burn_ok:
            return None
        open(out, "w", encoding="utf-8").write("burned")
        return out


class TestPipelineSubtitleIntegration(unittest.TestCase):
    """Exercises Script2VideoPipeline._maybe_burn_subtitles wiring via the
    unbound method + a duck-typed service (no moviepy / ffmpeg needed)."""

    def _call(self, service, root, final_name="final_video.mp4"):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        final_path = os.path.join(root, final_name)
        open(final_path, "w", encoding="utf-8").write("video")
        fake_self = SimpleNamespace(subtitle_service=service, working_dir=root)
        shots = [_shot(0, audio_desc="[Speaker] A: hi")]
        return Script2VideoPipeline._maybe_burn_subtitles(fake_self, final_path, shots, quiet=True, progress=None)

    def test_no_speech_returns_original(self):
        with tempfile.TemporaryDirectory() as root:
            out = self._call(_FakeService(SubtitleTrack(lines=[])), root)
            self.assertTrue(out.endswith("final_video.mp4"))

    def test_burn_success_returns_subtitled(self):
        with tempfile.TemporaryDirectory() as root:
            track = SubtitleTrack(lines=[SubtitleLine(text="hi", start=0, end=1)])
            out = self._call(_FakeService(track, burn_ok=True), root)
            self.assertTrue(out.endswith("final_video_with_subtitles.mp4"))
            self.assertTrue(os.path.exists(out))
            self.assertTrue(os.path.exists(os.path.join(root, "subtitles", "final.ass")))
            self.assertTrue(os.path.exists(os.path.join(root, "subtitles", "final.srt")))

    def test_burn_failure_falls_back_to_original(self):
        with tempfile.TemporaryDirectory() as root:
            track = SubtitleTrack(lines=[SubtitleLine(text="hi", start=0, end=1)])
            out = self._call(_FakeService(track, burn_ok=False), root)
            self.assertTrue(out.endswith("final_video.mp4"))

    def test_stale_existing_subtitled_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "final_video_with_subtitles.mp4"), "w").write("old")
            out = self._call(_FakeService(SubtitleTrack(lines=[])), root)
            self.assertTrue(out.endswith("final_video.mp4"))

    def test_fresh_existing_subtitled_is_reused(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        with tempfile.TemporaryDirectory() as root:
            final_path = os.path.join(root, "final_video.mp4")
            subtitled_path = os.path.join(root, "final_video_with_subtitles.mp4")
            open(final_path, "w", encoding="utf-8").write("video")
            open(subtitled_path, "w", encoding="utf-8").write("fresh")
            source_mtime = os.path.getmtime(final_path)
            os.utime(subtitled_path, (source_mtime + 1, source_mtime + 1))
            fake_self = SimpleNamespace(
                subtitle_service=_FakeService(SubtitleTrack(lines=[])),
                working_dir=root,
            )
            shots = [_shot(0, audio_desc="[Speaker] A: hi")]
            out = Script2VideoPipeline._maybe_burn_subtitles(
                fake_self, final_path, shots, quiet=True, progress=None
            )
            self.assertTrue(out.endswith("final_video_with_subtitles.mp4"))


class TestScreenTextOverlay(unittest.TestCase):
    """On-screen ('diegetic') text composited in post (policy 'none')."""

    def test_render_ass_emits_positioned_screen_events(self):
        from subtitles.renderer import render_ass
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "x.ass")
            render_ass(SubtitleTrack(lines=[]), p, screen_texts=[
                {"text": "全员邮件", "start": 0.0, "end": 3.0, "position": "top"},
                {"text": "余额 ¥2,376", "start": 3.0, "end": 6.0, "position": "center"},
            ])
            ass = open(p, encoding="utf-8").read()
            self.assertIn("Style: Screen,", ass)
            self.assertIn("{" + chr(92) + "an8}全员邮件", ass)   # top
            self.assertIn("{" + chr(92) + "an5}余额", ass)       # center
            self.assertIn("Dialogue: 3,", ass)                   # own layer

    def _events(self, shots, *, overlay=True, durs=(2.0, 4.0), final=6.0):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        import subtitles.timeline as tl
        with tempfile.TemporaryDirectory() as root:
            for sd in shots:  # create the per-shot clip files probe() would read
                d = os.path.join(root, "shots", str(sd.idx)); os.makedirs(d, exist_ok=True)
                open(os.path.join(d, "video.mp4"), "w").write("v")
            dur_map = {0: durs[0], 1: durs[1]}
            orig = tl.probe_duration
            tl.probe_duration = lambda path: (final if path.endswith("final.mp4")
                                              else dur_map.get(int(os.path.basename(os.path.dirname(path))), 0.0))
            try:
                fake = SimpleNamespace(screen_text_overlay=overlay, working_dir=root)
                return Script2VideoPipeline._build_screen_text_events(
                    fake, shots, os.path.join(root, "final.mp4"))
            finally:
                tl.probe_duration = orig

    def test_disabled_when_overlay_off(self):
        shots = [SimpleNamespace(idx=0, screen_text="x", screen_text_pos=None)]
        self.assertEqual(self._events(shots, overlay=False), [])

    def test_events_scaled_to_final_duration(self):
        # raw shot windows [0,2],[2,6]; final=12 -> scale x2 -> [0,4],[4,12]
        shots = [SimpleNamespace(idx=0, screen_text="A", screen_text_pos="top"),
                 SimpleNamespace(idx=1, screen_text=None, screen_text_pos=None)]
        ev = self._events(shots, durs=(2.0, 4.0), final=12.0)
        self.assertEqual(len(ev), 1)                     # only the shot with text
        self.assertEqual(ev[0]["text"], "A")
        self.assertAlmostEqual(ev[0]["start"], 0.0)
        self.assertAlmostEqual(ev[0]["end"], 4.0)        # 2.0 * (12/6)
        self.assertEqual(ev[0]["position"], "top")


class TestChineseMode(unittest.TestCase):
    def test_is_chinese_mode(self):
        self.assertFalse(is_chinese_mode({}))
        self.assertTrue(is_chinese_mode({"language": {"chinese_mode": True}}))

    def test_instruction_default_and_override(self):
        self.assertIn("简体中文", chinese_runtime_instruction({}))
        custom = chinese_runtime_instruction({"language": {"chinese_runtime_instruction": "自定义约束"}})
        self.assertEqual(custom, "自定义约束")


class TestChineseInjectionWiring(unittest.TestCase):
    def test_agents_append_instruction_to_system_prompt(self):
        from agents.screenwriter import Screenwriter
        from agents.storyboard_artist import StoryboardArtist

        sw = Screenwriter(chat_model=object(), extra_system_instruction="中文约束")
        self.assertEqual(sw._system("BASE"), "BASE\n\n中文约束")
        self.assertEqual(Screenwriter(chat_model=object())._system("BASE"), "BASE")  # unset -> unchanged

        sb = StoryboardArtist(chat_model=object(), extra_system_instruction="ZH")
        self.assertIn("ZH", sb._system("BASE"))

    def test_pipelines_thread_instruction_to_agents(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        from pipelines.idea2video_pipeline import Idea2VideoPipeline

        with tempfile.TemporaryDirectory() as root:
            p = Script2VideoPipeline(chat_model=object(), image_generator=object(), video_generator=object(),
                                     working_dir=root, chinese_instruction="ZH")
            self.assertEqual(p.storyboard_artist.extra_system_instruction, "ZH")
        with tempfile.TemporaryDirectory() as root:
            ip = Idea2VideoPipeline(chat_model=object(), image_generator=object(), video_generator=object(),
                                    working_dir=root, chinese_instruction="ZH")
            self.assertEqual(ip.screenwriter.extra_system_instruction, "ZH")


if __name__ == "__main__":
    unittest.main()
