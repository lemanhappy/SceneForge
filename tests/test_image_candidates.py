import json
import tempfile
import unittest
from pathlib import Path

from pipelines.script2video_pipeline import Script2VideoPipeline


class _ImageOutput:
    def __init__(self, payload: bytes):
        self.payload = payload

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.payload)


class _ImageGenerator:
    def __init__(self):
        self.calls = 0

    async def generate_single_image(self, **_kwargs):
        self.calls += 1
        return _ImageOutput(f"candidate-{self.calls}".encode())


class _PickLast:
    async def __call__(self, _references, _description, candidates):
        return candidates[-1]


class _FailSelection:
    async def __call__(self, _references, _description, _candidates):
        raise RuntimeError("selector unavailable")


class TestImageCandidates(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self, working_dir: str, count: int):
        generator = _ImageGenerator()
        pipeline = Script2VideoPipeline(
            chat_model=object(),
            image_generator=generator,
            video_generator=object(),
            working_dir=working_dir,
            image_candidate_count=count,
            render_retries=1,
        )
        return pipeline, generator

    async def test_multiple_candidates_select_and_record_best_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, generator = self._pipeline(tmp, 3)
            pipeline.best_image_selector = _PickLast()
            output = Path(tmp) / "shots" / "0" / "first_frame.png"

            await pipeline._generate_best_frame(
                shot_idx=0,
                frame_type="first_frame",
                output_path=str(output),
                prompt="prompt",
                reference_image_paths=[],
                reference_image_path_and_text_pairs=[],
                target_description="target",
            )

            self.assertEqual(generator.calls, 3)
            self.assertEqual(output.read_bytes(), b"candidate-3")
            selection_path = (
                Path(tmp) / "shots" / "0" / "frame_candidates"
                / "first_frame" / "selection.json"
            )
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            self.assertEqual(selection["selected_candidate"], 3)
            self.assertEqual(selection["selection_method"], "vision_model")
            self.assertEqual(selection["successful_count"], 3)

    async def test_selector_failure_keeps_first_successful_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, generator = self._pipeline(tmp, 2)
            pipeline.best_image_selector = _FailSelection()
            output = Path(tmp) / "shots" / "1" / "last_frame.png"

            await pipeline._generate_best_frame(
                shot_idx=1,
                frame_type="last_frame",
                output_path=str(output),
                prompt="prompt",
                reference_image_paths=[],
                reference_image_path_and_text_pairs=[],
                target_description="target",
            )

            self.assertEqual(generator.calls, 2)
            self.assertEqual(output.read_bytes(), b"candidate-1")
            selection_path = output.parent / "frame_candidates" / "last_frame" / "selection.json"
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            self.assertEqual(selection["selected_candidate"], 1)
            self.assertEqual(selection["selection_method"], "fallback_first")

    async def test_single_candidate_preserves_direct_generation_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, generator = self._pipeline(tmp, 1)
            pipeline.best_image_selector = _FailSelection()
            output = Path(tmp) / "shots" / "2" / "first_frame.png"
            stale = output.parent / "frame_candidates" / "first_frame"
            stale.mkdir(parents=True)
            (stale / "candidate_3.png").write_bytes(b"stale")

            await pipeline._generate_best_frame(
                shot_idx=2,
                frame_type="first_frame",
                output_path=str(output),
                prompt="prompt",
                reference_image_paths=[],
                reference_image_path_and_text_pairs=[],
                target_description="target",
            )

            self.assertEqual(generator.calls, 1)
            self.assertEqual(output.read_bytes(), b"candidate-1")
            self.assertFalse((output.parent / "frame_candidates").exists())


if __name__ == "__main__":
    unittest.main()
