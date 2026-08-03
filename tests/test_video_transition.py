"""Tests for optional shot/scene transitions in concatenate_video_files.

The moviepy render itself isn't exercised (heavy/slow); we verify the spec
normalization and that the right concatenation path/args are chosen, with stub
clips recording the effects applied.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import utils.video as uv
from utils.video import normalize_transition
from video import export_poster


class _StubClip:
    def __init__(self, name):
        self.name = name
        self.effects = None

    def with_effects(self, effects):
        self.effects = effects
        return self


class TestNormalize(unittest.TestCase):
    def test_none_and_disabled(self):
        self.assertIsNone(normalize_transition(None))
        self.assertIsNone(normalize_transition(""))
        self.assertIsNone(normalize_transition({"type": "none"}))
        self.assertIsNone(normalize_transition({"type": "crossfade", "duration": 0}))

    def test_string_form(self):
        self.assertEqual(normalize_transition("crossfade"), {"type": "crossfade", "duration": 0.5})

    def test_dict_form(self):
        self.assertEqual(normalize_transition({"type": "Fade", "duration": 1.0}),
                         {"type": "fade", "duration": 1.0})


class TestConcatPath(unittest.TestCase):
    def _run(self, spec):
        captured = {}

        def fake_concat(clips, **kw):
            captured["clips"], captured["kw"] = clips, kw
            return "COMPOSITE"

        orig = uv.concatenate_videoclips
        uv.concatenate_videoclips = fake_concat
        try:
            clips = [_StubClip("a"), _StubClip("b"), _StubClip("c")]
            result = uv._concat_with_transition(clips, spec)
            return clips, captured, result
        finally:
            uv.concatenate_videoclips = orig

    def test_crossfade_overlaps_and_skips_first(self):
        clips, captured, result = self._run({"type": "crossfade", "duration": 0.5})
        self.assertEqual(result, "COMPOSITE")
        self.assertEqual(captured["kw"].get("method"), "compose")
        self.assertEqual(captured["kw"].get("padding"), -0.5)
        self.assertIsNone(clips[0].effects)         # first clip not faded in
        self.assertIsNotNone(clips[1].effects)      # subsequent clips crossfade in

    def test_fade_applies_to_all(self):
        clips, captured, result = self._run({"type": "fade", "duration": 0.4})
        self.assertEqual(captured["kw"].get("method"), "compose")
        self.assertIsNotNone(clips[0].effects)      # fade applies to every clip
        self.assertEqual(len(clips[0].effects), 2)  # FadeIn + FadeOut

    def test_timeline_renders_ordered_subclips(self):
        captured = {"ranges": [], "closed": []}

        class Source:
            duration = 10.0

            def subclipped(self, start, end):
                captured["ranges"].append((start, end))
                return SimpleNamespace(close=lambda: captured["closed"].append((start, end)))

            def close(self):
                captured["source_closed"] = True

        class Final:
            def write_videofile(self, output, **kwargs):
                captured["output"] = output
                captured["kwargs"] = kwargs

            def close(self):
                captured["final_closed"] = True

        with mock.patch.object(uv, "VideoFileClip", return_value=Source()), \
             mock.patch.object(uv, "concatenate_videoclips", return_value=Final()):
            result = uv.render_timeline(
                "source.mp4",
                [{"start": 5, "end": 8}, {"start": 1, "end": 4}],
                "out.mp4",
            )

        self.assertEqual(result, "out.mp4")
        self.assertEqual(captured["ranges"], [(5.0, 8.0), (1.0, 4.0)])
        self.assertEqual(captured["kwargs"]["audio_codec"], "aac")
        self.assertTrue(captured["source_closed"])


class TestPoster(unittest.TestCase):
    def test_command_structure_and_success(self):
        captured = []

        def runner(cmd, **kw):
            captured.append(cmd)
            open(cmd[-1], "w").close()
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "poster.jpg")
            res = export_poster(os.path.join(d, "v.mp4"), out, at_seconds=1.5, runner=runner, ffmpeg="ffmpeg")
            self.assertEqual(res, out)
            cmd = captured[0]
            self.assertIn("-frames:v", cmd)
            self.assertIn("1.5", cmd)

    def test_failure_returns_none(self):
        res = export_poster("v.mp4", "o.jpg",
                            runner=lambda *a, **k: SimpleNamespace(returncode=1, stderr="x"), ffmpeg="ffmpeg")
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
