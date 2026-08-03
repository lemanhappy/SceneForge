import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from interfaces import Camera, CharacterInScene, ShotBriefDescription, ShotDescription
from agent_runtime.session_index import SessionIndex
from agent_runtime.sceneforge_adapters import SceneForgeAdapters
from agent_runtime.tools import ToolRuntimeContext
from pipelines.idea2video_pipeline import Idea2VideoPipeline
from pipelines.script2video_pipeline import Script2VideoPipeline


class FakeIdeaPipeline:
    def __init__(self, chat_model, image_generator, video_generator, working_dir, **kwargs):
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.kwargs = kwargs

    async def develop_story(self, idea, user_requirement, quiet=False):
        path = self.working_dir / "story.txt"
        path.write_text("story", encoding="utf-8")
        return "story"

    async def extract_characters(self, story, quiet=False):
        chars = [CharacterInScene(idx=0, identifier_in_scene="Cat", is_visible=True, static_features="black cat", dynamic_features="helmet")]
        (self.working_dir / "characters.json").write_text(json.dumps([c.model_dump() for c in chars]), encoding="utf-8")
        return chars

    async def write_script_based_on_story(self, story, user_requirement, quiet=False):
        script = [{"scene": "cat jumps"}]
        (self.working_dir / "script.json").write_text(json.dumps(script), encoding="utf-8")
        return script




class HangingIdeaPipeline(FakeIdeaPipeline):
    async def develop_story(self, idea, user_requirement, quiet=False):
        await asyncio.sleep(10)
        return "story"



class FakeRevisionModel:
    async def ainvoke(self, prompt):
        return SimpleNamespace(content='[{"idx": 0, "description": "more oppressive"}]')


class FailRenderIdeaPipeline(FakeIdeaPipeline):
    async def __call__(self, idea, user_requirement, style, quiet=False, hook_text=""):
        raise RuntimeError("render failed")


class NoisyRenderIdeaPipeline(FakeIdeaPipeline):
    async def __call__(self, idea, user_requirement, style, quiet=False, hook_text=""):
        print("NOISE_FROM_RENDER_PIPELINE")
        final = self.working_dir / "final_video.mp4"
        final.write_text("video", encoding="utf-8")
        return str(final)


class FakeScriptPipeline:
    def __init__(self, chat_model, image_generator, video_generator, working_dir, **kwargs):
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.kwargs = kwargs

    async def plan_text_artifacts(self, script, user_requirement, style, characters=None, progress=None, quiet=False):
        if progress:
            progress("design_storyboard", "Designing storyboard", {})
            progress("decompose_shots", "Decomposing shot visual descriptions", {"shot_count": 1})
            progress("construct_camera_tree", "Constructing camera tree", {"shot_count": 1})
        (self.working_dir / "storyboard.json").write_text("[]", encoding="utf-8")
        (self.working_dir / "camera_tree.json").write_text("[]", encoding="utf-8")
        shot_dir = self.working_dir / "shots" / "0"
        shot_dir.mkdir(parents=True, exist_ok=True)
        (shot_dir / "shot_description.json").write_text("{}", encoding="utf-8")
        if characters:
            (self.working_dir / "characters.json").write_text(json.dumps([c.model_dump() for c in characters]), encoding="utf-8")
        return {}




class FailingScriptPipeline(FakeScriptPipeline):
    async def plan_text_artifacts(self, script, user_requirement, style, characters=None, progress=None, quiet=False):
        if progress:
            progress("design_storyboard", "Designing storyboard", {})
        raise RuntimeError("storyboard failed")


