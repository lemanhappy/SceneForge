"""Pluggable implementations for the staged production workflow.

The workflow engine owns transitions and review gates. Handlers own the
potentially expensive work performed at each gate, which lets desktop, cloud,
and specialist pipelines replace one stage without forking the state machine.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Protocol


_SCENE_HEADER = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:"
    r"场\s*景\s*[一二三四五六七八九十\d]+|第\s*[一二三四五六七八九十\d]+\s*场|"
    r"scene\s*\d+|内景|外景|INT[\.\s]|EXT[\.\s])"
)


def split_script_into_scenes(text: str) -> list[str]:
    """Split a screenplay at scene headers while preserving its exact text."""
    text = (text or "").strip()
    if not text:
        return []
    lines = text.splitlines()
    indexes = [index for index, line in enumerate(lines) if _SCENE_HEADER.match(line)]
    if not indexes:
        return [text]

    scenes: list[str] = []
    preamble = "\n".join(lines[: indexes[0]]).strip()
    for position, start in enumerate(indexes):
        end = indexes[position + 1] if position + 1 < len(indexes) else len(lines)
        scene = "\n".join(lines[start:end]).strip()
        if scene:
            scenes.append(scene)
    if preamble and scenes:
        scenes[0] = f"{preamble}\n{scenes[0]}"
    elif preamble:
        scenes.append(preamble)
    return scenes or [text]


class StageHandler(Protocol):
    """A generation-stage implementation registered under a workflow gate."""

    stage: str

    async def run(
        self,
        engine: Any,
        session: dict,
        instruction: str = "",
        progress: Any = None,
    ) -> str: ...


class StageHandlerRegistry:
    """Small explicit registry used to override individual workflow stages."""

    def __init__(self, handlers: Iterable[StageHandler] = ()):
        self._handlers: dict[str, StageHandler] = {}
        for handler in handlers:
            self.register(handler)

    @classmethod
    def default(cls) -> "StageHandlerRegistry":
        return cls((
            ScriptStageHandler(),
            StoryboardStageHandler(),
            VideoStageHandler(),
            FinalStageHandler(),
        ))

    def register(self, handler: StageHandler, *, replace: bool = False) -> None:
        stage = str(getattr(handler, "stage", "") or "").strip()
        if not stage:
            raise ValueError("Stage handler must declare a non-empty stage")
        if stage in self._handlers and not replace:
            raise ValueError(f"Stage handler already registered: {stage}")
        self._handlers[stage] = handler

    def get(self, stage: str) -> StageHandler:
        try:
            return self._handlers[stage]
        except KeyError as exc:
            raise ValueError(f"Unknown stage handler: {stage}") from exc

    @property
    def stages(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    async def run(
        self,
        stage: str,
        engine: Any,
        session: dict,
        instruction: str = "",
        progress: Any = None,
    ) -> str:
        return await self.get(stage).run(
            engine,
            session,
            instruction=instruction,
            progress=progress,
        )


class ScriptStageHandler:
    stage = "script"

    async def run(self, engine: Any, session: dict, instruction: str = "", progress: Any = None) -> str:
        from agent_runtime.sceneforge_adapters import _UnavailableGenerator, _build_chat_model
        from pipelines.idea2video_pipeline import Idea2VideoPipeline

        idea_dir = engine._idea_dir(session["session_id"])
        mode = str(session.get("mode") or "idea")
        if instruction:
            names = ("characters.json",) if mode == "script" else (
                "story.txt", "characters.json", "script.json")
            for name in names:
                path = idea_dir / name
                if path.exists():
                    path.unlink()
        pipe = Idea2VideoPipeline(
            _build_chat_model(),
            _UnavailableGenerator(),
            _UnavailableGenerator(),
            str(idea_dir),
            chinese_instruction=engine._lang_instruction(session),
            domain=engine._domain(session),
        )
        requirement = engine._augment_requirement(session, instruction)

        if mode == "script":
            script_text = str(session.get("script", "") or "").strip()
            scenes = split_script_into_scenes(script_text)
            idea_dir.mkdir(parents=True, exist_ok=True)
            (idea_dir / "script.json").write_text(
                json.dumps(scenes, ensure_ascii=False, indent=4), encoding="utf-8")
            await pipe.extract_characters(story=script_text, quiet=True)
            return f"剧本已导入，共 {len(scenes)} 个场景（未改写原文）。请回复：通过 / 修改：<你的意见> / 取消"

        story = await pipe.develop_story(
            idea=str(session.get("idea", "")), user_requirement=requirement, quiet=True)
        await pipe.extract_characters(story=story, quiet=True)
        scripts = await pipe.write_script_based_on_story(
            story=story, user_requirement=requirement, quiet=True)
        return f"剧本已生成，共 {len(scripts)} 个场景。请回复：通过 / 修改：<你的意见> / 取消"


class StoryboardStageHandler:
    stage = "storyboard"

    async def run(self, engine: Any, session: dict, instruction: str = "", progress: Any = None) -> str:
        from agent_runtime.sceneforge_adapters import _UnavailableGenerator, _build_chat_model
        from pipelines.script2video_pipeline import Script2VideoPipeline

        idea_dir = engine._idea_dir(session["session_id"])
        characters = engine._load_characters(idea_dir)
        scripts = engine._load_scripts(idea_dir)
        chat = _build_chat_model()
        chinese = engine._lang_instruction(session)
        registry_assets = engine._registry_for(session)
        bindings = registry_assets.match_characters(characters) if registry_assets else {}
        reusable_assets = engine._reusable_assets(session)
        total_shots = 0
        for index, scene_text in enumerate(scripts):
            scene_dir = idea_dir / f"scene_{index}"
            inherited_ledger, inheritance_source = engine._continuity_source(session, index)
            if instruction:
                for name in ("storyboard.json", "camera_tree.json"):
                    path = scene_dir / name
                    if path.exists():
                        path.unlink()
            pipe = Script2VideoPipeline(
                chat,
                _UnavailableGenerator(),
                _UnavailableGenerator(),
                str(scene_dir),
                character_bindings=bindings,
                asset_registry=registry_assets,
                chinese_instruction=chinese,
                domain=engine._domain(session),
                reusable_assets=reusable_assets,
                inherited_continuity_ledger=inherited_ledger,
                continuity_inheritance_source=inheritance_source,
            )
            result = await pipe.plan_text_artifacts(
                script=scene_text,
                user_requirement=engine._augment_requirement(session, instruction),
                style=str(session.get("style", "")),
                characters=characters,
                quiet=True,
            )
            storyboard = result.get("storyboard", []) or []
            total_shots += len(storyboard)
            if engine.artifact_versions is not None:
                storyboard_path = scene_dir / "storyboard.json"
                for shot_index, shot in enumerate(storyboard):
                    payload = shot.model_dump(mode="json") if hasattr(shot, "model_dump") else shot
                    engine.artifact_versions.record_json_item(
                        session["session_id"],
                        index,
                        shot_index,
                        payload,
                        live_path=storyboard_path,
                        input_values={
                            "shot": payload,
                            "script": scene_text,
                            "style": str(session.get("style", "")),
                            "requirement": engine._augment_requirement(session, instruction),
                            "characters": characters,
                        },
                    )
        return f"分镜脚本已生成，共 {len(scripts)} 个场景、约 {total_shots} 个镜头。请回复：通过 / 修改：<意见> / 减少镜头"


class VideoStageHandler:
    stage = "shot_video"

    async def preview_keyframes(
        self,
        engine: Any,
        session: dict,
        progress: Any = None,
        scene_index: int | None = None,
        shot_index: int | None = None,
        force: bool = False,
    ) -> str:
        from agent_runtime.sceneforge_adapters import (
            _build_chat_model,
            _build_image_generator,
            _build_video_generator,
        )
        from pipelines.idea2video_pipeline import Idea2VideoPipeline
        from pipelines.script2video_pipeline import Script2VideoPipeline, aspect_to_size
        from prompting import image_text_constraint

        idea_dir = engine._idea_dir(session["session_id"])
        chat = _build_chat_model()
        image = _build_image_generator()
        from services.lora_runtime import with_project_loras
        image = with_project_loras(image, session)
        video = _build_video_generator(engine.selected_video_profile_id(session))
        cfg = engine._effective_config(session)
        registry_assets = engine._registry_for(session)
        characters = engine._load_characters(idea_dir)
        bindings = registry_assets.match_characters(characters) if registry_assets else {}
        chinese = engine._lang_instruction(session)
        domain = engine._domain(session)
        constraint = image_text_constraint(cfg)
        image_size = aspect_to_size((cfg.get("video") or {}).get("aspect_ratio"))
        retries = int((cfg.get("generation") or {}).get("render_retries", 2))
        image_candidate_count = int((cfg.get("generation") or {}).get("image_candidates", 1))
        reusable_references = engine._reusable_reference_pairs(session)
        reusable_assets = engine._reusable_assets(session)

        portrait_pipe = Idea2VideoPipeline(
            chat,
            image,
            video,
            str(idea_dir),
            character_bindings=bindings,
            asset_registry=registry_assets,
            chinese_instruction=chinese,
            domain=domain,
            image_text_constraint=constraint,
            image_size=image_size,
        )
        portraits = await portrait_pipe.generate_character_portraits(
            characters=characters,
            character_portraits_registry=None,
            style=str(session.get("style", "")),
        )

        total = 0
        targeted = shot_index is not None
        for index, scene_text in enumerate(engine._load_scripts(idea_dir)):
            if scene_index is not None and int(scene_index) != index:
                continue
            scene_dir = idea_dir / f"scene_{index}"
            inherited_ledger, inheritance_source = engine._continuity_source(session, index)
            scene_references = list(reusable_references)
            continuity_reference = engine._continuity_reference_pair(session, index)
            if continuity_reference and continuity_reference not in scene_references:
                scene_references.append(continuity_reference)
            pipe = Script2VideoPipeline(
                chat,
                image,
                video,
                str(scene_dir),
                character_bindings=bindings,
                asset_registry=registry_assets,
                chinese_instruction=chinese,
                render_retries=retries,
                image_candidate_count=image_candidate_count,
                domain=domain,
                image_text_constraint=constraint,
                image_size=image_size,
                global_reference_images=scene_references,
                reusable_assets=reusable_assets,
                inherited_continuity_ledger=inherited_ledger,
                continuity_inheritance_source=inheritance_source,
            )

            def scene_progress(stage, message, meta=None, scene_index=index):
                if progress:
                    progress(
                        stage,
                        f"[场景{scene_index + 1}] {message}",
                        {"scene_idx": scene_index, **(meta or {})},
                    )

            if targeted and force and engine.artifact_versions is not None:
                current_frame = scene_dir / "shots" / str(shot_index) / "first_frame.png"
                if current_frame.is_file():
                    engine.artifact_versions.record_file(
                        session["session_id"],
                        index,
                        int(shot_index),
                        "keyframe",
                        current_frame,
                        input_values={
                            "style": str(session.get("style", "")),
                            "character_bindings": bindings,
                            "source": "before_keyframe_regeneration",
                        },
                    )

            result = await pipe.generate_keyframes(
                script=scene_text,
                user_requirement=str(session.get("user_requirement", "")),
                style=str(session.get("style", "")),
                characters=characters,
                character_portraits_registry=portraits,
                quiet=True,
                progress=scene_progress,
                shot_indexes=[int(shot_index)] if targeted else None,
                force=bool(force),
            )
            total += int(result.get("shot_count") or 0)
            if engine.artifact_versions is not None:
                for shot_dir in sorted((scene_dir / "shots").glob("*")):
                    if not shot_dir.is_dir() or not shot_dir.name.isdigit():
                        continue
                    if targeted and int(shot_dir.name) != int(shot_index):
                        continue
                    first_frame = shot_dir / "first_frame.png"
                    if first_frame.is_file():
                        engine.artifact_versions.record_file(
                            session["session_id"],
                            index,
                            int(shot_dir.name),
                            "keyframe",
                            first_frame,
                            input_values={
                                "style": str(session.get("style", "")),
                                "character_bindings": bindings,
                                "source": "keyframe_preview",
                            },
                        )
        if targeted:
            return f"场景 {int(scene_index or 0) + 1} · 镜 {int(shot_index) + 1} 的首帧已生成。可继续生成下一镜，或重复生成当前镜头。"
        return f"关键帧预览已生成，共 {total} 个镜头。可检查人物与构图后再确认生成视频。"

    async def run(self, engine: Any, session: dict, instruction: str = "", progress: Any = None) -> str:
        from agent_runtime.sceneforge_adapters import (
            _build_chat_model,
            _build_image_generator,
            _build_video_generator,
        )
        from pipelines.idea2video_pipeline import Idea2VideoPipeline
        from pipelines.script2video_pipeline import Script2VideoPipeline, aspect_to_size
        from prompting import image_text_constraint
        from quality import ConsistencyCritic
        from subtitles import SubtitleService
        from utils.video import concatenate_video_files

        idea_dir = engine._idea_dir(session["session_id"])
        chat = _build_chat_model()
        image = _build_image_generator()
        from services.lora_runtime import with_project_loras
        image = with_project_loras(image, session)
        video = _build_video_generator(engine.selected_video_profile_id(session))
        cfg = engine._effective_config(session)
        chinese = engine._lang_instruction(session)
        subtitle = SubtitleService.from_config(cfg)
        registry_assets = engine._registry_for(session)
        characters = engine._load_characters(idea_dir)
        bindings = registry_assets.match_characters(characters) if registry_assets else {}

        voiceover = engine.adapters._build_voiceover_service(cfg) if engine.adapters is not None else None
        video_config = cfg.get("video") or {}
        transition = video_config.get("transition")
        hook = video_config.get("hook")
        cover = video_config.get("cover")
        aigc_label = (cfg.get("compliance") or {}).get("aigc_label")
        critic = ConsistencyCritic.from_config(cfg, chat)
        critic_retries = int(
            ((cfg.get("quality") or {}).get("consistency") or {}).get("max_retries", 1))
        render_retries = int((cfg.get("generation") or {}).get("render_retries", 2))
        image_candidate_count = int((cfg.get("generation") or {}).get("image_candidates", 1))
        video_candidate_count = int((cfg.get("generation") or {}).get("video_candidates", 1))
        video_concurrency = int(
            (((cfg.get("rate_limits") or {}).get("global") or {}).get(
                "max_concurrent_generations", 2
            ))
            or 2
        )
        image_constraint = image_text_constraint(cfg)
        image_size = aspect_to_size(video_config.get("aspect_ratio"))
        domain = engine._domain(session)
        reusable_references = engine._reusable_reference_pairs(session)
        reusable_assets = engine._reusable_assets(session)

        idea_pipe = Idea2VideoPipeline(
            chat,
            image,
            video,
            str(idea_dir),
            character_bindings=bindings,
            asset_registry=registry_assets,
            chinese_instruction=chinese,
            subtitle_service=subtitle,
            domain=domain,
            image_text_constraint=image_constraint,
            image_size=image_size,
        )
        portraits = await idea_pipe.generate_character_portraits(
            characters=characters,
            character_portraits_registry=None,
            style=str(session.get("style", "")),
        )

        scripts = engine._load_scripts(idea_dir)
        scene_finals = []
        for index, scene_text in enumerate(scripts):
            scene_dir = idea_dir / f"scene_{index}"
            inherited_ledger, inheritance_source = engine._continuity_source(session, index)
            scene_references = list(reusable_references)
            continuity_reference = engine._continuity_reference_pair(session, index)
            if continuity_reference and continuity_reference not in scene_references:
                scene_references.append(continuity_reference)
            pipe = Script2VideoPipeline(
                chat,
                image,
                video,
                str(scene_dir),
                character_bindings=bindings,
                asset_registry=registry_assets,
                subtitle_service=subtitle,
                chinese_instruction=chinese,
                voiceover_service=voiceover,
                transition=transition,
                hook=(hook if index == 0 else None),
                cover=None,
                aigc_label=aigc_label,
                consistency_critic=critic,
                consistency_max_retries=critic_retries,
                render_retries=render_retries,
                image_candidate_count=image_candidate_count,
                video_candidate_count=video_candidate_count,
                max_concurrent_video_generations=video_concurrency,
                domain=domain,
                image_text_constraint=image_constraint,
                image_size=image_size,
                global_reference_images=scene_references,
                reusable_assets=reusable_assets,
                inherited_continuity_ledger=inherited_ledger,
                continuity_inheritance_source=inheritance_source,
                generation_context=(
                    engine.selected_video_route(session)
                    if hasattr(engine, "selected_video_route") else {}
                ),
            )

            def scene_progress(stage, message, meta=None, scene_index=index):
                if progress:
                    progress(
                        stage,
                        f"[场景{scene_index + 1}] {message}",
                        {"scene_idx": scene_index, **(meta or {})},
                    )

            final = await pipe(
                script=scene_text,
                user_requirement=str(session.get("user_requirement", "")),
                style=str(session.get("style", "")),
                characters=characters,
                character_portraits_registry=portraits,
                quiet=True,
                progress=scene_progress,
            )
            scene_finals.append(final)
            if engine.artifact_versions is not None:
                for shot_dir in sorted((scene_dir / "shots").glob("*")):
                    if not shot_dir.is_dir() or not shot_dir.name.isdigit():
                        continue
                    shot_index = int(shot_dir.name)
                    description_path = shot_dir / "shot_description.json"
                    try:
                        description = json.loads(description_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        description = {}
                    common_inputs = {
                        "description": description,
                        "style": str(session.get("style", "")),
                        "config": cfg,
                        "character_bindings": bindings,
                    }
                    first_frame = shot_dir / "first_frame.png"
                    if first_frame.is_file():
                        engine.artifact_versions.record_file(
                            session["session_id"],
                            index,
                            shot_index,
                            "keyframe",
                            first_frame,
                            input_values=common_inputs,
                        )
                    shot_video = shot_dir / "video.mp4"
                    if shot_video.is_file():
                        engine.artifact_versions.record_file(
                            session["session_id"],
                            index,
                            shot_index,
                            "video",
                            shot_video,
                            input_values={
                                **common_inputs,
                                "keyframe_sha256": _file_sha256(first_frame),
                            },
                        )

        final_path = idea_dir / "final_video.mp4"
        if not final_path.exists() and scene_finals:
            concatenate_video_files(scene_finals, str(final_path), transition=transition)
        if (cover or {}).get("enabled") and final_path.exists():
            try:
                from video import export_poster

                export_poster(
                    str(final_path),
                    str(idea_dir / cover.get("filename", "poster.jpg")),
                    at_seconds=float(cover.get("at", 0.0)),
                )
            except Exception:
                pass
        return f"分镜视频已生成并合成（{len(scene_finals)} 个场景）。请回复：通过 / 重生成第 N 镜 / 修改：<意见>"


class FinalStageHandler:
    stage = "final"

    async def run(self, engine: Any, session: dict, instruction: str = "", progress: Any = None) -> str:
        return engine._finalize(session)


def _file_sha256(path) -> str:
    import hashlib

    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
