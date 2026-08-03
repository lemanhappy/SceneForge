"""Video duration capability and render-plan regression tests."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from interfaces.video_output import VideoOutput
from pipelines.script2video_pipeline import (
    Script2VideoPipeline,
    _camera_is_locked,
    _compile_reference_aware_video_prompt,
    _quality_target_description,
    _requires_last_frame,
)
from server.artifacts_reader import build_manifest
from tools.video_capabilities import (
    VideoCapabilities,
    plan_video_duration,
    storyboard_duration_instruction,
)


class _Generator:
    def __init__(self, capabilities):
        self.video_capabilities = capabilities
        self.calls = []

    async def generate_single_video(self, **kwargs):
        self.calls.append(kwargs)
        return VideoOutput(fmt="bytes", ext="mp4", data=b"video")


class _ConcurrentGenerator(_Generator):
    def __init__(self, capabilities):
        super().__init__(capabilities)
        self.active = 0
        self.max_active = 0

    async def generate_single_video(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            return VideoOutput(fmt="bytes", ext="mp4", data=b"video")
        finally:
            self.active -= 1


class TestVideoDurationPlanning(unittest.TestCase):
    def test_visible_actor_entry_is_rewritten_before_video_generation(self):
        shot = SimpleNamespace(
            duration_sec=5,
            visual_desc="One person enters a waiting hall.",
            motion_desc=(
                "Static camera. The character enters through the left door. "
                "They stop and look at the lunchbox."
            ),
            ff_vis_char_idxs=[0],
            beats=[{
                "start_sec": 0,
                "end_sec": 1,
                "action": "The character enters from the left.",
                "camera": "Fixed shot.",
            }],
            visual_style=[],
            avoid=[],
        )

        prompt, rewritten = _compile_reference_aware_video_prompt(shot)

        self.assertTrue(rewritten)
        self.assertNotIn("enters through", prompt)
        self.assertNotIn("enters from", prompt)
        self.assertIn("already contains exactly 1 visible", prompt)
        self.assertIn("They stop and look at the lunchbox", prompt)
        self.assertIn("Character count stays constant", prompt)
        quality_description = _quality_target_description(shot)
        self.assertEqual(quality_description, prompt)
        self.assertNotIn("enters through", quality_description)

    def test_video_quality_uses_motion_contract_not_first_frame_inventory(self):
        shot = SimpleNamespace(
            duration_sec=5,
            ff_desc="A clock is visible at the far left of the first frame.",
            visual_desc="The same waiting hall.",
            motion_desc="The camera tracks the man as he places a lunchbox on the counter.",
            ff_vis_char_idxs=[0],
            beats=[],
            visual_style=[],
            avoid=[],
        )

        description = _quality_target_description(shot)

        self.assertIn("camera tracks the man", description)
        self.assertNotIn("clock is visible", description)

    def test_already_held_prop_does_not_get_picked_up_twice(self):
        shot = SimpleNamespace(
            duration_sec=5,
            ff_desc="A man is holding the blue lunchbox with both hands near the bench.",
            visual_desc="A man carries a blue lunchbox.",
            motion_desc=(
                "The man picks up the blue lunchbox from the bench, straightens, and walks "
                "toward the ticket window."
            ),
            ff_vis_char_idxs=[0],
            beats=[{
                "start_sec": 0,
                "end_sec": 2,
                "action": "The man picks up the lunchbox from the bench and walks right.",
            }],
            visual_style=[],
            avoid=[],
        )

        prompt, rewritten = _compile_reference_aware_video_prompt(shot)

        self.assertTrue(rewritten)
        self.assertNotIn("picks up", prompt)
        self.assertIn("continues holding", prompt)
        self.assertIn("do not repeat an already-completed pickup", prompt)

    def test_reference_strategy_avoids_independent_endpoint_for_locked_or_medium_shots(self):
        locked_large = SimpleNamespace(
            variation_type="large",
            motion_desc="Static camera throughout the shot.",
            beats=[],
        )
        moving_large = SimpleNamespace(
            variation_type="large",
            motion_desc="The camera performs a slow dolly push-in.",
            beats=[],
        )
        moving_medium = SimpleNamespace(
            variation_type="medium",
            motion_desc="The character walks while the camera tracks.",
            beats=[],
        )

        self.assertTrue(_camera_is_locked(locked_large))
        self.assertFalse(_requires_last_frame(locked_large))
        self.assertTrue(_requires_last_frame(moving_large))
        self.assertFalse(_requires_last_frame(moving_medium))

    def test_discrete_backend_selects_nearest_and_prefers_longer_on_tie(self):
        generator = _Generator(VideoCapabilities(
            provider="seedance",
            duration_parameter="duration",
            supported_durations=(5, 10),
            default_duration=5,
        ))

        self.assertEqual(plan_video_duration(generator, 6).requested_duration_sec, 5)
        tied = plan_video_duration(generator, 7.5)
        self.assertEqual(tied.requested_duration_sec, 10)
        self.assertEqual(tied.generation_kwargs(), {"duration": 10})
        self.assertEqual(tied.reason, "nearest_supported")

    def test_ranged_backend_rounds_clamps_and_reports_exactness(self):
        generator = _Generator(VideoCapabilities(
            provider="range",
            duration_parameter="seconds",
            min_duration=2,
            max_duration=8,
            duration_step=2,
        ))

        self.assertEqual(plan_video_duration(generator, 5).requested_duration_sec, 6)
        self.assertEqual(plan_video_duration(generator, 15).requested_duration_sec, 8)
        self.assertTrue(plan_video_duration(generator, 4).exact)

    def test_fixed_backend_does_not_send_an_unsupported_keyword(self):
        generator = _Generator(VideoCapabilities(provider="fixed", default_duration=8))

        plan = plan_video_duration(generator, 5)

        self.assertEqual(plan.requested_duration_sec, 8)
        self.assertEqual(plan.reason, "backend_fixed")
        self.assertEqual(plan.generation_kwargs(), {})
        self.assertIn("fixed duration of 8 seconds", storyboard_duration_instruction(generator))

    def test_unknown_backend_preserves_compatibility(self):
        plan = plan_video_duration(object(), 5)

        self.assertIsNone(plan.requested_duration_sec)
        self.assertEqual(plan.reason, "backend_unspecified")
        self.assertEqual(plan.generation_kwargs(), {})


class TestPipelineDurationIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_high_quality_mode_generates_and_selects_two_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            scene_dir = Path(root) / "idea2video" / "scene_0"
            generator = _Generator(VideoCapabilities(
                provider="seedance",
                duration_parameter="duration",
                supported_durations=(5, 10),
                default_duration=5,
            ))
            pipeline = object.__new__(Script2VideoPipeline)
            pipeline.working_dir = str(scene_dir)
            pipeline.video_generator = generator
            pipeline.render_retries = 1
            pipeline.video_aspect_ratio = "9:16"
            pipeline.video_candidate_count = 2
            pipeline.global_reference_images = []
            pipeline._active_characters = []
            pipeline.max_concurrent_video_generations = 1
            ready = asyncio.Event()
            ready.set()
            pipeline.frame_events = {0: {"first_frame": ready}}
            shot_dir = scene_dir / "shots" / "0"
            shot_dir.mkdir(parents=True)
            (shot_dir / "first_frame.png").write_bytes(b"first")
            shot = SimpleNamespace(
                idx=0,
                duration_sec=5,
                variation_type="small",
                visual_desc="A quiet waiting room.",
                motion_desc="Static camera. One man breathes slowly.",
                ff_desc="One man is visible.",
                ff_vis_char_idxs=[0],
                lf_vis_char_idxs=[0],
                beats=[],
                visual_style=[],
                avoid=[],
            )

            with patch(
                "quality.score_video_candidate",
                side_effect=lambda video_path, *_args, **_kwargs: {
                    "path": video_path,
                    "score": 0.8,
                    "consistent": True,
                },
            ):
                await pipeline.generate_video_for_single_shot(shot)

            assert len(generator.calls) == 2
            assert (shot_dir / "candidates" / "candidate_1.mp4").exists()
            assert (shot_dir / "candidates" / "candidate_2.mp4").exists()
            assert (shot_dir / "video.mp4").exists()
            selection = json.loads(
                (shot_dir / "candidates" / "selection.json").read_text(encoding="utf-8")
            )
            assert selection["candidate_count"] == 2
            assert selection["selected_candidate"] == 1
            plan = json.loads((shot_dir / "render_plan.json").read_text(encoding="utf-8"))
            assert plan["video_candidate_count"] == 2
            assert plan["selected_candidate"] == 1

    async def test_locked_camera_uses_one_anchor_and_strict_stability_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            scene_dir = Path(root) / "scene_0"
            generator = _Generator(VideoCapabilities(
                provider="seedance",
                duration_parameter="duration",
                supported_durations=(5, 10),
                default_duration=5,
            ))
            pipeline = object.__new__(Script2VideoPipeline)
            pipeline.working_dir = str(scene_dir)
            pipeline.video_generator = generator
            pipeline.render_retries = 1
            ready = asyncio.Event()
            ready.set()
            pipeline.frame_events = {0: {"first_frame": ready}}
            shot_dir = scene_dir / "shots" / "0"
            shot_dir.mkdir(parents=True)
            (shot_dir / "first_frame.png").write_bytes(b"first")
            (shot_dir / "last_frame.png").write_bytes(b"independent endpoint")
            shot = SimpleNamespace(
                idx=0,
                duration_sec=5,
                variation_type="medium",
                visual_desc="One person enters a waiting hall.",
                motion_desc="Static camera throughout. The person enters from the left.",
                ff_vis_char_idxs=[0],
                beats=[],
                visual_style=[],
                avoid=[],
            )

            await pipeline.generate_video_for_single_shot(shot)

            call = generator.calls[0]
            self.assertEqual(call["reference_image_paths"], [str(shot_dir / "first_frame.png")])
            self.assertTrue(call["camera_fixed"])
            self.assertIn("Never clone, duplicate", call["prompt"])
            self.assertIn("must remain rigid, stationary", call["prompt"])
            self.assertNotIn("enters from the left", call["prompt"])
            self.assertIn("already contains exactly 1 visible", call["prompt"])
            plan = json.loads((shot_dir / "render_plan.json").read_text(encoding="utf-8"))
            self.assertTrue(plan["camera_locked"])
            self.assertFalse(plan["use_last_frame"])
            self.assertEqual(plan["reference_frame_count"], 1)
            self.assertTrue(plan["reference_entry_conflict_rewritten"])

    async def test_pipeline_passes_selected_duration_and_exposes_render_plan(self):
        with tempfile.TemporaryDirectory() as root:
            scene_dir = Path(root) / "idea2video" / "scene_0"
            generator = _Generator(VideoCapabilities(
                provider="seedance",
                duration_parameter="duration",
                supported_durations=(5, 10),
                default_duration=5,
            ))
            pipeline = object.__new__(Script2VideoPipeline)
            pipeline.working_dir = str(scene_dir)
            pipeline.video_generator = generator
            pipeline.render_retries = 1
            pipeline.video_aspect_ratio = "9:16"
            first_frame_ready = asyncio.Event()
            first_frame_ready.set()
            pipeline.frame_events = {0: {"first_frame": first_frame_ready}}
            events = []
            shot = SimpleNamespace(
                idx=0,
                duration_sec=8,
                variation_type="small",
                visual_desc="A restrained close-up.",
                motion_desc="A slow push-in as one tear falls.",
                beats=[],
                visual_style=[],
                avoid=[],
            )

            await pipeline.generate_video_for_single_shot(
                shot,
                progress=lambda stage, message, metadata: events.append((stage, metadata)),
            )

            self.assertEqual(generator.calls[0]["duration"], 10)
            self.assertEqual(generator.calls[0]["aspect_ratio"], "9:16")
            plan_path = scene_dir / "shots" / "0" / "render_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["planned_duration_sec"], 8)
            self.assertEqual(plan["requested_duration_sec"], 10)
            self.assertFalse(plan["exact"])
            self.assertEqual(plan["status"], "completed")
            start = next(metadata for stage, metadata in events if stage == "video_clip_start")
            self.assertEqual(start["requested_duration_sec"], 10)

            manifest = build_manifest(Path(root))
            self.assertEqual(
                manifest["scenes"][0]["shots"][0]["render_plan"]["requested_duration_sec"],
                10,
            )

    async def test_video_generation_concurrency_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            generator = _ConcurrentGenerator(VideoCapabilities(
                provider="seedance",
                duration_parameter="duration",
                supported_durations=(5, 10),
                default_duration=5,
            ))
            pipeline = object.__new__(Script2VideoPipeline)
            pipeline.working_dir = str(Path(root) / "scene_0")
            pipeline.video_generator = generator
            pipeline.render_retries = 1
            pipeline.max_concurrent_video_generations = 2
            pipeline._video_generation_semaphore = asyncio.Semaphore(2)
            pipeline.frame_events = {}
            shots = []
            for index in range(4):
                ready = asyncio.Event()
                ready.set()
                pipeline.frame_events[index] = {"first_frame": ready}
                shots.append(SimpleNamespace(
                    idx=index,
                    duration_sec=5,
                    variation_type="small",
                    visual_desc=f"Shot {index}",
                    motion_desc="A slow push-in.",
                    beats=[],
                    visual_style=[],
                    avoid=[],
                ))

            await asyncio.gather(*(
                pipeline.generate_video_for_single_shot(shot) for shot in shots
            ))

            self.assertEqual(len(generator.calls), 4)
            self.assertEqual(generator.max_active, 2)


if __name__ == "__main__":
    unittest.main()
