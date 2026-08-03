import logging
from typing import List, Literal, Mapping
import asyncio
import aiohttp
from interfaces.video_output import VideoOutput
from tools.video_capabilities import VideoCapabilities
from tools.remote_video import RemoteVideoInspection, RemoteVideoState
from utils.image import image_path_to_b64
from utils.retry import is_retryable_http_status, retry_after_seconds


def _emit_progress(progress, stage: str, message: str, metadata: dict | None = None) -> None:
    if progress is not None:
        progress(stage, message, metadata or {})


class VideoGeneratorDoubaoSeedanceYunwuAPI:
    video_capabilities = VideoCapabilities(
        provider="Doubao Seedance (Yunwu)",
        duration_parameter="duration",
        supported_durations=(5, 10),
        default_duration=5,
    )

    def __init__(
        self,
        api_key: str,
        t2v_model: str = "doubao-seedance-1-0-lite-t2v-250428",
        ff2v_model: str = "doubao-seedance-1-0-lite-i2v-250428",
        flf2v_model: str = "doubao-seedance-1-0-lite-i2v-250428",
        base_url: str = "https://yunwu.ai",
        max_create_attempts: int = 5,
        poll_interval: int = 2,
        max_poll_attempts: int = 300,
    ):
        self.api_key = api_key
        self.t2v_model = t2v_model
        self.ff2v_model = ff2v_model
        self.flf2v_model = flf2v_model
        root = str(base_url or "https://yunwu.ai").rstrip("/")
        if root.endswith("/contents/generations/tasks"):
            self.task_base_url = root
        else:
            if root.endswith("/v1"):
                root = root[:-3]
            self.task_base_url = f"{root}/volc/v1/contents/generations/tasks"
        self.max_create_attempts = max_create_attempts
        self.poll_interval = poll_interval
        self.max_poll_attempts = max_poll_attempts


    async def create_video_generation_task(
        self,
        prompt: str,
        reference_image_paths: List[str],
        resolution: Literal["480p", "720p", "1080p"] = "720p",
        aspect_ratio: str = "16:9",
        fps: Literal[16, 24] = 16,
        duration: Literal[5, 10] = 5,
        camera_fixed: bool = False,
        progress=None,
    ) -> str:
        """
        Create a video generation task and return the task ID.
        
        Args:
            prompt: Text prompt for video generation
            reference_image_paths: List of 1 or 2 reference images
            
        Returns:
            Task ID string
        """
        if len(reference_image_paths) == 0:
            model = self.t2v_model
        elif len(reference_image_paths) == 1:
            model = self.ff2v_model
        elif len(reference_image_paths) == 2:
            model = self.flf2v_model
        else:
            raise ValueError("reference_image_paths must contain 1 or 2 images.")

        logging.info(f"Calling {model} to generate video...")

        url = self.task_base_url


        content = [
            {
                "type": "text",
                "text": prompt + f" --rs {resolution} --rt {aspect_ratio} --dur {duration}  --fps {fps}  --wm false --seed -1 --cf {str(bool(camera_fixed)).lower()}"
            }
        ]
        if len(reference_image_paths) >= 1:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_path_to_b64(reference_image_paths[0])
                    },
                    "role": "first_frame",
                }
            )
        if len(reference_image_paths) >= 2:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_path_to_b64(reference_image_paths[1])
                    },
                    "role": "last_frame",
                }
            )

        payload = {
            "model": model,
            "content": content
        }

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        last_error = None
        for attempt in range(1, self.max_create_attempts + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload) as response:
                        response_json = await response.json()
                        http_status = response.status
                logging.debug(f"Response: {response_json}")
            except Exception as e:
                last_error = e
                logging.error(f"Error occurred while creating video generation task (attempt {attempt}/{self.max_create_attempts}): {e}")
                if attempt < self.max_create_attempts:
                    wait = retry_after_seconds(None, attempt)
                    _emit_progress(
                        progress,
                        "video_create_retry",
                        "Video provider request failed; retrying automatically",
                        {"attempt": attempt, "max_attempts": self.max_create_attempts, "wait_seconds": wait},
                    )
                    await asyncio.sleep(wait)
                continue

            if http_status >= 400:
                message = f"Video generation task creation failed with HTTP {http_status}: {response_json}"
                if not is_retryable_http_status(http_status):
                    raise RuntimeError(message)
                last_error = RuntimeError(message)
                logging.warning(f"{message} (attempt {attempt}/{self.max_create_attempts})")
                if attempt < self.max_create_attempts:
                    wait = retry_after_seconds(getattr(response, "headers", None), attempt)
                    _emit_progress(
                        progress,
                        "video_create_retry",
                        "Video provider is busy; retrying automatically",
                        {"attempt": attempt, "max_attempts": self.max_create_attempts, "wait_seconds": wait},
                    )
                    await asyncio.sleep(wait)
                continue

            task_id = response_json.get("id")
            if not task_id:
                raise RuntimeError(f"Video generation task creation returned no task id: {response_json}")
            logging.info(f"Video generation task created successfully. Task ID: {task_id}")
            return task_id

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Failed to create video generation task after {self.max_create_attempts} attempts.")

    async def inspect_remote_task(
        self,
        remote_task_id: str,
        *,
        model: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> RemoteVideoInspection:
        url = f"{self.task_base_url}/{remote_task_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                payload = await response.json()
                if response.status >= 400:
                    raise RuntimeError(f"Querying video generation task failed with HTTP {response.status}: {payload}")
        status = str(payload.get("status") or "unknown").lower()
        if status == "succeeded":
            video_url = (payload.get("content") or {}).get("video_url")
            if not video_url:
                return RemoteVideoInspection(RemoteVideoState.FAILED, status, error="completed task has no video_url")
            return RemoteVideoInspection(
                RemoteVideoState.SUCCEEDED,
                status,
                output=VideoOutput(fmt="url", ext="mp4", data=video_url),
            )
        if status == "failed":
            return RemoteVideoInspection(RemoteVideoState.FAILED, status, error=str(payload))
        return RemoteVideoInspection(RemoteVideoState.PENDING, status)

    async def query_video_generation_task(
        self,
        task_id: str,
    ) -> str:
        """
        Query the video generation task until completion and return the video URL.
        
        Args:
            task_id: Task ID to query
            
        Returns:
            Video URL string
        """
        url = f"{self.task_base_url}/{task_id}"
        headers = {
            'Authorization': f'Bearer {self.api_key}',
        }

        attempts = 0
        consecutive_errors = 0
        while True:
            if attempts >= self.max_poll_attempts:
                raise TimeoutError(f"Video generation did not complete after {attempts} polls.")
            attempts += 1

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        response_json = await response.json()
                        http_status = response.status
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise RuntimeError(f"Querying video generation task failed {consecutive_errors} times in a row.") from e
                logging.error(f"Error occurred while querying video generation task: {e}. Retrying in {self.poll_interval} seconds...")
                await asyncio.sleep(self.poll_interval)
                continue
            consecutive_errors = 0

            if http_status >= 400:
                raise RuntimeError(f"Querying video generation task failed with HTTP {http_status}: {response_json}")

            status = response_json.get("status")
            if status == "succeeded":
                video_url = response_json["content"]["video_url"]
                logging.info(f"Video generation completed successfully. Video URL: {video_url}")
                return video_url
            elif status == "failed":
                logging.error(f"Video generation failed. Response: {response_json}")
                raise ValueError("Video generation failed.")
            else:
                logging.info(f"Video generation is still in progress. Checking again in {self.poll_interval} seconds...")
                await asyncio.sleep(self.poll_interval)

    async def generate_single_video(
        self,
        prompt: str,
        reference_image_paths: List[str],
        resolution: Literal["480p", "720p", "1080p"] = "720p",
        aspect_ratio: str = "16:9",
        fps: Literal[16, 24] = 16,
        duration: Literal[5, 10] = 5,
        camera_fixed: bool = False,
        **kwargs,
    ) -> VideoOutput:
        """
        Generate a single video by creating a task and waiting for completion.
        
        Args:
            prompt: Text prompt for video generation
            reference_image_paths: List of 1 or 2 reference images
            resolution: Resolution of the video
            aspect_ratio: Aspect ratio of the video
            fps: Frames per second of the video
            duration: Duration of the video
        Returns:
            VideoOutput containing the video URL
        """
        progress = kwargs.get("progress")
        task_id = await self.create_video_generation_task(
            prompt,
            reference_image_paths,
            resolution,
            aspect_ratio,
            fps,
            duration,
            camera_fixed=camera_fixed,
            progress=progress,
        )
        if not reference_image_paths:
            model = self.t2v_model
        elif len(reference_image_paths) == 1:
            model = self.ff2v_model
        else:
            model = self.flf2v_model
        _emit_progress(
            progress,
            "video_task_created",
            "Seedance video generation task created",
            {"provider": "seedance_yunwu", "model": model, "base_url": self.task_base_url, "task_id": task_id},
        )
        video_url = await self.query_video_generation_task(task_id)
        _emit_progress(
            progress,
            "video_completed",
            "Seedance video generation completed",
            {"model": model, "task_id": task_id},
        )
        return VideoOutput(fmt="url", ext="mp4", data=video_url)