class FakeInitChatModel:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class Script2VideoPlanningProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_text_artifacts_emits_progress_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Script2VideoPipeline(chat_model=object(), image_generator=object(), video_generator=object(), working_dir=tmp)
            chars = [CharacterInScene(idx=0, identifier_in_scene="Cat", is_visible=True, static_features="black cat", dynamic_features="helmet")]
            storyboard = [ShotBriefDescription(idx=0, is_last=True, cam_idx=0, visual_desc="cat jumps", audio_desc="wind")]
            shot = ShotDescription(idx=0, is_last=True, cam_idx=0, visual_desc="cat jumps", variation_type="small", variation_reason="simple motion", ff_desc="cat starts", ff_vis_char_idxs=[0], lf_desc="cat lands", lf_vis_char_idxs=[0], motion_desc="cat jumps", audio_desc="wind")
            camera = [Camera(idx=0, active_shot_idxs=[0])]

            async def design_storyboard(script, characters, user_requirement, quiet=False):
                return storyboard

            async def decompose_visual_descriptions(shot_brief_descriptions, characters, quiet=False):
                return [shot]

            async def construct_camera_tree(shot_descriptions, quiet=False):
                return camera

            pipeline.design_storyboard = design_storyboard
            pipeline.decompose_visual_descriptions = decompose_visual_descriptions
            pipeline.construct_camera_tree = construct_camera_tree
            events = []
            await pipeline.plan_text_artifacts("script", "req", "style", characters=chars, progress=lambda stage, message, metadata=None: events.append(stage))
            self.assertEqual(events, [
                "extract_characters",
                "design_storyboard",
                "decompose_shots",
                "prompt_preflight",
                "construct_camera_tree",
            ])


    async def test_idea_pipeline_quiet_suppresses_text_planning_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Idea2VideoPipeline(chat_model=object(), image_generator=object(), video_generator=object(), working_dir=tmp)

            async def develop_story(idea, user_requirement):
                return "story"

            pipeline.screenwriter = SimpleNamespace(develop_story=develop_story)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = await pipeline.develop_story("idea", "req", quiet=True)
            self.assertEqual(result, "story")
            self.assertEqual(stdout.getvalue(), "")


class SceneForgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_build_chat_model_uses_bounded_init_chat_model_kwargs(self):
        fake = FakeInitChatModel()
        with patch.dict("os.environ", {
            "SCENEFORGE_LLM_API_KEY": "test-key",
            "SCENEFORGE_LLM_MODEL": "test-model",
            "SCENEFORGE_LLM_BASE_URL": "https://example.invalid/v1",
            "SCENEFORGE_LLM_REQUEST_TIMEOUT_SECONDS": "12",
            "SCENEFORGE_NARRATIVE_MAX_TOKENS": "1234",
        }), patch("agent_runtime.sceneforge_adapters.init_chat_model", fake):
            from agent_runtime.sceneforge_adapters import _build_chat_model

            _build_chat_model()

        self.assertEqual(fake.calls[0]["model"], "test-model")
        self.assertEqual(fake.calls[0]["base_url"], "https://example.invalid/v1")
        self.assertEqual(fake.calls[0]["timeout"], 12.0)
        self.assertEqual(fake.calls[0]["max_retries"], 0)
        self.assertEqual(fake.calls[0]["max_completion_tokens"], 1234)


    async def test_narrative_planning_uses_text_only_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                result = await adapter.sceneforge_narrative_planning({"idea": "moon cat", "user_requirement": "short", "style": "anime"})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertTrue(payload["ready_for_render"])
            root = Path(tmp) / payload["working_dir"]
            self.assertTrue((root / "idea2video" / "scene_0" / "storyboard.json").exists())
            self.assertTrue((root / "idea2video" / "scene_0" / "camera_tree.json").exists())
            self.assertTrue((root / "idea2video" / "scene_0" / "shots" / "0" / "shot_description.json").exists())
            self.assertFalse((root / "script2video" / "storyboard.json").exists())
            self.assertFalse((root / "script2video" / "final_video.mp4").exists())


    async def test_narrative_planning_forwards_pipeline_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            events = []
            runtime = ToolRuntimeContext("sceneforge_narrative_planning", "sceneforge_narrative_planning", turn_id="turn-test", progress_callback=events.append)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                result = await adapter.sceneforge_narrative_planning({"idea": "moon cat"}, runtime)
            self.assertTrue(result.ok)
            stages = [event["progress"]["stage"] for event in events if event.get("type") == "tool_progress"]
            self.assertIn("initializing_llm", stages)
            self.assertIn("develop_story", stages)
            self.assertIn("design_storyboard", stages)
            self.assertIn("decompose_shots", stages)
            self.assertIn("construct_camera_tree", stages)


    async def test_plan_scene_failure_marks_session_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FailingScriptPipeline):
                with self.assertRaises(RuntimeError):
                    await adapter.sceneforge_narrative_planning({"idea": "moon cat"})
            session = index.active()
            self.assertEqual(session["stage"], "error")
            self.assertIn("storyboard failed", session["summary"])


    async def test_narrative_planning_timeout_marks_session_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch.dict("os.environ", {"SCENEFORGE_NARRATIVE_STEP_TIMEOUT_SECONDS": "0.01"}), \
                 patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", HangingIdeaPipeline):
                with self.assertRaises(RuntimeError):
                    await adapter.sceneforge_narrative_planning({"idea": "moon cat"})
            session = index.active()
            self.assertIsNotNone(session)
            self.assertEqual(session["stage"], "error")
            self.assertIn("timed out", session["summary"])



    async def test_active_session_without_new_input_continues_existing_idea(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="moon cat", user_requirement="short", style="anime")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()),                  patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline),                  patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                result = await adapter.sceneforge_narrative_planning({})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertEqual(payload["session_id"], record["session_id"])
            self.assertEqual(index.active()["session_id"], record["session_id"])


    async def test_active_session_continuation_preserves_existing_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="moon cat", user_requirement="short", style="anime")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()),                  patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline),                  patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                result = await adapter.sceneforge_narrative_planning({"session_id": record["session_id"]})
            self.assertTrue(result.ok)
            self.assertEqual(index.get(record["session_id"])["style"], "anime")

    async def test_new_idea_creates_new_session_instead_of_reusing_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                first = await adapter.sceneforge_narrative_planning({"idea": "moon cat"})
                second = await adapter.sceneforge_narrative_planning({"idea": "ocean robot"})
            self.assertNotEqual(json.loads(first.content)["session_id"], json.loads(second.content)["session_id"])


    async def test_explicit_session_with_different_idea_creates_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            old = index.create(idea="old cat")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FakeIdeaPipeline), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeScriptPipeline):
                result = await adapter.sceneforge_narrative_planning({"session_id": old["session_id"], "idea": "new robot"})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertNotEqual(payload["session_id"], old["session_id"])
            self.assertEqual(index.get(payload["session_id"])["idea"], "new robot")

    async def test_revision_mode_rewrites_existing_artifact_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            target = Path(tmp) / record["working_dir"] / "idea2video" / "scene_0" / "storyboard.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('[{"idx": 0, "description": "calm"}]', encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=FakeRevisionModel()):
                result = await adapter.sceneforge_narrative_planning({"revision_target": "idea2video/scene_0/storyboard.json", "revision_instruction": "make it oppressive"})
            self.assertTrue(result.ok)
            self.assertIn("more oppressive", target.read_text(encoding="utf-8"))
            self.assertTrue((Path(tmp) / ".sceneforge" / "logs" / "revisions.jsonl").exists())
            self.assertTrue(index.get(record["session_id"])["stale"]["final_video"])


    async def test_revision_missing_instruction_marks_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            target = Path(tmp) / record["working_dir"] / "idea2video" / "scene_0" / "storyboard.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('[]', encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_narrative_planning({"revision_target": "idea2video/scene_0/storyboard.json"})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "missing_revision_instruction")
            self.assertEqual(index.get(record["session_id"])["stage"], "error")


    async def test_revision_missing_target_marks_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_narrative_planning({"revision_target": "idea2video/scene_0/missing.json", "revision_instruction": "change it"})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "dependency_missing")
            self.assertEqual(index.get(record["session_id"])["stage"], "error")

    async def test_render_setup_failure_marks_session_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            root = Path(tmp) / record["working_dir"] / "idea2video"
            (root / "scene_0" / "shots" / "0").mkdir(parents=True, exist_ok=True)
            (root / "story.txt").write_text("story", encoding="utf-8")
            (root / "characters.json").write_text("[]", encoding="utf-8")
            (root / "script.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "storyboard.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "camera_tree.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "shots" / "0" / "shot_description.json").write_text("{}", encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", side_effect=RuntimeError("missing key")):
                with self.assertRaises(RuntimeError):
                    await adapter.sceneforge_render_video({})
            self.assertEqual(index.get(record["session_id"])["stage"], "error")

    async def test_render_failure_marks_session_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            root = Path(tmp) / record["working_dir"] / "idea2video"
            (root / "scene_0" / "shots" / "0").mkdir(parents=True, exist_ok=True)
            (root / "story.txt").write_text("story", encoding="utf-8")
            (root / "characters.json").write_text("[]", encoding="utf-8")
            (root / "script.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "storyboard.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "camera_tree.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "shots" / "0" / "shot_description.json").write_text("{}", encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters._build_image_generator", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters._build_video_generator", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", FailRenderIdeaPipeline):
                with self.assertRaises(RuntimeError):
                    await adapter.sceneforge_render_video({})
            self.assertEqual(index.get(record["session_id"])["stage"], "error")


    async def test_render_pipeline_stdout_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x", style="anime")
            root = Path(tmp) / record["working_dir"] / "idea2video"
            (root / "scene_0" / "shots" / "0").mkdir(parents=True, exist_ok=True)
            (root / "story.txt").write_text("story", encoding="utf-8")
            (root / "characters.json").write_text("[]", encoding="utf-8")
            (root / "script.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "storyboard.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "camera_tree.json").write_text("[]", encoding="utf-8")
            (root / "scene_0" / "shots" / "0" / "shot_description.json").write_text("{}", encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)
            stdout = io.StringIO()
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()),                  patch("agent_runtime.sceneforge_adapters._build_image_generator", return_value=object()),                  patch("agent_runtime.sceneforge_adapters._build_video_generator", return_value=object()),                  patch("agent_runtime.sceneforge_adapters.Idea2VideoPipeline", NoisyRenderIdeaPipeline),                  contextlib.redirect_stdout(stdout):
                result = await adapter.sceneforge_render_video({})
            self.assertTrue(result.ok)
            self.assertNotIn("NOISE_FROM_RENDER_PIPELINE", stdout.getvalue())

    async def test_render_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_render_video({})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "dependency_missing")


