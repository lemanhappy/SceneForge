"""Tests for single-shot regeneration helpers on Script2VideoPipeline.

These cover the dependency-closure computation (the main correctness risk for
局部重生成: a missed edge silently leaves a downstream shot referencing a stale
frame) and the non-destructive archiving behaviour.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from interfaces import Camera
from interfaces.video_output import VideoOutput
from pipelines.script2video_pipeline import Script2VideoPipeline


def _camera(idx, active_shot_idxs, parent_shot_idx=None):
    return Camera(idx=idx, active_shot_idxs=active_shot_idxs, parent_shot_idx=parent_shot_idx)


class TestCollectDependentShots(unittest.TestCase):
    def setUp(self):
        # Camera 0: shots [0,1,2], root.
        # Camera 1: shots [3,4], child of shot 0 (an anchor).
        # Camera 2: shots [5],   child of shot 3 (an anchor).
        # Camera 3: shots [6],   child of shot 1 (a *sibling*, not an anchor).
        self.tree = [
            _camera(0, [0, 1, 2]),
            _camera(1, [3, 4], parent_shot_idx=0),
            _camera(2, [5], parent_shot_idx=3),
            _camera(3, [6], parent_shot_idx=1),
        ]

    def _closure(self, shot_idx):
        return Script2VideoPipeline._collect_dependent_shots(shot_idx, self.tree)

    def test_regenerating_root_anchor_cascades_everywhere(self):
        # shot 0 -> siblings 1,2 ; child anchor 3 -> sibling 4 + child anchor 5 ;
        # shot 1 is the parent of camera 3's anchor 6.
        self.assertEqual(self._closure(0), [0, 1, 2, 3, 4, 5, 6])

    def test_regenerating_plain_sibling_affects_only_itself(self):
        # shot 2 is a sibling that no camera depends on.
        self.assertEqual(self._closure(2), [2])

    def test_sibling_used_as_parent_shot_still_propagates(self):
        # shot 1 is a sibling of camera 0, but camera 3 points parent_shot_idx=1.
        self.assertEqual(self._closure(1), [1, 6])

    def test_regenerating_intermediate_anchor(self):
        # shot 3 anchors camera 1 -> sibling 4 + child anchor 5.
        self.assertEqual(self._closure(3), [3, 4, 5])

    def test_includes_self_when_isolated(self):
        self.assertEqual(self._closure(5), [5])


class TestArchiveShotDir(unittest.TestCase):
    def _make_shot_dir(self, root):
        shot_dir = os.path.join(root, "shots", "0")
        os.makedirs(shot_dir, exist_ok=True)
        for name in ("first_frame.png", "last_frame.png", "video.mp4", "shot_description.json"):
            with open(os.path.join(shot_dir, name), "w", encoding="utf-8") as f:
                f.write(name)
        return shot_dir

    def test_missing_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(
                Script2VideoPipeline._archive_shot_dir(os.path.join(root, "nope"))
            )

    def test_keep_description_leaves_plan_in_place(self):
        with tempfile.TemporaryDirectory() as root:
            shot_dir = self._make_shot_dir(root)
            archive = Script2VideoPipeline._archive_shot_dir(shot_dir, keep_description=True)

            self.assertEqual(archive, os.path.join(shot_dir, "_archive", "v1"))
            # plan stays, rendered artifacts move out
            self.assertTrue(os.path.exists(os.path.join(shot_dir, "shot_description.json")))
            self.assertFalse(os.path.exists(os.path.join(shot_dir, "first_frame.png")))
            self.assertTrue(os.path.exists(os.path.join(archive, "first_frame.png")))
            self.assertTrue(os.path.exists(os.path.join(archive, "video.mp4")))
            self.assertFalse(os.path.exists(os.path.join(archive, "shot_description.json")))

    def test_versions_increment_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            shot_dir = self._make_shot_dir(root)
            first = Script2VideoPipeline._archive_shot_dir(shot_dir, keep_description=False)
            # re-populate and archive again
            self._make_shot_dir(root)
            second = Script2VideoPipeline._archive_shot_dir(shot_dir, keep_description=False)

            self.assertTrue(first.endswith(os.path.join("_archive", "v1")))
            self.assertTrue(second.endswith(os.path.join("_archive", "v2")))
            self.assertTrue(os.path.exists(os.path.join(first, "video.mp4")))
            self.assertTrue(os.path.exists(os.path.join(second, "video.mp4")))

    def test_keep_description_false_moves_everything(self):
        with tempfile.TemporaryDirectory() as root:
            shot_dir = self._make_shot_dir(root)
            archive = Script2VideoPipeline._archive_shot_dir(shot_dir, keep_description=False)
            self.assertFalse(os.path.exists(os.path.join(shot_dir, "shot_description.json")))
            self.assertTrue(os.path.exists(os.path.join(archive, "shot_description.json")))


class TestRegenerateVideoClip(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_keyframe_and_archives_only_temporal_outputs(self):
        class Generator:
            def __init__(self):
                self.calls = []

            async def generate_single_video(self, **kwargs):
                self.calls.append(kwargs)
                return VideoOutput(fmt="bytes", ext="mp4", data=b"new-video")

        with tempfile.TemporaryDirectory() as root:
            scene = Path(root) / "scene_0"
            shot_dir = scene / "shots" / "0"
            shot_dir.mkdir(parents=True)
            Image.new("RGB", (32, 18), "blue").save(shot_dir / "first_frame.png")
            (shot_dir / "video.mp4").write_bytes(b"old-video")
            (shot_dir / "render_plan.json").write_text("{}", encoding="utf-8")
            (scene / "final_video.mp4").write_bytes(b"old-final")
            (scene / "quality.json").write_text("{}", encoding="utf-8")
            generator = Generator()
            pipeline = object.__new__(Script2VideoPipeline)
            pipeline.working_dir = str(scene)
            pipeline.video_generator = generator
            pipeline.render_retries = 1
            pipeline.transition = None
            pipeline.frame_events = {}
            pipeline._shot_corrections = {}
            shot = SimpleNamespace(
                idx=0,
                duration_sec=5,
                variation_type="medium",
                visual_desc="A fixed wide shot.",
                motion_desc="Static camera. One person enters from the left.",
                ff_vis_char_idxs=[0],
                beats=[],
                visual_style=[],
                avoid=[],
            )

            def concatenate(_paths, output, transition=None):
                Path(output).write_bytes(b"new-final")

            with patch(
                "pipelines.script2video_pipeline.concatenate_video_files",
                side_effect=concatenate,
            ):
                result = await pipeline.regenerate_video_clip(0, [shot])

            self.assertEqual(result, str(scene / "final_video.mp4"))
            self.assertTrue((shot_dir / "first_frame.png").is_file())
            self.assertEqual((shot_dir / "video.mp4").read_bytes(), b"new-video")
            self.assertEqual(
                (shot_dir / "_archive" / "clip_v1" / "video.mp4").read_bytes(),
                b"old-video",
            )
            self.assertTrue(
                (scene / "_archive" / "video_clip_rerenders" / "v1" / "quality.json").is_file()
            )
            self.assertEqual(len(generator.calls[0]["reference_image_paths"]), 1)
            self.assertTrue(generator.calls[0]["camera_fixed"])


if __name__ == "__main__":
    unittest.main()
