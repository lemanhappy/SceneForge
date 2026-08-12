"""Tests for FFmpeg-backed shot/scene transitions."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import utils.video as uv
from utils.video import normalize_transition
from video import export_poster


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
    def test_crossfade_blends_overlap_segments_in_order(self):
        filters, output = uv._transition_filter(
            ["[v0]", "[v1]", "[v2]"], [5.0, 4.0, 3.0],
            {"type": "crossfade", "duration": 0.5},
        )
        self.assertEqual(output, "[vout]")
        graph = ";".join(filters)
        self.assertIn("trim=start=4.500:end=5.000", graph)
        self.assertIn("blend=all_expr=", graph)
        self.assertIn("concat=n=5:v=1:a=0[vout]", graph)

    def test_fade_applies_to_every_input(self):
        filters, output = uv._transition_filter(
            ["[v0]", "[v1]"], [5.0, 4.0], {"type": "fade", "duration": 0.4},
        )
        self.assertEqual(output, "[vout]")
        self.assertIn("fade=t=in", filters[0])
        self.assertIn("fade=t=out", filters[1])
        self.assertIn("concat=n=2", filters[2])

    def test_timeline_renders_ordered_subclips(self):
        captured = []
        with mock.patch.object(uv, "_ffmpeg_or_raise", return_value="ffmpeg"), \
             mock.patch.object(uv, "probe_media_duration", return_value=10.0), \
             mock.patch.object(uv, "media_has_audio", return_value=True), \
             mock.patch.object(uv, "_run_ffmpeg", side_effect=lambda command: captured.append(command)):
            result = uv.render_timeline(
                "source.mp4",
                [{"start": 5, "end": 8}, {"start": 1, "end": 4}],
                "out.mp4",
            )

        self.assertEqual(result, "out.mp4")
        filter_graph = captured[0][captured[0].index("-filter_complex") + 1]
        self.assertIn("trim=start=5.000:end=8.000", filter_graph)
        self.assertIn("trim=start=1.000:end=4.000", filter_graph)
        self.assertIn("atrim=start=5.000:end=8.000", filter_graph)
        self.assertEqual(Path(captured[0][-1]).name, "out.mp4")

    def test_timeline_crossfades_audio_with_video(self):
        captured = []
        with mock.patch.object(uv, "_ffmpeg_or_raise", return_value="ffmpeg"), \
             mock.patch.object(uv, "probe_media_duration", return_value=10.0), \
             mock.patch.object(uv, "media_has_audio", return_value=True), \
             mock.patch.object(uv, "_run_ffmpeg", side_effect=lambda command: captured.append(command)):
            uv.render_timeline(
                "source.mp4",
                [{"start": 0, "end": 4}, {"start": 4, "end": 8}],
                "out.mp4",
                transition={"type": "crossfade", "duration": 0.5},
            )

        filter_graph = captured[0][captured[0].index("-filter_complex") + 1]
        self.assertIn("blend=all_expr=", filter_graph)
        self.assertIn("acrossfade=d=0.500[aout]", filter_graph)
        self.assertIn("fps=30,format=yuv420p", filter_graph)


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
