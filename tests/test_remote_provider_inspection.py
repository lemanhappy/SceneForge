import os
import unittest
from unittest.mock import AsyncMock, patch

from tools.remote_video import RemoteVideoProvider, RemoteVideoState
from tools.video_generator_doubao_seedance_yunwu_api import (
    VideoGeneratorDoubaoSeedanceYunwuAPI,
)
from tools.video_generator_omni_yunwu_api import VideoGeneratorOmniYunwuAPI
from tools.video_generator_openrouter_api import VideoGeneratorOpenRouterAPI
from tools.video_generator_veo_yunwu_api import VideoGeneratorVeoYunwuAPI


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, **kwargs):
        return self.payload


class _Session:
    def __init__(self, payload, status=200):
        self.response = _Response(payload, status)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class RemoteProviderInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_veo_inspects_completed_task(self):
        generator = VideoGeneratorVeoYunwuAPI(api_key="key", base_url="https://yunwu.ai")
        session = _Session({"status": "completed", "video_url": "https://example.com/veo.mp4"})
        with patch("tools.video_generator_veo_yunwu_api.aiohttp.ClientSession", return_value=session):
            result = await generator.inspect_remote_task("veo-1", model="veo3")
        self.assertIsInstance(generator, RemoteVideoProvider)
        self.assertEqual(result.state, RemoteVideoState.SUCCEEDED)
        self.assertEqual(result.output.data, "https://example.com/veo.mp4")

    async def test_seedance_inspects_completed_task(self):
        generator = VideoGeneratorDoubaoSeedanceYunwuAPI(api_key="key")
        session = _Session(
            {"status": "succeeded", "content": {"video_url": "https://example.com/seedance.mp4"}}
        )
        with patch(
            "tools.video_generator_doubao_seedance_yunwu_api.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await generator.inspect_remote_task("seedance-1")
        self.assertEqual(result.state, RemoteVideoState.SUCCEEDED)
        self.assertEqual(result.output.data, "https://example.com/seedance.mp4")

    async def test_omni_inspects_completed_task(self):
        generator = VideoGeneratorOmniYunwuAPI(api_key="key")
        session = _Session(
            {"status": "completed", "detail": {"upsample_video_url": "https://example.com/omni.mp4"}}
        )
        with patch("tools.video_generator_omni_yunwu_api.aiohttp.ClientSession", return_value=session):
            result = await generator.inspect_remote_task("omni-1", model="omni-flash")
        self.assertEqual(result.state, RemoteVideoState.SUCCEEDED)
        self.assertEqual(result.output.data, "https://example.com/omni.mp4")

    async def test_openrouter_uses_persisted_polling_url_and_downloads_result(self):
        generator = VideoGeneratorOpenRouterAPI(
            api_key="key",
            model="google/veo",
            base_url="https://openrouter.ai/api/v1",
        )
        get_json = AsyncMock(
            return_value=(200, {"status": "completed", "unsigned_urls": ["https://cdn.example/video.mp4"]})
        )
        get_bytes = AsyncMock(return_value=(200, b"openrouter-video"))
        with patch("tools.video_generator_openrouter_api._get_json", get_json), patch(
            "tools.video_generator_openrouter_api._get_bytes", get_bytes
        ):
            result = await generator.inspect_remote_task(
                "openrouter-1",
                metadata={"polling_url": "/api/v1/videos/openrouter-1/status"},
            )
        self.assertEqual(result.state, RemoteVideoState.SUCCEEDED)
        self.assertEqual(result.output.data, b"openrouter-video")
        self.assertEqual(get_json.await_args.args[0], "https://openrouter.ai/api/v1/videos/openrouter-1/status")

    async def test_nonterminal_status_remains_pending(self):
        generator = VideoGeneratorVeoYunwuAPI(api_key="key", base_url="https://yunwu.ai")
        session = _Session({"status": "processing"})
        with patch("tools.video_generator_veo_yunwu_api.aiohttp.ClientSession", return_value=session):
            result = await generator.inspect_remote_task("veo-2")
        self.assertEqual(result.state, RemoteVideoState.PENDING)

    async def test_veo_does_not_resubmit_when_remote_handle_callback_rejects_worker(self):
        generator = VideoGeneratorVeoYunwuAPI(api_key="key", base_url="https://yunwu.ai")
        session = _Session({"id": "paid-task-1"})

        def progress(stage, message, metadata):
            if stage == "video_task_created":
                raise RuntimeError("worker lease is no longer owned")

        with patch("tools.video_generator_veo_yunwu_api.aiohttp.ClientSession", return_value=session), patch.dict(
            os.environ,
            {"SCENEFORGE_VIDEO_CREATE_RETRIES": "3"},
        ):
            with self.assertRaisesRegex(RuntimeError, "worker lease"):
                await generator.generate_single_video(progress=progress)
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
