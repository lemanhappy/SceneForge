from __future__ import annotations

import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_openai import OpenAIEmbeddings

from interfaces import CharacterInScene, ShotBriefDescription
from agents.event_extractor import EventExtractor
from agents.global_information_planner import GlobalInformationPlanner
from agents.novel_compressor import NovelCompressor
from agents.scene_extractor import SceneExtractor
from pipelines.novel2movie_pipeline import Novel2MoviePipeline
from pipelines.idea2video_pipeline import Idea2VideoPipeline
from pipelines.script2video_pipeline import Script2VideoPipeline
from tools.image_generator_nanobanana_yunwu_api import ImageGeneratorNanobananaYunwuAPI
from tools.image_generator_doubao_seedream_yunwu_api import ImageGeneratorDoubaoSeedreamYunwuAPI
from tools.reranker_bge_silicon_api import RerankerBgeSiliconapi
from tools.video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI
from tools.video_generator_openrouter_api import VideoGeneratorOpenRouterAPI
from tools.video_generator_veo_yunwu_api import VideoGeneratorVeoYunwuAPI

from .config import embedding_api_key, embedding_base_url, embedding_model, embedding_model_provider, image_api_key, image_base_url, image_model, image_provider, llm_api_key, llm_base_url, llm_model, llm_model_provider, reranker_api_key, reranker_base_url, reranker_model, video_api_key, video_base_url, video_model, video_profile, video_provider
from .models import ToolResult
from .tools import ToolArgumentSchema, ToolRuntimeContext, ToolSpec


