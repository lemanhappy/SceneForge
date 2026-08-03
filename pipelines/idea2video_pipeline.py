import os
import logging
from agents import Screenwriter, CharacterExtractor, CharacterPortraitsGenerator
from pipelines.script2video_pipeline import Script2VideoPipeline
from interfaces import CharacterInScene
from typing import List, Dict, Optional
import asyncio
import json
import yaml
from langchain.chat_models import init_chat_model
from tools.render_backend import RenderBackend
from utils.provider_presets import resolve_chat_model_config
from utils.text import safe_path_component
from utils.video import concatenate_video_files


def _pipeline_print(quiet: bool, message: str) -> None:
    if not quiet:
        print(message)


class Idea2VideoPipeline:
    def __init__(
        self,
        chat_model: str,
        image_generator: str,
        video_generator: str,
        working_dir: str,
        character_bindings: Optional[Dict[str, str]] = None,
        asset_registry=None,
        chinese_instruction: str = "",
        subtitle_service=None,
        voiceover_service=None,
        transition=None,
        hook=None,
        cover=None,
        aigc_label=None,
        consistency_critic=None,
        consistency_max_retries: int = 1,
        domain: str = "",
        image_text_constraint: str = "",
        image_size: str = "1920x1080",
        render_retries: int = 3,
        max_concurrent_video_generations: int = 2,
        image_candidate_count: int = 1,
        video_candidate_count: int = 1,
    ):
        self.image_size = (image_size or "1920x1080").strip()
        self.chat_model = chat_model
        # Suppress spurious foreign/garbled on-screen text in generated frames by
        # wrapping the generators (forwarded already-wrapped to each scene's
        # sub-pipeline, so it isn't applied twice).
        from pipelines.script2video_pipeline import _PromptSuffixGenerator
        self._image_text_constraint = (image_text_constraint or "").strip()
        # Resolve the domain/skill pack up-front so its visual-style (`video`)
        # snippet is appended to every image/video prompt alongside the on-screen
        # text constraint. The wrapped generators are forwarded already-wrapped to
        # each scene's sub-pipeline (isinstance guard there prevents double-wrap).
        from agents.domain_packs import resolve_domain
        self.domain = (domain or "").strip()
        self._domain_pack = resolve_domain(self.domain)
        _prompt_suffix = "\n\n".join(
            s for s in (self._image_text_constraint, (self._domain_pack.video or "").strip()) if s)
        if _prompt_suffix:
            image_generator = _PromptSuffixGenerator(image_generator, _prompt_suffix)
            video_generator = _PromptSuffixGenerator(video_generator, _prompt_suffix)
        self.image_generator = image_generator
        self.video_generator = video_generator
        self.working_dir = working_dir
        os.makedirs(self.working_dir, exist_ok=True)

        # Fixed-character support (optional, fully backward compatible when unset).
        self.character_bindings: Dict[str, str] = character_bindings or {}
        self.asset_registry = asset_registry
        self.chinese_instruction = (chinese_instruction or "").strip()
        # Subtitles are burned per scene by the sub Script2VideoPipeline, then the
        # already-subtitled scene videos are concatenated.
        self.subtitle_service = subtitle_service
        # Voiceover is likewise added per scene by the sub Script2VideoPipeline.
        self.voiceover_service = voiceover_service
        # Transition applies both between shots (sub-pipeline) and between scenes
        # (final concat below).
        self.transition = transition
        # Opening hook (applied to the first scene only) + poster export (on the
        # final concatenated video).
        self.hook = hook or {}
        self.cover = cover or {}
        # Persistent AIGC label applies to every scene (it's a full-duration mark).
        self.aigc_label = aigc_label or {}
        # Character-consistency critic (applied per scene by the sub-pipeline).
        self.consistency_critic = consistency_critic
        self.consistency_max_retries = consistency_max_retries
        self.render_retries = max(1, int(render_retries or 1))
        self.max_concurrent_video_generations = max(1, int(max_concurrent_video_generations or 1))
        self.image_candidate_count = max(1, min(3, int(image_candidate_count or 1)))
        self.video_candidate_count = max(1, min(3, int(video_candidate_count or 1)))
        # Domain/skill pack (短剧/解说/科普 或 用户上传的 skill) was resolved above so its
        # `video` style could feed the prompt suffix. It steers the screenwriter
        # here and is forwarded to each scene's Script2VideoPipeline for storyboard
        # + hook reasoning. Empty -> no-op general pack (upstream behaviour).
        self.screenwriter = Screenwriter(
            chat_model=self.chat_model,
            extra_system_instruction=self._domain_pack.instruction_for("screenwriter", self.chinese_instruction),
        )
        self.character_extractor = CharacterExtractor(
            chat_model=self.chat_model)
        self.character_portraits_generator = CharacterPortraitsGenerator(
            image_generator=self.image_generator)

    @classmethod
    def init_from_config(cls, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        chat_model_args = resolve_chat_model_config(config["chat_model"]["init_args"])
        chat_model = init_chat_model(**chat_model_args)
        backend = RenderBackend.from_config(config)

        from characters import AssetCatalog
        asset_registry = AssetCatalog.from_config(config)

        from prompting import runtime_language_instruction, image_text_constraint
        from pipelines.script2video_pipeline import aspect_to_size
        chinese_instruction = runtime_language_instruction(config)
        img_text = image_text_constraint(config)
        img_size = aspect_to_size((config.get("video") or {}).get("aspect_ratio"))

        from subtitles import SubtitleService
        subtitle_service = SubtitleService.from_config(config)

        from audio import VoiceoverService
        voiceover_service = VoiceoverService.from_config(config)

        video_cfg = config.get("video") or {}
        transition = video_cfg.get("transition")
        hook = video_cfg.get("hook")
        cover = video_cfg.get("cover")
        aigc_label = (config.get("compliance") or {}).get("aigc_label")

        from quality import ConsistencyCritic
        consistency_critic = ConsistencyCritic.from_config(config, chat_model)
        consistency_max_retries = int(((config.get("quality") or {}).get("consistency") or {}).get("max_retries", 1))
        generation = config.get("generation") or {}

        domain = ((config.get("creative") or {}).get("domain") or "")

        return cls(
            chat_model=chat_model,
            image_generator=backend.image_generator,
            video_generator=backend.video_generator,
            working_dir=config["working_dir"],
            asset_registry=asset_registry,
            chinese_instruction=chinese_instruction,
            subtitle_service=subtitle_service,
            voiceover_service=voiceover_service,
            transition=transition,
            hook=hook,
            cover=cover,
            aigc_label=aigc_label,
            consistency_critic=consistency_critic,
            consistency_max_retries=consistency_max_retries,
            render_retries=int(generation.get("render_retries", 3)),
            image_candidate_count=int(generation.get("image_candidates", 1)),
            video_candidate_count=int(generation.get("video_candidates", 1)),
            domain=domain,
            image_text_constraint=img_text,
            image_size=img_size,
        )

    async def extract_characters(
        self,
        story: str,
        quiet: bool = False,
    ):
        save_path = os.path.join(self.working_dir, "characters.json")

        if os.path.exists(save_path):
            with open(save_path, "r", encoding="utf-8") as f:
                characters = json.load(f)
            characters = [CharacterInScene.model_validate(
                character) for character in characters]
            _pipeline_print(quiet, f"🚀 Loaded {len(characters)} characters from existing file.")
        else:
            characters = await self.character_extractor.extract_characters(story)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump([character.model_dump()
                          for character in characters], f, ensure_ascii=False, indent=4)
            _pipeline_print(quiet, f"✅ Extracted {len(characters)} characters from story and saved to {save_path}.")

        return characters

    async def generate_character_portraits(
        self,
        characters: List[CharacterInScene],
        character_portraits_registry: Optional[Dict[str, Dict[str, Dict[str, str]]]],
        style: str,
    ):
        character_portraits_registry_path = os.path.join(
            self.working_dir, "character_portraits_registry.json")
        if character_portraits_registry is None:
            if os.path.exists(character_portraits_registry_path):
                with open(character_portraits_registry_path, 'r', encoding='utf-8') as f:
                    character_portraits_registry = json.load(f)
            else:
                character_portraits_registry = {}

        tasks = [
            self.generate_portraits_for_single_character(character, style)
            for character in characters
            if character.identifier_in_scene not in character_portraits_registry
        ]
        if tasks:
            for future in asyncio.as_completed(tasks):
                character_portraits_registry.update(await future)
                with open(character_portraits_registry_path, 'w', encoding='utf-8') as f:
                    json.dump(character_portraits_registry,
                              f, ensure_ascii=False, indent=4)

            print(
                f"✅ Completed character portrait generation for {len(characters)} characters.")
        else:
            print(
                "🚀 All characters already have portraits, skipping portrait generation.")

        return character_portraits_registry

    async def develop_story(
        self,
        idea: str,
        user_requirement: str,
        quiet: bool = False,
    ):
        save_path = os.path.join(self.working_dir, "story.txt")
        if os.path.exists(save_path):
            with open(save_path, "r", encoding="utf-8") as f:
                story = f.read()
            _pipeline_print(quiet, f"🚀 Loaded story from existing file.")
        else:
            _pipeline_print(quiet, "🧠 Developing story...")
            story = await self.screenwriter.develop_story(idea=idea, user_requirement=user_requirement)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(story)
            _pipeline_print(quiet, f"✅ Developed story and saved to {save_path}.")

        return story

    async def write_script_based_on_story(
        self,
        story: str,
        user_requirement: str,
        quiet: bool = False,
    ):
        save_path = os.path.join(self.working_dir, "script.json")
        if os.path.exists(save_path):
            with open(save_path, "r", encoding="utf-8") as f:
                script = json.load(f)
            _pipeline_print(quiet, f"🚀 Loaded script from existing file.")
        else:
            _pipeline_print(quiet, "🧠 Writing script based on story...")
            script = await self.screenwriter.write_script_based_on_story(story=story, user_requirement=user_requirement)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(script, f, ensure_ascii=False, indent=4)
            _pipeline_print(quiet, f"✅ Written script based on story and saved to {save_path}.")
        return script

    async def generate_portraits_for_single_character(
        self,
        character: CharacterInScene,
        style: str,
    ):
        identifier = character.identifier_in_scene
        character_dir = os.path.join(
            self.working_dir, "character_portraits", f"{character.idx}_{safe_path_component(identifier)}")

        # Fixed character: reuse bound reference images instead of generating.
        from characters import resolve_fixed_asset, build_fixed_registry_entry
        fixed_asset = resolve_fixed_asset(self.asset_registry, self.character_bindings, identifier)
        if fixed_asset is not None:
            print(f"📌 Using fixed character asset '{fixed_asset.asset_id}' for {identifier}, skipping portrait generation.")
            return build_fixed_registry_entry(identifier, fixed_asset, character_dir)

        os.makedirs(character_dir, exist_ok=True)

        front_portrait_path = os.path.join(character_dir, "front.png")
        if os.path.exists(front_portrait_path):
            pass
        else:
            front_portrait_output = await self.character_portraits_generator.generate_front_portrait(character, style)
            front_portrait_output.save(front_portrait_path)

        side_portrait_path = os.path.join(character_dir, "side.png")
        if os.path.exists(side_portrait_path):
            pass
        else:
            side_portrait_output = await self.character_portraits_generator.generate_side_portrait(character, front_portrait_path)
            side_portrait_output.save(side_portrait_path)

        back_portrait_path = os.path.join(character_dir, "back.png")
        if os.path.exists(back_portrait_path):
            pass
        else:
            back_portrait_output = await self.character_portraits_generator.generate_back_portrait(character, front_portrait_path)
            back_portrait_output.save(back_portrait_path)

        print(
            f"☑️ Completed character portrait generation for {character.identifier_in_scene}.")

        return {
            character.identifier_in_scene: {
                "front": {
                    "path": front_portrait_path,
                    "description": f"A front view portrait of {character.identifier_in_scene}.",
                },
                "side": {
                    "path": side_portrait_path,
                    "description": f"A side view portrait of {character.identifier_in_scene}.",
                },
                "back": {
                    "path": back_portrait_path,
                    "description": f"A back view portrait of {character.identifier_in_scene}.",
                },
            }
        }

    async def __call__(
        self,
        idea: str,
        user_requirement: str,
        style: str,
        quiet: bool = False,
        hook_text: str = "",
    ):

        story = await self.develop_story(idea=idea, user_requirement=user_requirement, quiet=quiet)

        if not hook_text and self.hook.get("enabled") and self.hook.get("auto", True):
            try:
                from agents.hook_writer import HookWriter
                hook_text = await HookWriter(self.chat_model).generate(story or idea, chinese=bool(self.chinese_instruction))
            except Exception:
                hook_text = ""

        characters = await self.extract_characters(story=story, quiet=quiet)

        character_portraits_registry = await self.generate_character_portraits(
            characters=characters,
            character_portraits_registry=None,
            style=style,
        )

        scene_scripts = await self.write_script_based_on_story(story=story, user_requirement=user_requirement, quiet=quiet)

        all_video_paths = []

        for idx, scene_script in enumerate(scene_scripts):
            scene_working_dir = os.path.join(self.working_dir, f"scene_{idx}")
            os.makedirs(scene_working_dir, exist_ok=True)
            script2video_pipeline = Script2VideoPipeline(
                chat_model=self.chat_model,
                image_generator=self.image_generator,
                video_generator=self.video_generator,
                working_dir=scene_working_dir,
                chinese_instruction=self.chinese_instruction,
                subtitle_service=self.subtitle_service,
                voiceover_service=self.voiceover_service,
                transition=self.transition,
                hook=(self.hook if idx == 0 else None),  # hook only on the opening scene
                cover=None,                # poster is exported once, on the final concat
                aigc_label=self.aigc_label,  # persistent label on every scene
                consistency_critic=self.consistency_critic,
                consistency_max_retries=self.consistency_max_retries,
                render_retries=self.render_retries,
                max_concurrent_video_generations=self.max_concurrent_video_generations,
                image_candidate_count=self.image_candidate_count,
                video_candidate_count=self.video_candidate_count,
                domain=self.domain,
                image_text_constraint=self._image_text_constraint,
                image_size=self.image_size,
            )
            final_video_path = await script2video_pipeline(
                script=scene_script,
                user_requirement=user_requirement,
                style=style,
                characters=characters,
                character_portraits_registry=character_portraits_registry,
                quiet=quiet,
                hook_text=(hook_text if idx == 0 else ""),
            )
            all_video_paths.append(final_video_path)

        final_video_path = os.path.join(self.working_dir, "final_video.mp4")
        if os.path.exists(final_video_path):
            _pipeline_print(quiet, f"🚀 Skipped concatenating videos, already exists.")
        else:
            _pipeline_print(quiet, f"🎬 Starting concatenating videos...")
            concatenate_video_files(all_video_paths, final_video_path, transition=self.transition)
            _pipeline_print(quiet, f"☑️ Concatenated videos, saved to {final_video_path}.")

        if self.cover.get("enabled"):
            try:
                from video import export_poster
                out = os.path.join(self.working_dir, self.cover.get("filename", "poster.jpg"))
                poster = export_poster(final_video_path, out, at_seconds=float(self.cover.get("at", 0.0)))
                if poster:
                    _pipeline_print(quiet, f"🖼️ Poster exported -> {poster}.")
            except Exception:
                pass
        return final_video_path