class FakeRegenScriptPipeline:
    last: dict = {}

    def __init__(self, chat_model, image_generator, video_generator, working_dir, **kwargs):
        self.working_dir = Path(working_dir)
        self.kwargs = kwargs

    async def regenerate_shot(self, shot_idx, script, user_requirement, style, keep_description=True, progress=None):
        FakeRegenScriptPipeline.last = {"shot_idx": shot_idx, "keep_description": keep_description, "working_dir": str(self.working_dir)}
        final = self.working_dir / "final_video.mp4"
        final.write_text("video", encoding="utf-8")
        return str(final)


class SceneForgeRegenerateShotTests(unittest.IsolatedAsyncioTestCase):
    def _setup_script_session(self, tmp, *, shots=(0,)):
        index = SessionIndex(tmp)
        record = index.create(idea="x", user_requirement="req", style="anime")
        script_dir = index.working_dir(record["session_id"]) / "script2video"
        (script_dir).mkdir(parents=True, exist_ok=True)
        (script_dir / "camera_tree.json").write_text("[]", encoding="utf-8")
        for s in shots:
            shot_dir = script_dir / "shots" / str(s)
            shot_dir.mkdir(parents=True, exist_ok=True)
            (shot_dir / "shot_description.json").write_text("{}", encoding="utf-8")
        return index, record, script_dir

    async def test_regenerate_shot_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, record, script_dir = self._setup_script_session(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            with patch("agent_runtime.sceneforge_adapters._build_chat_model", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters._build_image_generator", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters._build_video_generator", return_value=object()), \
                 patch("agent_runtime.sceneforge_adapters.Script2VideoPipeline", FakeRegenScriptPipeline):
                result = await adapter.sceneforge_regenerate_shot({"shot_idx": 0, "keep_description": False})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertEqual(payload["shot_idx"], 0)
            self.assertEqual(payload["regenerations"], 1)
            self.assertEqual(FakeRegenScriptPipeline.last["shot_idx"], 0)
            self.assertFalse(FakeRegenScriptPipeline.last["keep_description"])
            self.assertEqual(index.get(record["session_id"])["stage"], "rendered")
            self.assertTrue((Path(tmp) / ".sceneforge" / "logs" / "regenerations.jsonl").exists())

    async def test_regenerate_shot_dependency_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_regenerate_shot({"shot_idx": 0})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "dependency_missing")

    async def test_regenerate_unknown_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, record, script_dir = self._setup_script_session(tmp, shots=(0,))
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_regenerate_shot({"shot_idx": 7})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "unknown_shot")

    async def test_regenerate_shot_budget_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, record, script_dir = self._setup_script_session(tmp)
            # Pre-create 3 archived versions -> at the default cap of 3.
            for v in (1, 2, 3):
                (script_dir / "shots" / "0" / "_archive" / f"v{v}").mkdir(parents=True, exist_ok=True)
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_regenerate_shot({"shot_idx": 0})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "budget_exceeded")
            self.assertEqual(result.metadata["limit"], 3)

    async def test_regenerate_shot_uses_configured_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, record, script_dir = self._setup_script_session(tmp)
            config_dir = Path(tmp) / "configs"
            config_dir.mkdir()
            (config_dir / "script2video.yaml").write_text(
                "generation_budget:\n  max_shot_regenerations: 1\n",
                encoding="utf-8",
            )
            (script_dir / "shots" / "0" / "_archive" / "v1").mkdir(parents=True)
            result = await SceneForgeAdapters(Path(tmp), index).sceneforge_regenerate_shot({"shot_idx": 0})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["limit"], 1)