class _UnavailableGenerator:
    async def generate_single_image(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Image generator is not available in narrative planning mode")

    async def generate_single_video(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Video generator is not available in narrative planning mode")


def build_sceneforge_adapter_specs(workspace_root: str | Path, session_index: Any) -> list[ToolSpec]:
    adapter = SceneForgeAdapters(Path(workspace_root), session_index)
    return [
        ToolSpec(
            name="sceneforge_narrative_planning",
            description=(
                "Create or revise SceneForge structured text artifacts for the active session. "
                "Idea mode writes story, characters, script, and scene-level storyboard/shot_decomposition/camera_tree under idea2video/scene_<idx>/. "
                "Script mode writes characters, storyboard, shot_decomposition, and camera_tree under script2video/. "
                "For a new video idea or new script, omit session_id or pass the new idea/script; the adapter will create a new session instead of reusing mismatched artifacts. If idea/script/revision_target are omitted and the active session has an idea, continue that session and fill missing structured text artifacts. "
                "It does not generate keyframes, video clips, or final video. Call this before revising storyboard/shots when those artifacts do not exist."
            ),
            handler=adapter.sceneforge_narrative_planning,
            schema={
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "idea": ToolArgumentSchema(str, required=False, default=""),
                "script": ToolArgumentSchema(str, required=False, default=""),
                "user_requirement": ToolArgumentSchema(str, required=False, default=""),
                "style": ToolArgumentSchema(str, required=False, default=""),
                "revision_target": ToolArgumentSchema(str, required=False, default=""),
                "revision_instruction": ToolArgumentSchema(str, required=False, default=""),
            },
        ),
        ToolSpec(
            name="sceneforge_novel_planning",
            description=(
                "Create SceneForge structured text artifacts from a novel or novel excerpt. "
                "This writes novel2video/novel, events, relevant_chunks, scenes, and global_information text artifacts. "
                "Use this when the user provides long prose, a novel excerpt, or asks for novel-to-video planning. "
                "It does not generate character portraits, scene videos, or final video."
            ),
            handler=adapter.sceneforge_novel_planning,
            schema={
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "novel_text": ToolArgumentSchema(str, required=True),
                "user_requirement": ToolArgumentSchema(str, required=False, default=""),
                "style": ToolArgumentSchema(str, required=False, default=""),
            },
        ),
        ToolSpec(
            name="sceneforge_render_video",
            description=(
                "Render keyframes, video clips, and final video for the active SceneForge session. "
                "This checks that structured text artifacts exist before rendering and reports missing dependencies instead of pretending render started."
            ),
            handler=adapter.sceneforge_render_video,
            schema={
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "mode": ToolArgumentSchema(str, required=False, default="foreground"),
                "force": ToolArgumentSchema(bool, required=False, default=False),
            },
        ),
        ToolSpec(
            name="sceneforge_regenerate_shot",
            description=(
                "Regenerate a single shot (and every shot that depends on it) for the active "
                "SceneForge session, instead of rerunning the whole film. The current shot artifacts "
                "are archived (never overwritten) and only the affected shots plus the final "
                "concatenation are recomputed. Use this for feedback like '重生成第 3 镜'. "
                "shot_idx is the 0-based shot index. For idea mode with multiple scenes, pass "
                "scene_index. keep_description=True (default) re-renders from the existing shot "
                "plan; pass False to also re-decompose the shot's visual description."
            ),
            handler=adapter.sceneforge_regenerate_shot,
            schema={
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "shot_idx": ToolArgumentSchema(int, required=True),
                "scene_index": ToolArgumentSchema(int, required=False, default=-1),
                "keep_description": ToolArgumentSchema(bool, required=False, default=True),
            },
        ),
        ToolSpec(
            name="sceneforge_publish",
            description=(
                "Publish the active SceneForge session's finished video: host it (returns a shareable "
                "URL) and回传 the link through any enabled messaging channel (Feishu/console). Use "
                "this only for an explicit '生成分享链接' action after production is complete. "
                "Hosting and messaging are read from the pipeline config (configs/*.yaml); if "
                "neither is configured it reports the local file path without marking it published."
            ),
            handler=adapter.sceneforge_publish,
            schema={
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "target": ToolArgumentSchema(str, required=False, default=""),
                "config_path": ToolArgumentSchema(str, required=False, default=""),
            },
        ),
        ToolSpec(
            name="sceneforge_review",
            description=(
                "Manage structured human-review gates for the active SceneForge session. "
                "action=create opens a pending review for a stage (character/script/storyboard/"
                "shot_video/final) with a summary the user will see; action=list returns all "
                "review tasks; action=resolve marks a review approved/rejected/revised. Use this "
                "to drive the staged审核 flow instead of free-form status strings."
            ),
            handler=adapter.sceneforge_review,
            schema={
                "action": ToolArgumentSchema(str, required=False, default="list"),
                "session_id": ToolArgumentSchema(str, required=False, default=""),
                "stage": ToolArgumentSchema(str, required=False, default=""),
                "summary": ToolArgumentSchema(str, required=False, default=""),
                "review_id": ToolArgumentSchema(str, required=False, default=""),
                "status": ToolArgumentSchema(str, required=False, default="approved"),
            },
        ),
    ]


class SceneForgeAdapters:
    def __init__(self, workspace_root: Path, session_index: Any) -> None:
        self.workspace_root = workspace_root.resolve()
        self.session_index = session_index

    async def sceneforge_narrative_planning(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        idea = str(args.get("idea", "") or "").strip()
        script = str(args.get("script", "") or "").strip()
        user_requirement = str(args.get("user_requirement", "") or "").strip()
        requested_style = str(args.get("style", "") or "").strip()
        style = requested_style
        session = self._resolve_session(str(args.get("session_id", "") or ""), idea=idea, script=script, user_requirement=user_requirement, style=requested_style)
        session_id = session["session_id"]
        working_dir = self.session_index.working_dir(session_id)
        idea_dir = working_dir / "idea2video"
        script_dir = working_dir / "script2video"
        idea_dir.mkdir(parents=True, exist_ok=True)
        script_dir.mkdir(parents=True, exist_ok=True)

        if not idea and not script:
            revision_target = str(args.get("revision_target") or "").strip()
            if revision_target:
                return await self._revise_narrative_artifact(session_id, working_dir, revision_target, str(args.get("revision_instruction") or "").strip(), runtime)
            session_idea = str(session.get("idea") or "").strip()
            if session_idea:
                idea = session_idea
                user_requirement = user_requirement or str(session.get("user_requirement") or "").strip()
                style = requested_style or str(session.get("style") or "").strip() or "Cinematic, coherent, 16:9"
            else:
                return ToolResult("sceneforge_narrative_planning", False, "Provide `idea`, `script`, a revision target, or an active session with an existing idea for narrative planning.", {"error_type": "missing_input", "session_id": session_id})

        style = style or str(session.get("style") or "").strip() or "Cinematic, coherent, 16:9"
        self._update_session_metadata(session_id, idea="", user_requirement="", style=style)

        try:
            self.session_index.update_stage(session_id, "narrative_planning", "Generating structured text artifacts")
            if runtime:
                runtime.emit_progress("Starting narrative planning", stage="starting", metadata={"session_id": session_id})
                await asyncio.sleep(0)
            generated_before = self.session_index.artifact_checklist(session_id)
            if runtime:
                runtime.emit_progress("Initializing bounded chat model", stage="initializing_llm", metadata={"session_id": session_id, "timeout_seconds": _llm_request_timeout_seconds(), "max_tokens": _narrative_max_tokens()})
                await asyncio.sleep(0)
            chat_model = _build_chat_model()
            if runtime:
                runtime.emit_progress("Bounded chat model initialized", stage="chat_model_ready", metadata={"session_id": session_id})
                await asyncio.sleep(0)
            dummy = _UnavailableGenerator()
            # Do not globally redirect stdout/stderr while the JSONL CLI is streaming events.
            # The adapter exposes pipeline progress through explicit tool_progress events instead.
            if idea:
                idea_chinese = self._chinese_instruction("idea2video")
                idea_pipeline = Idea2VideoPipeline(chat_model=chat_model, image_generator=dummy, video_generator=dummy, working_dir=str(idea_dir), chinese_instruction=idea_chinese)
                if runtime:
                    runtime.emit_progress("Idea pipeline initialized", stage="idea_pipeline_ready", metadata={"session_id": session_id})
                    await asyncio.sleep(0)
                story = await _run_planning_step(
                    "Developing story from user idea",
                    "develop_story",
                    idea_pipeline.develop_story(idea=idea, user_requirement=user_requirement, quiet=True),
                    runtime,
                    {"session_id": session_id},
                )
                characters = await _run_planning_step(
                    "Extracting characters from story",
                    "extract_characters",
                    idea_pipeline.extract_characters(story=story, quiet=True),
                    runtime,
                    {"session_id": session_id},
                )
                scene_scripts = await _run_planning_step(
                    "Writing scene scripts from story",
                    "write_script",
                    idea_pipeline.write_script_based_on_story(story=story, user_requirement=user_requirement, quiet=True),
                    runtime,
                    {"session_id": session_id},
                )
                for idx, scene_script in enumerate(scene_scripts if isinstance(scene_scripts, list) else [scene_scripts]):
                    scene_dir = idea_dir / f"scene_{idx}"
                    scene_text = scene_script if isinstance(scene_script, str) else json.dumps(scene_script, ensure_ascii=False, indent=2)
                    script_pipeline = Script2VideoPipeline(chat_model=chat_model, image_generator=dummy, video_generator=dummy, working_dir=str(scene_dir), chinese_instruction=idea_chinese)
                    await _run_planning_step(
                        f"Planning scene {idx} storyboard and shots",
                        "plan_scene",
                        script_pipeline.plan_text_artifacts(script=scene_text, user_requirement=user_requirement, style=style, characters=characters, progress=_pipeline_progress(runtime, session_id, scene_index=idx), quiet=True),
                        runtime,
                        {"session_id": session_id, "scene_index": idx},
                    )
            else:
                script_pipeline = Script2VideoPipeline(chat_model=chat_model, image_generator=dummy, video_generator=dummy, working_dir=str(script_dir), chinese_instruction=self._chinese_instruction("script2video"))
                if runtime:
                    runtime.emit_progress("Script pipeline initialized", stage="script_pipeline_ready", metadata={"session_id": session_id})
                    await asyncio.sleep(0)
                await _run_planning_step(
                    "Planning storyboard and shots from provided script",
                    "plan_script",
                    script_pipeline.plan_text_artifacts(script=script, user_requirement=user_requirement, style=style, progress=_pipeline_progress(runtime, session_id), quiet=True),
                    runtime,
                    {"session_id": session_id},
                )
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Narrative planning failed: {exc}")
            raise

        checklist = self.session_index.artifact_checklist(session_id)
        generated = [path for path, present in checklist.items() if present and not generated_before.get(path)]
        reused = [path for path, present in checklist.items() if present and generated_before.get(path)]
        ready_for_render = _ready_for_render(checklist)
        self.session_index.update_stage(session_id, "narrative_planned", "Structured text planning complete" if ready_for_render else "Structured text planning partially complete")
        if runtime:
            runtime.emit_progress("Narrative planning complete", stage="completed", metadata={"ready_for_render": ready_for_render})
        payload = {
            "session_id": session_id,
            "working_dir": _portable_path(working_dir, self.workspace_root),
            "generated": generated,
            "reused": reused,
            "missing": [path for path, present in checklist.items() if not present],
            "ready_for_render": ready_for_render,
        }
        return ToolResult("sceneforge_narrative_planning", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

    async def _revise_narrative_artifact(self, session_id: str, working_dir: Path, revision_target: str, revision_instruction: str, runtime: ToolRuntimeContext | None = None) -> ToolResult:
        if not revision_instruction:
            self.session_index.update_stage(session_id, "error", "Revision failed: missing revision_instruction")
            return ToolResult("sceneforge_narrative_planning", False, "revision_instruction is required when revision_target is provided.", {"error_type": "missing_revision_instruction", "session_id": session_id, "revision_target": revision_target})
        try:
            target_path = _resolve_artifact_path(working_dir, revision_target)
        except ValueError as exc:
            self.session_index.update_stage(session_id, "error", f"Revision failed: {exc}")
            return ToolResult("sceneforge_narrative_planning", False, str(exc), {"error_type": "invalid_revision_target", "session_id": session_id, "revision_target": revision_target})
        if not target_path.exists():
            self.session_index.update_stage(session_id, "error", f"Revision failed: target does not exist: {revision_target}")
            return ToolResult("sceneforge_narrative_planning", False, f"Revision target does not exist: {revision_target}", {"error_type": "dependency_missing", "session_id": session_id, "revision_target": revision_target})
        try:
            self.session_index.update_stage(session_id, "narrative_planning", "Revising structured text artifact")
            if runtime:
                runtime.emit_progress("Revising structured text artifact", stage="revising", metadata={"session_id": session_id, "revision_target": revision_target})
            chat_model = _build_chat_model()
            before = target_path.read_text(encoding="utf-8")
            revised = await _revise_artifact_with_llm(chat_model, target_path.relative_to(working_dir).as_posix(), before, revision_instruction)
            if target_path.suffix == ".json":
                try:
                    revised_payload = json.loads(revised)
                except json.JSONDecodeError as exc:
                    self.session_index.update_stage(session_id, "error", f"Revision failed: invalid JSON output: {exc}")
                    return ToolResult("sceneforge_narrative_planning", False, f"Revision output was not valid JSON: {exc}", {"error_type": "invalid_revision_json", "session_id": session_id, "revision_target": revision_target})
                revised = json.dumps(revised_payload, ensure_ascii=False, indent=2)
            target_path.write_text(revised, encoding="utf-8")
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Revision failed: {exc}")
            raise

        stale = _stale_keys_for_revision(target_path.relative_to(working_dir).as_posix())
        if stale:
            self.session_index.mark_stale(session_id, stale)
        self.session_index.append_log("revisions", {"session_id": session_id, "target": target_path.relative_to(working_dir).as_posix(), "instruction": revision_instruction, "stale": stale, "before_preview": before[:500], "after_preview": revised[:500]})
        checklist = self.session_index.artifact_checklist(session_id)
        ready_for_render = _ready_for_render(checklist)
        self.session_index.update_stage(session_id, "narrative_planned" if ready_for_render else "narrative_planning", "Revised structured text artifact")
        payload = {
            "session_id": session_id,
            "working_dir": _portable_path(working_dir, self.workspace_root),
            "generated": [],
            "reused": [path for path, present in checklist.items() if present],
            "revised": [target_path.relative_to(working_dir).as_posix()],
            "missing": [path for path, present in checklist.items() if not present],
            "stale": stale,
            "ready_for_render": ready_for_render,
            "revision_target": target_path.relative_to(working_dir).as_posix(),
        }
        return ToolResult("sceneforge_narrative_planning", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

    async def sceneforge_novel_planning(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        novel_text = str(args.get("novel_text", "") or "").strip()
        user_requirement = str(args.get("user_requirement", "") or "").strip()
        style = str(args.get("style", "") or "").strip() or "Cinematic, coherent, 16:9"
        if not novel_text:
            return ToolResult("sceneforge_novel_planning", False, "novel_text is required for novel planning.", {"error_type": "missing_input"})

        session_id_arg = str(args.get("session_id", "") or "").strip()
        session = self.session_index.create(idea=novel_text, user_requirement=user_requirement, style=style, session_id=session_id_arg or None)
        session_id = session["session_id"]
        working_dir = self.session_index.working_dir(session_id)
        novel_dir = working_dir / "novel2video"
        novel_dir.mkdir(parents=True, exist_ok=True)
        generated_before = self.session_index.artifact_checklist(session_id)

        try:
            self.session_index.update_stage(session_id, "novel_planning", "Generating novel structured text artifacts")
            if runtime:
                runtime.emit_progress("Starting novel planning", stage="starting", metadata={"session_id": session_id})
                await asyncio.sleep(0)
            pipeline = _build_novel_pipeline(novel_dir)
            await _run_planning_step(
                "Planning novel structured text artifacts",
                "novel_plan_text_artifacts",
                pipeline.plan_text_artifacts(
                    novel_text=novel_text,
                    user_requirement=user_requirement,
                    style=style,
                    progress=_pipeline_progress(runtime, session_id),
                    quiet=True,
                ),
                runtime,
                {"session_id": session_id},
            )
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Novel planning failed: {exc}")
            return ToolResult("sceneforge_novel_planning", False, str(exc), {"error_type": "exception", "session_id": session_id})

        checklist = self.session_index.artifact_checklist(session_id)
        generated = [path for path, present in checklist.items() if path.startswith("novel2video/") and present and not generated_before.get(path)]
        reused = [path for path, present in checklist.items() if path.startswith("novel2video/") and present and generated_before.get(path)]
        missing = [path for path, present in checklist.items() if path.startswith("novel2video/") and not present]
        ready = _novel_text_ready(checklist)
        self.session_index.update_stage(session_id, "novel_planned" if ready else "novel_planning", "Novel structured text planning complete" if ready else "Novel structured text planning partially complete")
        if runtime:
            runtime.emit_progress("Novel planning complete", stage="completed", metadata={"session_id": session_id, "ready_for_scene_render": False})
        payload = {
            "session_id": session_id,
            "working_dir": _portable_path(working_dir, self.workspace_root),
            "generated": generated,
            "reused": reused,
            "missing": missing,
            "ready_for_scene_render": False,
        }
        return ToolResult("sceneforge_novel_planning", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

    async def sceneforge_render_video(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        session_id = str(args.get("session_id", "") or "").strip()
        session = self.session_index.get(session_id) if session_id else self.session_index.active()
        if session is None:
            return ToolResult("sceneforge_render_video", False, "No active session to render.", {"error_type": "missing_session"})
        session_id = session["session_id"]
        checklist = self.session_index.artifact_checklist(session_id)
        missing = _missing_render_dependencies(checklist)
        if missing:
            return ToolResult("sceneforge_render_video", False, f"Dependency missing: {', '.join(missing)}", {"error_type": "dependency_missing", "missing": missing, "session_id": session_id})

        working_dir = self.session_index.working_dir(session_id)
        self.session_index.update_stage(session_id, "rendering", "Rendering video artifacts")
        try:
            chat_model = _build_chat_model()
            image_generator = _build_image_generator()
            video_generator = _build_video_generator(_session_video_profile_id(session))
            if runtime:
                runtime.emit_progress("Starting video render", stage="rendering", metadata={"session_id": session_id})
            if _idea_mode_ready(checklist):
                services = self._render_services(
                    "idea2video",
                    working_dir / "idea2video" / "characters.json",
                    quality_tier=session.get("quality_tier"),
                )
                idea_pipeline = Idea2VideoPipeline(
                    chat_model=chat_model, image_generator=image_generator, video_generator=video_generator,
                    working_dir=str(working_dir / "idea2video"),
                    asset_registry=services["asset_registry"], character_bindings=services["character_bindings"],
                    subtitle_service=services["subtitle_service"], voiceover_service=services["voiceover_service"],
                    transition=services["transition"], hook=services["hook"], cover=services["cover"],
                    aigc_label=services["aigc_label"], consistency_critic=services["consistency_critic"],
                    consistency_max_retries=services["consistency_max_retries"],
                    render_retries=services["render_retries"],
                    image_candidate_count=services["image_candidate_count"],
                    video_candidate_count=services["video_candidate_count"],
                    max_concurrent_video_generations=services["max_concurrent_video_generations"],
                    chinese_instruction=services["chinese_instruction"],
                )
                with _suppress_pipeline_output():
                    final_video = await idea_pipeline(idea=str(session.get("idea", "")), user_requirement=str(session.get("user_requirement", "")), style=str(session.get("style", "")), quiet=True, hook_text=str(session.get("hook_text", "")))
                self.session_index.update_stage(session_id, "rendered", "Final video rendered")
                payload = {"session_id": session_id, "render_mode": "idea2video", "render_started": True, "render_completed": True, "final_video_path": _portable_path(Path(final_video), self.workspace_root), "missing": []}
                return ToolResult("sceneforge_render_video", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)
            if _script_mode_ready(checklist):
                script_dir = working_dir / "script2video"
                script_text = _load_script_text(working_dir)
                characters = _load_characters(script_dir / "characters.json")
                services = self._render_services(
                    "script2video",
                    script_dir / "characters.json",
                    quality_tier=session.get("quality_tier"),
                )
                pipeline = Script2VideoPipeline(
                    chat_model=chat_model, image_generator=image_generator, video_generator=video_generator,
                    working_dir=str(script_dir),
                    asset_registry=services["asset_registry"], character_bindings=services["character_bindings"],
                    subtitle_service=services["subtitle_service"], voiceover_service=services["voiceover_service"],
                    transition=services["transition"], hook=services["hook"], cover=services["cover"],
                    aigc_label=services["aigc_label"], consistency_critic=services["consistency_critic"],
                    consistency_max_retries=services["consistency_max_retries"],
                    render_retries=services["render_retries"],
                    image_candidate_count=services["image_candidate_count"],
                    video_candidate_count=services["video_candidate_count"],
                    max_concurrent_video_generations=services["max_concurrent_video_generations"],
                    chinese_instruction=services["chinese_instruction"],
                )
                with _suppress_pipeline_output():
                    final_video = await pipeline(script=script_text, user_requirement=str(session.get("user_requirement", "")), style=str(session.get("style", "")), characters=characters, quiet=True, progress=_pipeline_progress(runtime, session_id), hook_text=str(session.get("hook_text", "")))
                self.session_index.update_stage(session_id, "rendered", "Final video rendered")
                payload = {"session_id": session_id, "render_mode": "script2video", "render_started": True, "render_completed": True, "final_video_path": _portable_path(Path(final_video), self.workspace_root), "missing": []}
                return ToolResult("sceneforge_render_video", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)
            if _novel_mode_ready(checklist):
                novel_dir = working_dir / "novel2video"
                pipeline = _build_novel_render_pipeline(novel_dir, chat_model, image_generator, video_generator)
                with _suppress_pipeline_output():
                    render_result = await pipeline.render_video_artifacts(style=str(session.get("style", "")), user_requirement=str(session.get("user_requirement", "")), quiet=True, progress=_pipeline_progress(runtime, session_id))
                scene_videos_dir = Path(render_result["scene_videos_dir"])
                self.session_index.update_stage(session_id, "novel_scene_rendered", "Novel scene videos rendered")
                payload = {
                    "session_id": session_id,
                    "render_mode": "novel2video",
                    "render_started": True,
                    "render_completed": True,
                    "scene_render_completed": True,
                    "final_video_path": None,
                    "scene_videos_dir": _portable_path(scene_videos_dir, self.workspace_root),
                    "scene_video_dirs": [_portable_path(Path(path), self.workspace_root) for path in render_result.get("scene_video_dirs", [])],
                    "scene_count": render_result.get("scene_count", 0),
                    "missing": [],
                }
                return ToolResult("sceneforge_render_video", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Render failed: {exc}")
            raise
        return ToolResult("sceneforge_render_video", False, "No render mode matched current session.", {"error_type": "dependency_missing", "session_id": session_id})

    async def sceneforge_regenerate_shot(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        session_id = str(args.get("session_id", "") or "").strip()
        session = self.session_index.get(session_id) if session_id else self.session_index.active()
        if session is None:
            return ToolResult("sceneforge_regenerate_shot", False, "No active session to regenerate.", {"error_type": "missing_session"})
        session_id = session["session_id"]

        try:
            shot_idx = int(args.get("shot_idx"))
        except (TypeError, ValueError):
            return ToolResult("sceneforge_regenerate_shot", False, "shot_idx must be an integer.", {"error_type": "invalid_shot_idx", "session_id": session_id})

        try:
            scene_index = int(args.get("scene_index", -1))
        except (TypeError, ValueError):
            scene_index = -1
        scene_index = None if scene_index < 0 else scene_index
        keep_description = bool(args.get("keep_description", True))
        locked_dimensions = args.get("locked_dimensions")
        if not isinstance(locked_dimensions, list):
            locked_dimensions = [locked_dimensions] if locked_dimensions else []
        locked_dimensions = sorted({
            str(item).strip().lower()
            for item in locked_dimensions
            if str(item).strip().lower() in {"identity", "composition", "motion", "audio"}
        })

        working_dir = self.session_index.working_dir(session_id)
        render_dir = self._resolve_render_dir(working_dir, scene_index)
        if render_dir is None:
            hint = "; specify scene_index for idea mode with multiple scenes." if scene_index is None else f" for scene_index={scene_index}."
            return ToolResult("sceneforge_regenerate_shot", False, "No rendered scene with a camera_tree.json was found. Render the video before regenerating a shot" + hint, {"error_type": "dependency_missing", "session_id": session_id})

        resolved_scene_index = scene_index
        if resolved_scene_index is None:
            try:
                resolved_scene_index = int(render_dir.name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                resolved_scene_index = 0
        from services.production_metrics import current_generation_id
        rejected_generation_id = current_generation_id(
            working_dir, int(resolved_scene_index), shot_idx
        )

        shot_dir = render_dir / "shots" / str(shot_idx)
        if not shot_dir.exists():
            return ToolResult("sceneforge_regenerate_shot", False, f"Shot {shot_idx} does not exist under {render_dir.name}/shots.", {"error_type": "unknown_shot", "session_id": session_id, "shot_idx": shot_idx})

        # Edited prompt (from the 分镜视频 卡片「改提示词重生成」): persist into the
        # scene storyboard for this shot, then force re-decomposition so the new
        # visual/audio drives fresh frames + video.
        _desc_keys = (
            "visual_desc", "audio_desc", "screen_text", "screen_text_pos",
            "duration_sec", "director_desc", "beats", "visual_style", "avoid",
        )
        new_desc = {k: args.get(k) for k in _desc_keys if args.get(k) is not None}
        if new_desc and str(new_desc.get("visual_desc", "")).strip():
            sb_path = render_dir / "storyboard.json"
            if sb_path.exists():
                try:
                    sb_shots = json.loads(sb_path.read_text(encoding="utf-8"))
                    tgt = next((s for s in sb_shots if s.get("idx") == shot_idx), None)
                    if tgt is None and 0 <= shot_idx < len(sb_shots):
                        tgt = sb_shots[shot_idx]
                    if tgt is not None:
                        tgt["visual_desc"] = str(new_desc["visual_desc"]).strip()
                        if "audio_desc" in new_desc:
                            tgt["audio_desc"] = str(new_desc.get("audio_desc") or "")
                        if "screen_text" in new_desc:
                            tgt["screen_text"] = (str(new_desc["screen_text"]).strip() or None) if new_desc.get("screen_text") else None
                        if "screen_text_pos" in new_desc:
                            tgt["screen_text_pos"] = new_desc.get("screen_text_pos") or None
                        for key in ("duration_sec", "director_desc", "beats", "visual_style", "avoid"):
                            if key in new_desc:
                                tgt[key] = new_desc[key]
                        # Normalize and validate the complete entry while retaining
                        # compatibility defaults for storyboards created before
                        # structured performance beats were introduced.
                        normalized = ShotBriefDescription.model_validate(tgt).model_dump()
                        tgt.clear()
                        tgt.update(normalized)
                        sb_path.write_text(json.dumps(sb_shots, ensure_ascii=False, indent=4), encoding="utf-8")
                        keep_description = False  # re-decompose from the edited storyboard
                except Exception:
                    pass

        mode = "idea2video" if "idea2video" in render_dir.parts else "script2video"
        cap = _max_shot_regenerations(self._mode_config(mode))
        archive_dir = shot_dir / "_archive"
        prior = len(list(archive_dir.glob("v*"))) if archive_dir.exists() else 0
        if prior >= cap:
            return ToolResult("sceneforge_regenerate_shot", False, f"Shot {shot_idx} reached the regeneration limit ({cap}). Raise it in Settings > generation budget.", {"error_type": "budget_exceeded", "session_id": session_id, "shot_idx": shot_idx, "regenerations": prior, "limit": cap})

        self.session_index.update_stage(session_id, "shot_regenerating", f"Regenerating shot {shot_idx}")
        try:
            chat_model = _build_chat_model()
            image_generator = _build_image_generator()
            video_generator = _build_video_generator(_session_video_profile_id(session))
            services = self._render_services(
                mode,
                render_dir / "characters.json",
                quality_tier=session.get("quality_tier"),
            )
            if runtime:
                runtime.emit_progress(f"Regenerating shot {shot_idx}", stage="shot_regenerating", metadata={"session_id": session_id, "shot_idx": shot_idx, "scene_index": scene_index})
            pipeline = Script2VideoPipeline(
                chat_model=chat_model,
                image_generator=image_generator,
                video_generator=video_generator,
                working_dir=str(render_dir),
                render_retries=services["render_retries"],
                image_candidate_count=services["image_candidate_count"],
                video_candidate_count=services["video_candidate_count"],
                max_concurrent_video_generations=services["max_concurrent_video_generations"],
            )
            pipeline.generation_context = _session_video_route(session)
            lock_prompts = {
                "identity": "Preserve the exact character identity, face, hairstyle, wardrobe, and distinguishing details from the approved references.",
                "composition": "Preserve the approved camera position, lens, framing, subject placement, and background geometry.",
                "motion": "Preserve the approved action intent, movement direction, performance beats, and timing.",
                "audio": "Preserve the approved dialogue content, speaker assignment, voice intent, and audio timing.",
            }
            if locked_dimensions:
                pipeline._shot_corrections[shot_idx] = "[Locked constraints] " + " ".join(
                    lock_prompts[item] for item in locked_dimensions
                )
            with _suppress_pipeline_output():
                final_video = await pipeline.regenerate_shot(
                    shot_idx=shot_idx,
                    script=str(session.get("idea", "")),
                    user_requirement=str(session.get("user_requirement", "")),
                    style=str(session.get("style", "")),
                    keep_description=keep_description,
                    progress=_pipeline_progress(runtime, session_id),
                )
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Shot regeneration failed: {exc}")
            raise

        # regenerate_shot already re-rendered the affected shots and re-concatenated
        # the final video, so the artifacts are fresh; no stale marking needed.
        affected_shots = list(getattr(pipeline, "_last_regenerated_shots", []) or [])
        self.session_index.append_log("regenerations", {"session_id": session_id, "shot_idx": shot_idx, "scene_index": scene_index, "keep_description": keep_description, "locked_dimensions": locked_dimensions, "affected_shots": affected_shots, "render_dir": _portable_path(render_dir, self.workspace_root), "prior_regenerations": prior})
        from services.production_metrics import append_decision, rebuild_provider_performance

        replacement_generation_id = current_generation_id(
            working_dir, int(resolved_scene_index), shot_idx
        )
        dimensions = args.get("dimensions")
        if not isinstance(dimensions, list):
            dimensions = [dimensions] if dimensions else []
        append_decision(
            working_dir,
            "regenerated",
            scene_index=int(resolved_scene_index),
            shot_index=shot_idx,
            generation_id=rejected_generation_id,
            reason=str(args.get("reason") or "user_requested"),
            dimensions=dimensions,
            metadata={
                "replacement_generation_id": replacement_generation_id,
                "keep_description": keep_description,
                "locked_dimensions": locked_dimensions,
                "affected_shots": affected_shots,
            },
        )
        rebuild_provider_performance(self.workspace_root, self.session_index)
        self.session_index.update_stage(session_id, "rendered", f"Shot {shot_idx} regenerated")
        payload = {
            "session_id": session_id,
            "shot_idx": shot_idx,
            "scene_index": scene_index,
            "keep_description": keep_description,
            "locked_dimensions": locked_dimensions,
            "affected_shots": affected_shots,
            "regenerations": prior + 1,
            "limit": cap,
            "final_video_path": _portable_path(Path(final_video), self.workspace_root) if final_video else None,
        }
        return ToolResult("sceneforge_regenerate_shot", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

    def _resolve_render_dir(self, working_dir: Path, scene_index: int | None) -> Path | None:
        script_dir = working_dir / "script2video"
        if (script_dir / "camera_tree.json").exists():
            return script_dir
        idea_dir = working_dir / "idea2video"
        if scene_index is not None:
            scene_dir = idea_dir / f"scene_{scene_index}"
            return scene_dir if (scene_dir / "camera_tree.json").exists() else None
        candidates = [s for s in sorted(idea_dir.glob("scene_*")) if (s / "camera_tree.json").exists()] if idea_dir.exists() else []
        return candidates[0] if len(candidates) == 1 else None

    async def sceneforge_publish(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        from artifacts import ArtifactHost
        from channels import ChannelDispatcher

        session_id = str(args.get("session_id", "") or "").strip()
        session = self.session_index.get(session_id) if session_id else self.session_index.active()
        if session is None:
            return ToolResult("sceneforge_publish", False, "No active session to publish.", {"error_type": "missing_session"})
        session_id = session["session_id"]

        working_dir = self.session_index.working_dir(session_id)
        final_video = self._find_final_video(working_dir)
        if final_video is None:
            return ToolResult("sceneforge_publish", False, "No final video found; render the session before publishing.", {"error_type": "dependency_missing", "session_id": session_id})

        config = self._load_publish_config(str(args.get("config_path", "") or ""))
        host = ArtifactHost.from_config(config)
        dispatcher = ChannelDispatcher.from_config(config)

        if host is None:
            payload = {
                "session_id": session_id,
                "final_video_path": _portable_path(final_video, self.workspace_root),
                "url": None,
                "hosted": None,
                "channels_notified": 0,
                "hosting_configured": False,
                "messaging_configured": dispatcher is not None,
                "exported_only": True,
            }
            return ToolResult("sceneforge_publish", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

        hosted = None
        try:
            hosted = await host.upload(final_video)
            sent = 0
            if dispatcher is not None:
                target = str(args.get("target", "") or "") or None
                sent = len(await dispatcher.broadcast_artifact(hosted, target=target))
        except Exception as exc:
            self.session_index.update_stage(session_id, "error", f"Publish failed: {exc}")
            raise

        self.session_index.append_log("publications", {"session_id": session_id, "final_video": _portable_path(final_video, self.workspace_root), "url": getattr(hosted, "url", None), "channels_notified": sent})
        self.session_index.update_stage(session_id, "published", "Final video published")
        payload = {
            "session_id": session_id,
            "final_video_path": _portable_path(final_video, self.workspace_root),
            "url": getattr(hosted, "url", None),
            "hosted": hosted.model_dump() if hosted is not None else None,
            "channels_notified": sent,
            "hosting_configured": host is not None,
            "messaging_configured": dispatcher is not None,
            "exported_only": False,
        }
        return ToolResult("sceneforge_publish", True, json.dumps(payload, ensure_ascii=False, indent=2), payload)

    def publish_capabilities(self) -> dict[str, Any]:
        from artifacts import ArtifactHost
        from channels import ChannelDispatcher

        config = self._load_publish_config("")
        host = ArtifactHost.from_config(config)
        dispatcher = ChannelDispatcher.from_config(config)
        return {
            "share_enabled": host is not None,
            "messaging_enabled": dispatcher is not None,
        }

    async def sceneforge_review(self, args: dict[str, Any], runtime: ToolRuntimeContext | None = None) -> ToolResult:
        from .review import REVIEW_STAGES, REVIEW_STATUSES

        session_id = str(args.get("session_id", "") or "").strip()
        session = self.session_index.get(session_id) if session_id else self.session_index.active()
        if session is None:
            return ToolResult("sceneforge_review", False, "No active session.", {"error_type": "missing_session"})
        session_id = session["session_id"]
        action = str(args.get("action", "list") or "list").strip().lower()

        if action == "list":
            tasks = self.session_index.list_review_tasks(session_id)
            return ToolResult("sceneforge_review", True, json.dumps({"session_id": session_id, "review_tasks": tasks}, ensure_ascii=False, indent=2), {"session_id": session_id, "review_tasks": tasks})

        if action == "create":
            stage = str(args.get("stage", "") or "").strip()
            if stage not in REVIEW_STAGES:
                return ToolResult("sceneforge_review", False, f"stage must be one of {REVIEW_STAGES}.", {"error_type": "invalid_stage", "session_id": session_id})
            task = self.session_index.create_review_task(session_id, stage=stage, summary=str(args.get("summary", "") or ""))
            self.session_index.update_stage(session_id, f"{stage}_review_pending", f"Review pending for {stage}")
            return ToolResult("sceneforge_review", True, json.dumps(task, ensure_ascii=False, indent=2), task)

        if action == "resolve":
            review_id = str(args.get("review_id", "") or "").strip()
            status = str(args.get("status", "approved") or "approved").strip().lower()
            if status not in REVIEW_STATUSES:
                return ToolResult("sceneforge_review", False, f"status must be one of {REVIEW_STATUSES}.", {"error_type": "invalid_status", "session_id": session_id})
            try:
                task = self.session_index.resolve_review_task(session_id, review_id, status)
            except KeyError:
                return ToolResult("sceneforge_review", False, f"Unknown review_id: {review_id}", {"error_type": "unknown_review", "session_id": session_id, "review_id": review_id})
            return ToolResult("sceneforge_review", True, json.dumps(task, ensure_ascii=False, indent=2), task)

        return ToolResult("sceneforge_review", False, f"Unknown action: {action}", {"error_type": "invalid_action", "session_id": session_id})

    def _find_final_video(self, working_dir: Path) -> Path | None:
        candidates = [
            working_dir / "script2video" / "final_video_with_subtitles.mp4",
            working_dir / "script2video" / "final_video.mp4",
            working_dir / "idea2video" / "final_video_with_subtitles.mp4",
            working_dir / "idea2video" / "final_video.mp4",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _load_publish_config(self, config_path: str) -> dict[str, Any]:
        """Merge hosting/messaging sections from the first pipeline config that
        declares them (explicit config_path wins, then the standard configs)."""
        import yaml

        candidates = []
        if config_path:
            candidates.append(Path(config_path))
        candidates += [self.workspace_root / "configs" / "script2video.yaml", self.workspace_root / "configs" / "idea2video.yaml"]
        merged: dict[str, Any] = {}
        for candidate in candidates:
            if candidate and candidate.exists():
                try:
                    data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                for key in ("hosting", "messaging"):
                    if key in data and key not in merged:
                        merged[key] = data[key]
        return merged

    def _mode_config(self, mode: str) -> dict[str, Any]:
        """Load configs/<mode>.yaml so the agent-driven path honours the same
        character_assets / subtitle / language settings as the CLI path."""
        import yaml
        path = self.workspace_root / "configs" / f"{mode}.yaml"
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _chinese_instruction(self, mode: str) -> str:
        from prompting import is_chinese_mode, chinese_runtime_instruction
        config = self._mode_config(mode)
        return chinese_runtime_instruction(config) if is_chinese_mode(config) else ""

    def _render_services(
        self,
        mode: str,
        characters_path: Path,
        quality_tier: str | None = None,
    ) -> dict[str, Any]:
        from characters import AssetCatalog
        from services.quality_profiles import apply_quality_profile
        from subtitles import SubtitleService
        config = self._mode_config(mode)
        if quality_tier:
            config = apply_quality_profile(config, quality_tier)
        asset_registry = AssetCatalog.from_config(config)
        generation = config.get("generation") or {}
        global_limits = (config.get("rate_limits") or {}).get("global") or {}
        return {
            "asset_registry": asset_registry,
            "subtitle_service": SubtitleService.from_config(config),
            "voiceover_service": self._build_voiceover_service(config),
            "transition": (config.get("video") or {}).get("transition"),
            "hook": (config.get("video") or {}).get("hook"),
            "cover": (config.get("video") or {}).get("cover"),
            "aigc_label": (config.get("compliance") or {}).get("aigc_label"),
            **self._consistency_services(config),
            "render_retries": int(generation.get("render_retries", 2)),
            "image_candidate_count": int(generation.get("image_candidates", 1)),
            "video_candidate_count": int(generation.get("video_candidates", 1)),
            "max_concurrent_video_generations": int(global_limits.get("max_concurrent_generations", 2) or 2),
            "chinese_instruction": self._chinese_instruction(mode),
            "character_bindings": self._resolve_character_bindings(asset_registry, characters_path),
        }

    def _consistency_services(self, config: dict) -> dict[str, Any]:
        qc = (config.get("quality") or {}).get("consistency") or {}
        critic = None
        if qc.get("enabled"):
            try:
                from quality import ConsistencyCritic
                critic = ConsistencyCritic.from_config(config, _build_chat_model())
            except Exception:
                critic = None
        return {"consistency_critic": critic, "consistency_max_retries": int(qc.get("max_retries", 1))}

    def _build_voiceover_service(self, config: dict):
        """Build a VoiceoverService for the agent/web path when any of
        ``audio.tts`` / ``audio.bgm`` / ``audio.sfx`` is enabled. Unlike the
        CLI's VoiceoverService.from_config (which reads the TTS key from the
        config section/env), here the key comes from agent.local.yaml via the
        shared config getters, so a single configured LLM key drives TTS too."""
        audio = (config or {}).get("audio") or {}
        tts = audio.get("tts") or {}
        bgm = audio.get("bgm") or {}
        sfx = audio.get("sfx") or {}
        loud = audio.get("loudnorm") or {}
        tts_on, bgm_on, sfx_on = bool(tts.get("enabled")), bool(bgm.get("enabled")), bool(sfx.get("enabled"))
        if not (tts_on or bgm_on or sfx_on):
            return None

        from audio import VoiceoverService
        from audio.service import make_provider, resolve_bgm_path
        from .config import tts_api_key, tts_base_url, tts_model, tts_voice

        provider = None
        if tts_on:
            api_key = tts_api_key(self.workspace_root)
            if api_key:
                # Fill section defaults from the shared config getters so a single
                # configured LLM key + gateway drive TTS too.
                section = dict(tts)
                section.setdefault("base_url", tts_base_url(self.workspace_root))
                if (section.get("provider") or "openai").lower() != "minimax":
                    section.setdefault("model", tts_model(self.workspace_root))
                    section.setdefault("voice", tts_voice(self.workspace_root))
                provider = make_provider(api_key, section["base_url"], section)
        return VoiceoverService(
            provider=provider,
            voice=None,  # voice is baked into the provider
            mix_with_original=bool(tts.get("mix_with_original", False)),
            enabled=True,
            bgm_path=resolve_bgm_path(bgm) if bgm_on else None,
            bgm_volume=float(bgm.get("volume", 0.2)),
            sfx_library=(sfx.get("library") if sfx_on else None),
            sfx_volume=float(sfx.get("volume", 0.8)),
            loudnorm=bool(loud.get("enabled", True)),
            loudnorm_i=float(loud.get("i", -16.0)),
            loudnorm_tp=float(loud.get("tp", -1.5)),
            loudnorm_lra=float(loud.get("lra", 11.0)),
            fit_shot_to_speech=bool(audio.get("fit_shot_to_speech", True)),
            fit_tail_pad=float(audio.get("fit_tail_pad", 0.4)),
            max_shot_extend=float(audio.get("max_shot_extend", 6.0)),
        )

    def _resolve_character_bindings(self, asset_registry, characters_path: Path) -> dict[str, str]:
        """Resolve {identifier_in_scene: asset_id}. Prefer an explicit
        character_bindings.json next to characters.json; otherwise auto-match by
        name/alias against the registry (safe: only active when character_assets
        is enabled)."""
        if asset_registry is None or not characters_path.exists():
            return {}
        bindings_path = characters_path.parent / "character_bindings.json"
        if bindings_path.exists():
            try:
                data = json.loads(bindings_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if isinstance(v, str)}
            except Exception:
                pass
        try:
            from interfaces import CharacterInScene
            characters = [CharacterInScene.model_validate(c) for c in json.loads(characters_path.read_text(encoding="utf-8"))]
        except Exception:
            return {}
        return asset_registry.match_characters(characters)

    def _resolve_session(self, session_id: str, *, idea: str, script: str, user_requirement: str, style: str) -> dict[str, Any]:
        requested_source = idea or script
        if session_id:
            session = self.session_index.get(session_id)
            if session is None:
                session = self.session_index.create(idea=requested_source, user_requirement=user_requirement, style=style, session_id=session_id)
            elif requested_source and _is_new_source_for_session(session, requested_source):
                session = self.session_index.create(idea=requested_source, user_requirement=user_requirement, style=style)
            else:
                self.session_index.set_active(session_id)
        else:
            if requested_source:
                session = self.session_index.create(idea=requested_source, user_requirement=user_requirement, style=style)
            else:
                session = self.session_index.active() or self.session_index.create(idea=requested_source, user_requirement=user_requirement, style=style)
        self._update_session_metadata(session["session_id"], idea=requested_source, user_requirement=user_requirement, style=style)
        return self.session_index.get(session["session_id"]) or session

    def _update_session_metadata(self, session_id: str, *, idea: str, user_requirement: str, style: str) -> None:
        self.session_index.update_metadata(
            session_id,
            idea=idea,
            user_requirement=user_requirement,
            style=style,
        )


class _DiscardStream:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


_PIPELINE_OUTPUT_SINK = _DiscardStream()


@contextmanager
def _suppress_pipeline_output():
    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with redirect_stdout(_PIPELINE_OUTPUT_SINK), redirect_stderr(_PIPELINE_OUTPUT_SINK):
            yield
    finally:
        logging.disable(previous_disable_level)


def _max_shot_regenerations(config: dict | None = None) -> int:
    configured = ((config or {}).get("generation_budget") or {}).get(
        "max_shot_regenerations", 3
    )
    raw = os.environ.get("SCENEFORGE_MAX_SHOT_REGENERATIONS", configured)
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _narrative_step_timeout_seconds() -> float:
    raw = os.environ.get("SCENEFORGE_NARRATIVE_STEP_TIMEOUT_SECONDS", "900")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 900.0


async def _run_planning_step(
    message: str,
    stage: str,
    awaitable: Any,
    runtime: ToolRuntimeContext | None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    timeout_seconds = _narrative_step_timeout_seconds()
    event_metadata = dict(metadata or {})
    event_metadata["timeout_seconds"] = timeout_seconds
    if runtime:
        runtime.emit_progress(message, stage=stage, metadata=event_metadata)
        await asyncio.sleep(0)
    try:
        with _suppress_pipeline_output():
            if timeout_seconds <= 0:
                return await awaitable
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{message} timed out after {timeout_seconds:g}s") from exc
    except Exception as exc:
        raise RuntimeError(f"{message} failed: {exc}") from exc


def _is_new_source_for_session(session: dict[str, Any], requested_source: str) -> bool:
    current = str(session.get("idea") or "").strip()
    requested = requested_source.strip()
    if not current or not requested:
        return False
    return current != requested


def _llm_request_timeout_seconds() -> float:
    raw = os.environ.get("SCENEFORGE_LLM_REQUEST_TIMEOUT_SECONDS", "300")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 300.0


def _narrative_max_tokens() -> int:
    raw = os.environ.get("SCENEFORGE_NARRATIVE_MAX_TOKENS", "4096")
    try:
        return max(256, int(raw))
    except ValueError:
        return 4096


def _pipeline_progress(runtime: ToolRuntimeContext | None, session_id: str, *, scene_index: int | None = None):
    if runtime is None:
        return None

    def emit(stage: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        payload = dict(metadata or {})
        payload["session_id"] = session_id
        if scene_index is not None:
            payload["scene_index"] = scene_index
        runtime.emit_progress(message, stage=stage, metadata=payload)

    return emit


def _build_chat_model() -> Any:
    api_key = llm_api_key()
    if not api_key:
        raise RuntimeError("SCENEFORGE_LLM_API_KEY or configs/agent.local.yaml llm.api_key is required for narrative planning")
    return init_chat_model(
        model=llm_model(),
        model_provider=llm_model_provider(),
        api_key=api_key,
        base_url=llm_base_url(),
        timeout=_llm_request_timeout_seconds(),
        max_retries=0,
        max_completion_tokens=_narrative_max_tokens(),
    )


def _build_image_generator() -> Any:
    api_key = image_api_key()
    if not api_key:
        raise RuntimeError("SCENEFORGE_IMAGE_API_KEY, SCENEFORGE_LLM_API_KEY, or configs/agent.local.yaml image/llm api_key is required for image generation")
    model = image_model()
    provider = image_provider().strip().lower()
    # Doubao Seedream (即梦, strong at Chinese text) — selected explicitly via
    # provider=seedream/doubao, or auto-detected from a seedream model name.
    if provider in ("seedream", "doubao") or "seedream" in model.lower():
        return ImageGeneratorDoubaoSeedreamYunwuAPI(
            api_key=api_key, model=model, base_url=image_base_url()
        )
    # default: nano-banana (gemini-image) on yunwu
    return ImageGeneratorNanobananaYunwuAPI(api_key=api_key, model=model, base_url=image_base_url())


def _build_video_generator(profile_id: str | None = None) -> VideoGeneratorVeoYunwuAPI | VideoGeneratorOpenRouterAPI | VideoGeneratorDoubaoSeedanceYunwuAPI:
    profile = video_profile(profile_id) if profile_id else None
    api_key = str(profile.get("api_key") or "") if profile else video_api_key()
    if not api_key:
        raise RuntimeError("SCENEFORGE_VIDEO_API_KEY, SCENEFORGE_LLM_API_KEY, or configs/agent.local.yaml video/llm api_key is required for video generation")
    model = str(profile.get("model") or "") if profile else video_model()
    base_url = str(profile.get("base_url") or "") if profile else video_base_url()
    transport = str(profile.get("transport") or "").strip().lower() if profile else video_provider().strip().lower()
    model_provider = str(profile.get("provider") or "").strip().lower() if profile else ""
    if transport == "openrouter":
        return VideoGeneratorOpenRouterAPI(api_key=api_key, model=model, base_url=base_url)
    # Doubao Seedance on yunwu: selected by provider=seedance/doubao, or auto-detected
    # from a seedance model name even when provider is left as "yunwu".
    if model_provider in ("seedance", "doubao") or "seedance" in model.lower():
        return VideoGeneratorDoubaoSeedanceYunwuAPI(
            api_key=api_key,
            t2v_model=model,
            ff2v_model=model,
            flf2v_model=model,
            base_url=base_url,
        )
    if transport == "yunwu" or model_provider == "veo" or "veo" in model.lower():
        return VideoGeneratorVeoYunwuAPI(api_key=api_key, t2v_model=model, ff2v_model=model, base_url=base_url)
    raise RuntimeError(
        f"Unsupported video profile '{profile_id or 'legacy'}' "
        f"(provider={model_provider or 'auto'}, transport={transport or 'unknown'}, base_url={base_url})"
    )


def _session_video_profile_id(session: dict[str, Any] | None) -> str | None:
    session = session or {}
    explicit = str(session.get("video_profile_id") or "").strip()
    if explicit:
        return explicit
    route = session.get("provider_route")
    routes = route.get("routes") if isinstance(route, dict) else []
    for item in routes or []:
        if isinstance(item, dict) and item.get("purpose") == "video":
            value = str(item.get("profile_id") or "").strip()
            return value or None
    return None


def _session_video_route(session: dict[str, Any] | None) -> dict[str, Any]:
    session = session or {}
    profile_id = _session_video_profile_id(session)
    route = session.get("provider_route")
    routes = route.get("routes") if isinstance(route, dict) else []
    for item in routes or []:
        if isinstance(item, dict) and item.get("purpose") == "video":
            return dict(item)
    profile = video_profile(profile_id) if profile_id else None
    if not isinstance(profile, dict):
        return {"purpose": "video", "profile_id": profile_id} if profile_id else {}
    allowed = {
        "profile_id", "provider", "provider_id", "transport", "transport_id",
        "model", "model_id", "quality_tier", "estimated_cost", "currency",
    }
    result = {key: value for key, value in profile.items() if key in allowed}
    result["purpose"] = "video"
    result.setdefault("profile_id", profile_id)
    result.setdefault("provider_id", result.get("provider"))
    result.setdefault("model_id", result.get("model"))
    return result


class _IdentityRewriter:
    async def __call__(self, prompt: str) -> str:
        return prompt


def _build_embedding_model() -> Any:
    api_key = embedding_api_key()
    base_url = embedding_base_url()
    provider = embedding_model_provider().strip().lower()
    if not api_key or not base_url:
        raise RuntimeError("SCENEFORGE_EMBEDDING_API_KEY or configs/agent.local.yaml embedding api_key/base_url is required for novel planning")
    if provider != "openai":
        raise RuntimeError(f"Unsupported embedding model_provider: {provider}")
    return OpenAIEmbeddings(model=embedding_model(), api_key=api_key, base_url=base_url)


def _build_reranker() -> RerankerBgeSiliconapi:
    api_key = reranker_api_key()
    base_url = reranker_base_url()
    if not api_key or not base_url:
        raise RuntimeError("SCENEFORGE_RERANKER_API_KEY or configs/agent.local.yaml reranker api_key/base_url is required for novel planning")
    return RerankerBgeSiliconapi(api_key=api_key, base_url=base_url, model=reranker_model())


def _build_novel_pipeline(working_dir: Path) -> Novel2MoviePipeline:
    api_key = llm_api_key()
    if not api_key:
        raise RuntimeError("SCENEFORGE_LLM_API_KEY or configs/agent.local.yaml llm.api_key is required for novel planning")
    base_url = llm_base_url()
    model = llm_model()
    dummy = _UnavailableGenerator()
    return Novel2MoviePipeline(
        novel_compressor=NovelCompressor(api_key=api_key, base_url=base_url, chat_model=model),
        event_extractor=EventExtractor(api_key=api_key, base_url=base_url, chat_model=model),
        embeddings=_build_embedding_model(),
        rerank_model=_build_reranker(),
        scene_extractor=SceneExtractor(api_key=api_key, base_url=base_url, chat_model=model),
        global_information_planner=GlobalInformationPlanner(api_key=api_key, base_url=base_url, chat_model=model),
        image_generator=dummy,
        rewriter=_IdentityRewriter(),
        script2video_pipeline=dummy,
        working_dir=str(working_dir),
    )


def _build_novel_render_pipeline(working_dir: Path, chat_model: Any, image_generator: Any, video_generator: Any) -> Novel2MoviePipeline:
    api_key = llm_api_key()
    if not api_key:
        raise RuntimeError("SCENEFORGE_LLM_API_KEY or configs/agent.local.yaml llm.api_key is required for novel rendering")
    base_url = llm_base_url()
    model = llm_model()
    script_pipeline = Script2VideoPipeline(chat_model=chat_model, image_generator=image_generator, video_generator=video_generator, working_dir=str(working_dir / "videos"))
    return Novel2MoviePipeline(
        novel_compressor=NovelCompressor(api_key=api_key, base_url=base_url, chat_model=model),
        event_extractor=EventExtractor(api_key=api_key, base_url=base_url, chat_model=model),
        embeddings=_build_embedding_model(),
        rerank_model=_build_reranker(),
        scene_extractor=SceneExtractor(api_key=api_key, base_url=base_url, chat_model=model),
        global_information_planner=GlobalInformationPlanner(api_key=api_key, base_url=base_url, chat_model=model),
        image_generator=image_generator,
        rewriter=_IdentityRewriter(),
        script2video_pipeline=script_pipeline,
        working_dir=str(working_dir),
    )


def _write_characters_if_missing(path: Path, characters: list[CharacterInScene]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([character.model_dump() for character in characters], ensure_ascii=False, indent=2), encoding="utf-8")


def _load_characters(path: Path) -> list[CharacterInScene]:
    return [CharacterInScene.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def _load_script_text(working_dir: Path) -> str:
    idea_script = working_dir / "idea2video" / "script.json"
    if idea_script.exists():
        payload = json.loads(idea_script.read_text(encoding="utf-8"))
        return json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload
    story = working_dir / "idea2video" / "story.txt"
    if story.exists():
        return story.read_text(encoding="utf-8")
    return ""


def _resolve_artifact_path(working_dir: Path, revision_target: str) -> Path:
    rel = Path(revision_target)
    if rel.is_absolute():
        raise ValueError(f"revision_target must be relative to session working_dir: {revision_target}")
    path = (working_dir / rel).resolve()
    if path != working_dir and working_dir not in path.parents:
        raise ValueError(f"revision_target escapes session working_dir: {revision_target}")
    return path


async def _revise_artifact_with_llm(chat_model: Any, target: str, current_text: str, instruction: str) -> str:
    prompt = (
        "Revise this SceneForge structured artifact exactly as requested. "
        "Return only the complete replacement file content, with no Markdown fences or explanation. "
        "If the file is JSON, preserve valid JSON and the existing schema shape.\n\n"
        f"Target: {target}\n"
        f"Revision instruction: {instruction}\n\n"
        "Current file content:\n"
        f"{current_text}"
    )
    if hasattr(chat_model, "ainvoke"):
        response = await chat_model.ainvoke(prompt)
    elif hasattr(chat_model, "invoke"):
        response = chat_model.invoke(prompt)
    else:
        raise RuntimeError("chat_model does not support invoke/ainvoke for revision mode")
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
    return _strip_markdown_fences(str(content).strip())


def _strip_markdown_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _portable_path(path: Path, workspace_root: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(workspace_root.resolve()))
    except ValueError:
        return str(resolved)


def _stale_keys_for_revision(target: str) -> list[str]:
    if "storyboard.json" in target:
        return ["shot_descriptions", "camera_tree", "frames", "clips", "final_video"]
    if "shot_description.json" in target:
        return ["frames", "clips", "final_video"]
    if "camera_tree.json" in target:
        return ["frames", "clips", "final_video"]
    if target.endswith("script.json") or target.endswith("story.txt"):
        return ["storyboard", "shot_descriptions", "camera_tree", "frames", "clips", "final_video"]
    if target.endswith("characters.json"):
        return ["storyboard", "shot_descriptions", "frames", "clips", "final_video"]
    return ["frames", "clips", "final_video"]


def _ready_for_render(checklist: dict[str, bool]) -> bool:
    return _idea_mode_ready(checklist) or _script_mode_ready(checklist) or _novel_mode_ready(checklist)


def _missing_render_dependencies(checklist: dict[str, bool]) -> list[str]:
    if _ready_for_render(checklist):
        return []
    idea_required = ["idea2video/story.txt", "idea2video/characters.json", "idea2video/script.json", "idea2video/scene_*/storyboard.json", "idea2video/scene_*/shots/*/shot_description.json", "idea2video/scene_*/camera_tree.json"]
    script_required = ["script2video/characters.json", "script2video/storyboard.json", "script2video/shots/*/shot_description.json", "script2video/camera_tree.json"]
    novel_required = ["novel2video/novel/novel_compressed.txt", "novel2video/events/event_*.json", "novel2video/relevant_chunks/event_*", "novel2video/scenes/event_*/scene_*.json", "novel2video/global_information/characters/event_level/*.json", "novel2video/global_information/characters/novel_level/*.json"]
    return [f"idea mode: {path}" for path in idea_required if not checklist.get(path)] + [f"script mode: {path}" for path in script_required if not checklist.get(path)] + [f"novel mode: {path}" for path in novel_required if not checklist.get(path)]


def _idea_mode_ready(checklist: dict[str, bool]) -> bool:
    return bool(checklist.get("idea2video/story.txt") and checklist.get("idea2video/characters.json") and checklist.get("idea2video/script.json") and checklist.get("idea2video/scene_*/storyboard.json") and checklist.get("idea2video/scene_*/shots/*/shot_description.json") and checklist.get("idea2video/scene_*/camera_tree.json"))


def _novel_text_ready(checklist: dict[str, bool]) -> bool:
    return _novel_mode_ready(checklist)


def _novel_mode_ready(checklist: dict[str, bool]) -> bool:
    return bool(checklist.get("novel2video/novel/novel_compressed.txt") and checklist.get("novel2video/events/event_*.json") and checklist.get("novel2video/relevant_chunks/event_*") and checklist.get("novel2video/scenes/event_*/scene_*.json") and checklist.get("novel2video/global_information/characters/event_level/*.json") and checklist.get("novel2video/global_information/characters/novel_level/*.json"))


def _script_mode_ready(checklist: dict[str, bool]) -> bool:
    return bool(checklist.get("script2video/characters.json") and checklist.get("script2video/storyboard.json") and checklist.get("script2video/shots/*/shot_description.json") and checklist.get("script2video/camera_tree.json"))
