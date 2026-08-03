import unittest
from unittest.mock import AsyncMock

from tools.video_generator_doubao_seedance_yunwu_api import (
    VideoGeneratorDoubaoSeedanceYunwuAPI,
)
from tools.video_generator_omni_yunwu_api import VideoGeneratorOmniYunwuAPI


class VideoRemoteProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_seedance_reports_remote_task_id(self):
        generator = VideoGeneratorDoubaoSeedanceYunwuAPI(api_key="key")
        generator.create_video_generation_task = AsyncMock(return_value="seedance-task-1")
        generator.query_video_generation_task = AsyncMock(return_value="https://example.com/seedance.mp4")
        events = []

        result = await generator.generate_single_video(
            "prompt",
            [],
            camera_fixed=True,
            progress=lambda stage, message, meta: events.append((stage, message, meta)),
        )

        self.assertEqual(result.data, "https://example.com/seedance.mp4")
        self.assertEqual(events[0][0], "video_task_created")
        self.assertEqual(events[0][2]["task_id"], "seedance-task-1")
        self.assertEqual(events[0][2]["model"], generator.t2v_model)
        self.assertTrue(generator.create_video_generation_task.await_args.kwargs["camera_fixed"])

    async def test_omni_reports_remote_task_id(self):
        generator = VideoGeneratorOmniYunwuAPI(api_key="key")
        generator.create_video_generation_task = AsyncMock(return_value=("omni-task-1", "omni-flash"))
        generator.query_video_generation_task = AsyncMock(return_value="https://example.com/omni.mp4")
        events = []

        result = await generator.generate_single_video(
            "prompt",
            [],
            progress=lambda stage, message, meta: events.append((stage, message, meta)),
        )

        self.assertEqual(result.data, "https://example.com/omni.mp4")
        self.assertEqual(events[0][0], "video_task_created")
        self.assertEqual(events[0][2]["task_id"], "omni-task-1")
        self.assertEqual(events[0][2]["model"], "omni-flash")


if __name__ == "__main__":
    unittest.main()