class SceneForgePublishTests(unittest.IsolatedAsyncioTestCase):
    def _write_config(self, tmp):
        configs = Path(tmp) / "configs"
        configs.mkdir(parents=True, exist_ok=True)
        pub_root = (Path(tmp) / "pub").as_posix()
        (configs / "script2video.yaml").write_text(
            "hosting:\n"
            "  type: local_static\n"
            "  public_base_url: https://cdn.example.com/sceneforge\n"
            f"  local_root: {pub_root}\n"
            "messaging:\n"
            "  outbound_enabled: true\n"
            "  channels:\n"
            "    - type: console\n"
            "      enabled: true\n"
            "      echo: false\n"
            "      default_target: u-1\n",
            encoding="utf-8",
        )

    async def test_publish_hosts_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            final = index.working_dir(record["session_id"]) / "script2video" / "final_video.mp4"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_text("video", encoding="utf-8")
            self._write_config(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_publish({})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertTrue(payload["url"].startswith("https://cdn.example.com/sceneforge/"))
            self.assertEqual(payload["channels_notified"], 1)
            self.assertTrue(payload["hosting_configured"])
            self.assertEqual(index.get(record["session_id"])["stage"], "published")
            self.assertTrue((Path(tmp) / ".sceneforge" / "logs" / "publications.jsonl").exists())

    async def test_publish_without_final_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_publish({})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "dependency_missing")

    async def test_publish_unconfigured_reports_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            final = index.working_dir(record["session_id"]) / "idea2video" / "final_video.mp4"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_text("video", encoding="utf-8")
            adapter = SceneForgeAdapters(Path(tmp), index)  # no configs/ dir
            result = await adapter.sceneforge_publish({})
            self.assertTrue(result.ok)
            payload = json.loads(result.content)
            self.assertIsNone(payload["url"])
            self.assertFalse(payload["hosting_configured"])
            self.assertFalse(payload["messaging_configured"])
            self.assertEqual(payload["channels_notified"], 0)


class SessionIndexReviewTaskTests(unittest.TestCase):
    def test_create_list_get_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            sid = record["session_id"]
            t1 = index.create_review_task(sid, stage="storyboard", summary="共 8 个镜头", artifact_refs=["storyboard.json"])
            t2 = index.create_review_task(sid, stage="final", summary="成片就绪")
            self.assertEqual(t1["review_id"], "rev_1_storyboard")
            self.assertEqual(t2["review_id"], "rev_2_final")
            self.assertEqual(t1["status"], "pending")
            self.assertEqual(len(index.list_review_tasks(sid)), 2)
            self.assertEqual(index.get_review_task(sid, "rev_1_storyboard")["summary"], "共 8 个镜头")

            resolved = index.resolve_review_task(sid, "rev_1_storyboard", "approved")
            self.assertEqual(resolved["status"], "approved")
            self.assertIsNotNone(resolved["resolved_at"])
            with self.assertRaises(KeyError):
                index.resolve_review_task(sid, "nope", "approved")

    def test_review_task_validates_as_model(self):
        from agent_runtime.review import ReviewTask
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            sid = index.create(idea="x")["session_id"]
            task = index.create_review_task(sid, stage="script", summary="s")
            model = ReviewTask.model_validate(task)
            self.assertEqual(model.stage, "script")
            self.assertEqual(model.status, "pending")


class SceneForgeReviewToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_list_resolve_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)

            created = await adapter.sceneforge_review({"action": "create", "stage": "storyboard", "summary": "共 8 镜"})
            self.assertTrue(created.ok)
            review_id = json.loads(created.content)["review_id"]
            self.assertEqual(index.get(record["session_id"])["stage"], "storyboard_review_pending")

            listed = await adapter.sceneforge_review({"action": "list"})
            self.assertEqual(len(json.loads(listed.content)["review_tasks"]), 1)

            resolved = await adapter.sceneforge_review({"action": "resolve", "review_id": review_id, "status": "approved"})
            self.assertTrue(resolved.ok)
            self.assertEqual(json.loads(resolved.content)["status"], "approved")

    async def test_invalid_stage_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            bad_stage = await adapter.sceneforge_review({"action": "create", "stage": "bogus"})
            self.assertFalse(bad_stage.ok)
            self.assertEqual(bad_stage.metadata["error_type"], "invalid_stage")
            await adapter.sceneforge_review({"action": "create", "stage": "final"})
            bad_status = await adapter.sceneforge_review({"action": "resolve", "review_id": "rev_1_final", "status": "bogus"})
            self.assertFalse(bad_status.ok)
            self.assertEqual(bad_status.metadata["error_type"], "invalid_status")

    async def test_resolve_unknown_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            index.create(idea="x")
            adapter = SceneForgeAdapters(Path(tmp), index)
            result = await adapter.sceneforge_review({"action": "resolve", "review_id": "ghost"})
            self.assertFalse(result.ok)
            self.assertEqual(result.metadata["error_type"], "unknown_review")


class BuildVideoGeneratorTests(unittest.TestCase):
    def _build(self, model, provider, base_url="https://yunwu.ai"):
        from agent_runtime import sceneforge_adapters as va
        with patch.object(va, "video_api_key", lambda: "k"), \
             patch.object(va, "video_model", lambda: model), \
             patch.object(va, "video_base_url", lambda: base_url), \
             patch.object(va, "video_provider", lambda: provider):
            return va._build_video_generator()

    def test_seedance_selected_by_provider(self):
        from tools.video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI
        gen = self._build("doubao-seedance-1-5-pro-251215", "seedance")
        self.assertIsInstance(gen, VideoGeneratorDoubaoSeedanceYunwuAPI)
        self.assertEqual(gen.t2v_model, "doubao-seedance-1-5-pro-251215")

    def test_seedance_uses_configured_base_url(self):
        gen = self._build(
            "doubao-seedance-1-5-pro-251215",
            "seedance",
            "https://video.example/v1",
        )
        self.assertEqual(
            gen.task_base_url,
            "https://video.example/volc/v1/contents/generations/tasks",
        )

    def test_seedance_autodetected_from_model_name(self):
        from tools.video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI
        # provider left as yunwu, but a seedance model name still routes to Seedance
        gen = self._build("doubao-seedance-1-5-pro-251215", "yunwu")
        self.assertIsInstance(gen, VideoGeneratorDoubaoSeedanceYunwuAPI)

    def test_veo_yunwu_still_works(self):
        from tools.video_generator_veo_yunwu_api import VideoGeneratorVeoYunwuAPI
        self.assertIsInstance(self._build("veo3.1-fast", "yunwu"), VideoGeneratorVeoYunwuAPI)

    def test_openrouter_still_works(self):
        from tools.video_generator_openrouter_api import VideoGeneratorOpenRouterAPI
        self.assertIsInstance(self._build("veo-3", "openrouter"), VideoGeneratorOpenRouterAPI)

    def test_explicit_profile_builds_the_routed_generator(self):
        from agent_runtime import sceneforge_adapters as va
        from tools.video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI

        with patch.object(va, "video_profile", return_value={
            "profile_id": "fast",
            "api_key": "k",
            "provider": "seedance",
            "transport": "yunwu",
            "model": "seedance-fast",
            "base_url": "https://video.example/v1",
        }):
            generator = va._build_video_generator("fast")

        self.assertIsInstance(generator, VideoGeneratorDoubaoSeedanceYunwuAPI)
        self.assertEqual(generator.t2v_model, "seedance-fast")

    def test_session_route_resolves_video_profile(self):
        from agent_runtime.sceneforge_adapters import _session_video_profile_id

        self.assertEqual(_session_video_profile_id({
            "provider_route": {"routes": [
                {"purpose": "image", "profile_id": "image-main"},
                {"purpose": "video", "profile_id": "cinema"},
            ]},
        }), "cinema")


class RenderConfigInjectionTests(unittest.TestCase):
    def test_web_voiceover_uses_timing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = SceneForgeAdapters(Path(tmp), SessionIndex(tmp))
            service = adapter._build_voiceover_service({
                "audio": {
                    "fit_shot_to_speech": False,
                    "fit_tail_pad": 1.2,
                    "max_shot_extend": 9.0,
                    "sfx": {"enabled": True, "library": None},
                }
            })
            self.assertFalse(service.fit_shot_to_speech)
            self.assertEqual(service.fit_tail_pad, 1.2)
            self.assertEqual(service.max_shot_extend, 9.0)

    def test_render_services_and_auto_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # character asset registry + images
            char_dir = tmp_path / "assets" / "teacher_lin"
            char_dir.mkdir(parents=True, exist_ok=True)
            (char_dir / "front.png").write_text("img", encoding="utf-8")
            registry_path = tmp_path / "assets" / "registry.yaml"
            registry_path.write_text(
                "characters:\n  teacher_lin:\n    display_name: 林老师\n    aliases: [林老师]\n"
                "    type: reference_images\n    description: 老师\n    assets:\n      front: teacher_lin/front.png\n",
                encoding="utf-8",
            )
            # pipeline config enabling character_assets + subtitle + chinese
            configs = tmp_path / "configs"
            configs.mkdir(parents=True, exist_ok=True)
            (configs / "script2video.yaml").write_text(
                "character_assets:\n  enabled: true\n"
                f"  registry_path: {registry_path.as_posix()}\n"
                "subtitle:\n  enabled: true\n"
                "language:\n  chinese_mode: true\n",
                encoding="utf-8",
            )
            # characters.json that should auto-match 林老师 -> teacher_lin
            chars_path = tmp_path / "chars" / "characters.json"
            chars_path.parent.mkdir(parents=True, exist_ok=True)
            chars_path.write_text(
                json.dumps([{"idx": 0, "identifier_in_scene": "林老师", "is_visible": True, "static_features": "", "dynamic_features": ""}]),
                encoding="utf-8",
            )

            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(tmp_path, index)
            services = adapter._render_services("script2video", chars_path)

            self.assertIsNotNone(services["asset_registry"])
            self.assertIsNotNone(services["subtitle_service"])
            self.assertIn("简体中文", services["chinese_instruction"])
            self.assertEqual(services["character_bindings"], {"林老师": "teacher_lin"})

    def test_render_services_empty_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            adapter = SceneForgeAdapters(Path(tmp), index)  # no configs/
            services = adapter._render_services("script2video", Path(tmp) / "nope.json")
            self.assertIsNone(services["asset_registry"])
            self.assertIsNone(services["subtitle_service"])
            self.assertEqual(services["chinese_instruction"], "")
            self.assertEqual(services["character_bindings"], {})
