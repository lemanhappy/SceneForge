import os
import shutil
import json
import logging
import asyncio
import time
import re
from typing import Any, Callable, Optional, Dict, List, Tuple, Literal
from PIL import Image
from agents import *
from agents.reference_image_selector import (
    reusable_asset_kind,
    reusable_asset_reference_instruction,
)
from agents.best_image_selector import BestImageSelector
import yaml
from interfaces import *
from langchain.chat_models import init_chat_model
from tools.render_backend import RenderBackend
from tools.video_capabilities import plan_video_duration, storyboard_duration_instruction
from prompting import compile_video_prompt
from quality.prompt_preflight import (
    camera_state_for_shot,
    preflight_shot,
    preflight_storyboard,
    save_prompt_preflight_report,
)
from utils.atomic import atomic_write_text
from utils.provider_presets import resolve_chat_model_config
from utils.text import safe_path_component
from utils.video import concatenate_video_files


def _pipeline_print(quiet: bool, message: str) -> None:
    if not quiet:
        print(message)


def _emit_text_plan_progress(progress, stage: str, message: str, metadata: Dict[str, Any] | None = None) -> None:
    if progress is not None:
        progress(stage, message, metadata or {})


def _emit_render_progress(progress, stage: str, message: str, metadata: Dict[str, Any] | None = None) -> None:
    if progress is not None:
        progress(stage, message, metadata or {})


def _scoped_progress(progress, **scope):
    if progress is None:
        return None

    def emit(stage: str, message: str, metadata: Dict[str, Any] | None = None) -> None:
        payload = dict(scope)
        payload.update(metadata or {})
        _emit_render_progress(progress, stage, message, payload)

    return emit


# Aspect ratio -> generated-frame size. The video model derives its output aspect
# from the conditioning frames, so the frame size is the single lever. Sizes keep
# each dimension within the image model's valid range (Seedream: 1024–4096).
_ASPECT_SIZES = {
    "landscape": "1920x1080",  # 16:9 横屏
    "16:9": "1920x1080",
    "portrait": "1080x1920",   # 9:16 竖屏（抖音/快手/视频号）
    "9:16": "1080x1920",
    "square": "1440x1440",     # 1:1 方形
    "1:1": "1440x1440",
}


def aspect_to_size(aspect: str) -> str:
    return _ASPECT_SIZES.get(str(aspect or "").strip().lower(), "1920x1080")


def size_to_aspect_ratio(size: str) -> str:
    """Map the generated keyframe canvas to the provider's aspect argument."""
    try:
        width, height = (int(part) for part in str(size).lower().split("x", 1))
    except (TypeError, ValueError):
        return "16:9"
    if width == height:
        return "1:1"
    return "16:9" if width > height else "9:16"


def _frame_target_description(shot_description, frame_type: str) -> str:
    """Compile a keyframe target without losing whole-shot prop state.

    Decomposition models sometimes put a static prop only in ``visual_desc`` or
    ``motion_desc`` and omit it from ``ff_desc``. Feeding only the frame field
    lets that prop pop into existence later. Keep the frame-specific pose first,
    then attach the full-shot contract to every generated keyframe and critic.
    """
    field = "lf_desc" if frame_type == "last_frame" else "ff_desc"
    primary = str(getattr(shot_description, field, "") or "").strip()
    visual = str(getattr(shot_description, "visual_desc", "") or "").strip()
    motion = str(getattr(shot_description, "motion_desc", "") or "").strip()
    parts = [primary]
    if visual and visual not in primary:
        parts.append("[Whole-shot visual contract]\n" + visual)
    if motion and motion not in primary:
        parts.append("[Whole-shot action contract]\n" + motion)
    parts.append(
        "[Object-state rule] Every static scene element and reusable prop described "
        "for this shot must already exist in the first frame and remain present until "
        "an explicitly depicted action moves, carries, occludes, or removes it. Never "
        "make an object pop in, disappear, duplicate, change support surface, or change "
        "appearance without that action."
    )
    return "\n\n".join(part for part in parts if part)


def _is_reusable_image(path: str) -> bool:
    """Return true only for a complete, decodable generated image.

    A worker can be stopped while a provider download is writing the target. An
    existence-only resume check would then treat the partial file as complete.
    Quarantine the broken frame and its cached selector output so resume rebuilds
    both with the current prompt rules while preserving the interrupted bytes for
    diagnostics.
    """
    if not os.path.exists(path):
        return False
    try:
        with Image.open(path) as image:
            image.verify()
            if image.width <= 0 or image.height <= 0:
                raise ValueError("image has invalid dimensions")
        return True
    except (OSError, ValueError, SyntaxError) as exc:
        suffix = f".invalid-{int(time.time() * 1000)}"
        quarantined = path + suffix
        os.replace(path, quarantined)
        selector = os.path.splitext(path)[0] + "_selector_output.json"
        if os.path.exists(selector):
            os.replace(selector, selector + suffix)
        logging.warning(
            "Quarantined incomplete generated frame %s as %s: %s",
            path,
            quarantined,
            exc,
        )
        return False


def _camera_is_locked(shot_description) -> bool:
    return camera_state_for_shot(shot_description).mode == "locked"


def _requires_last_frame(shot_description) -> bool:
    """Use a second endpoint only for a genuine large, moving-camera transition.

    Independent last-frame images are rarely pixel-aligned. Feeding one to a
    locked or ordinary performance shot makes the video model slide furniture,
    resize props, and morph architecture merely to connect the endpoints.
    """
    variation = str(getattr(shot_description, "variation_type", "small") or "small").lower()
    return variation == "large" and not _camera_is_locked(shot_description)


def _video_stability_constraints(shot_description, *, use_last_frame: bool) -> str:
    visible_count = len(set(getattr(shot_description, "ff_vis_char_idxs", None) or []))
    count_rule = (
        f"There are exactly {visible_count} visible character instance(s) at time zero. "
        if visible_count
        else "Do not introduce a person unless the timed action explicitly requires one. "
    )
    parts = [
        "[Reference-frame continuity rules]",
        "The first reference image is the authoritative time-zero state. "
        "Continue motion from the positions already visible in that image; never restart the action "
        "by spawning a second copy of an existing subject.",
        count_rule
        + "Never clone, duplicate, reintroduce, double-expose, or create an afterimage/ghost trail "
        "of a character. If the action says enter, arrive, or walk in, animate the single existing "
        "character continuing inward from the exact threshold/edge position already shown.",
        "All walls, doors, windows, clocks, lights, counters, fixed furniture, and every prop not "
        "explicitly moved by the scripted action must remain rigid, stationary, and identical in "
        "shape, scale, color, and screen position. No background breathing, object sliding, resizing, "
        "melting, or texture morphing.",
    ]
    if _camera_is_locked(shot_description):
        parts.append(
            "The camera is locked to a tripod: preserve the exact framing and perspective with no "
            "pan, tilt, zoom, dolly, reframing, shake, or parallax. Animate only the explicitly named "
            "subject motion, rain/reflections, and permitted focus change."
        )
    elif use_last_frame:
        parts.append(
            "Both endpoint images depict the same physical world. Interpolate only the intended "
            "camera/subject change; fixed scene geometry and unmoved props must not morph between them."
        )
    parts.append("[/Reference-frame continuity rules]")
    return "\n".join(parts)


def _compile_reference_aware_video_prompt(shot_description) -> Tuple[str, bool]:
    """Compile a shot only after provider-neutral semantic preflight."""
    preflight = preflight_shot(shot_description)
    issue_codes = {issue.code for issue in preflight.issues if issue.auto_fixed}
    entry_conflict = "actor_already_visible_before_entry" in issue_codes
    pickup_conflict = "prop_already_held_before_pickup" in issue_codes
    if not preflight.rewritten:
        return compile_video_prompt(shot_description), False

    continuity_parts = []
    if entry_conflict:
        visible_count = len(set(getattr(shot_description, "ff_vis_char_idxs", None) or []))
        continuity_parts.append(
            f"At time zero, the reference image already contains exactly {visible_count} visible "
            "character body/bodies. Animate only those same existing bodies from their exact "
            "reference positions. The foreground character takes at most one small, slow step "
            "toward the room interior and then stops. Character count stays constant in every frame."
        )
    remaining_motion = preflight.normalized_motion_desc
    if pickup_conflict:
        continuity_parts.append(
            " Any prop already held at time zero remains the same single prop. Continue carrying "
            "it from its current position; do not repeat an already-completed pickup."
        )
    motion_desc = " ".join([*continuity_parts, remaining_motion]).strip()

    prompt_input = preflight.prompt_input(shot_description)
    prompt_input["motion_desc"] = motion_desc
    return compile_video_prompt(prompt_input), True


def _quality_target_description(shot_description) -> str:
    """Judge rendered motion against the same contract used by the I2V model."""
    prompt, _rewritten = _compile_reference_aware_video_prompt(shot_description)
    return prompt


def _continuity_target_text(target: Any) -> str:
    if isinstance(target, str):
        return target
    if isinstance(target, dict):
        value = target.get
    else:
        value = lambda name, default="": getattr(target, name, default)
    parts = [
        value(name, "")
        for name in (
            "ff_desc", "lf_desc", "visual_desc", "motion_desc", "director_desc",
            "audio_desc", "screen_text", "variation_reason",
        )
    ]
    for beat in value("beats", []) or []:
        if isinstance(beat, dict):
            parts.extend(beat.get(name, "") for name in ("camera", "action", "performance"))
        else:
            parts.extend(getattr(beat, name, "") for name in ("camera", "action", "performance"))
    return " ".join(str(part or "") for part in parts)


def _reusable_asset_matches_text(asset: Any, normalized_text: str) -> bool:
    candidates = [
        getattr(asset, "asset_id", ""),
        getattr(asset, "display_name", ""),
        *(getattr(asset, "aliases", None) or []),
    ]
    return any(
        str(candidate).strip().casefold() in normalized_text
        for candidate in candidates
        if len(str(candidate).strip()) >= 2
    )


def _prepare_frame_references(
    selected_pairs: List[Tuple[str, str]],
    reusable_pairs: List[Tuple[str, str]],
    prompt: str,
    image_size: str,
    limit: int = 8,
) -> Tuple[List[Tuple[str, str]], str]:
    """Pin bound reusable assets and lead with a target-aspect reference.

    Reference selectors can omit an explicitly bound prop or scene. Some image
    providers also inherit the canvas orientation from the first reference even
    when ``size`` requests another aspect ratio. Keep the selector's ordering
    semantics in the prompt, append bound assets, then move the closest-aspect
    image to the front and remap every ``Image N`` reference.
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for pair in [*selected_pairs, *reusable_pairs]:
        key = str(pair[0])
        if key in seen:
            continue
        seen.add(key)
        pairs.append(pair)
        if len(pairs) >= max(1, int(limit)):
            break

    original_pairs = list(pairs)
    try:
        width, height = (int(part) for part in str(image_size).lower().split("x", 1))
        target_ratio = width / height
    except (TypeError, ValueError, ZeroDivisionError):
        width, height, target_ratio = 1920, 1080, 16 / 9

    best_index = None
    best_error = float("inf")
    for index, (path, _) in enumerate(pairs):
        try:
            with Image.open(path) as reference:
                ratio = reference.width / reference.height
        except (OSError, ValueError, ZeroDivisionError):
            continue
        error = abs(ratio - target_ratio) / target_ratio
        if error < best_error:
            best_index, best_error = index, error

    if best_index not in (None, 0) and best_error <= 0.12:
        pairs = [pairs[best_index], *pairs[:best_index], *pairs[best_index + 1:]]
        new_indices = {str(pair[0]): index for index, pair in enumerate(pairs)}
        old_to_new = {
            index: new_indices[str(pair[0])]
            for index, pair in enumerate(original_pairs)
        }
        prompt = re.sub(
            r"\bImage\s+(\d+)\b",
            lambda match: f"Image {old_to_new.get(int(match.group(1)), int(match.group(1)))}",
            str(prompt or ""),
        )

    reusable_paths = {str(pair[0]) for pair in reusable_pairs}
    pinned_lines = []
    for index, pair in enumerate(pairs):
        if str(pair[0]) in reusable_paths:
            pinned_lines.append(reusable_asset_reference_instruction(index, pair[1]))

    orientation = "landscape" if width > height else "portrait" if height > width else "square"
    aspect_line = (
        f"Compose the output on a strict {width}:{height} {orientation} canvas. "
        "Do not inherit a different canvas orientation from portrait or square reference images."
    )
    additions = "\n".join([*pinned_lines, aspect_line])
    return pairs, f"{str(prompt or '').strip()}\n\n{additions}".strip()


def _has_bound_scene_reference(pairs: List[Tuple[str, str]]) -> bool:
    return any(reusable_asset_kind(description) == "scene" for _path, description in pairs)


class _PromptSuffixGenerator:
    """Wraps an image/video generator, appending a fixed instruction to every
    prompt. Used to keep spurious foreign-language text out of generated frames.
    Delegates everything else to the wrapped generator unchanged."""

    def __init__(self, inner, suffix: str):
        self._inner = inner
        self._suffix = suffix

    def __getattr__(self, name):  # delegate any other attribute/method
        return getattr(self._inner, name)

    def _aug(self, prompt):
        if not prompt:
            return self._suffix
        return f"{prompt}\n\n{self._suffix}"

    async def generate_single_image(self, prompt, *args, **kwargs):
        return await self._inner.generate_single_image(self._aug(prompt), *args, **kwargs)

    async def generate_single_video(self, prompt, *args, **kwargs):
        return await self._inner.generate_single_video(self._aug(prompt), *args, **kwargs)


class Script2VideoPipeline:

    def __init__(
        self,
        chat_model: str,
        image_generator,
        video_generator,
        working_dir: str,
        character_bindings: Optional[Dict[str, str]] = None,
        asset_registry=None,
        subtitle_service=None,
        voiceover_service=None,
        transition=None,
        hook=None,
        cover=None,
        aigc_label=None,
        render_retries: int = 3,
        consistency_critic=None,
        consistency_max_retries: int = 1,
        chinese_instruction: str = "",
        domain: str = "",
        image_text_constraint: str = "",
        image_size: str = "1920x1080",
        global_reference_images: Optional[List[Tuple[str, str]]] = None,
        reusable_assets: Optional[List[Any]] = None,
        inherited_continuity_ledger: Optional[dict] = None,
        continuity_inheritance_source: Optional[dict] = None,
        max_concurrent_video_generations: int = 2,
        image_candidate_count: int = 1,
        video_candidate_count: int = 1,
        generation_context: Optional[Dict[str, Any]] = None,
    ):
        # Per-instance coordination events; these were once class attributes,
        # which leaked shot/frame state across scenes when one pipeline object
        # (or several) rendered more than one scene.
        self.character_portrait_events = {}
        self.shot_desc_events = {}
        self.frame_events = {}

        self.chat_model = chat_model
        # Optionally constrain on-screen text in generated frames (suppress spurious
        # foreign/garbled text; Chinese-only if any). Wrapping the generators means
        # every image/video prompt — incl. character portraits & transitions — gets
        # the constraint, without touching each call site.
        self._image_text_constraint = (image_text_constraint or "").strip()
        # Domain/skill visual style (the active pack's `video` snippet) is appended
        # to every image/video prompt alongside the on-screen-text constraint.
        # Resolve the pack here (before wrapping) so the suffix can include it.
        from agents.domain_packs import resolve_domain
        self.domain = (domain or "").strip()
        self._domain_pack = resolve_domain(self.domain)
        _prompt_suffix = "\n\n".join(
            s for s in (self._image_text_constraint, (self._domain_pack.video or "").strip()) if s)
        if _prompt_suffix:
            # Idempotent: a caller (e.g. Idea2VideoPipeline) may pass generators it
            # already wrapped — don't append the suffix twice.
            if not isinstance(image_generator, _PromptSuffixGenerator):
                image_generator = _PromptSuffixGenerator(image_generator, _prompt_suffix)
            if not isinstance(video_generator, _PromptSuffixGenerator):
                video_generator = _PromptSuffixGenerator(video_generator, _prompt_suffix)
        # When the picture is kept text-free (policy "none"), composite each shot's
        # essential `screen_text` in post with a real font instead. Only in that
        # mode, to avoid doubling text the image model already drew under other policies.
        from prompting import IMAGE_TEXT_NONE
        self.screen_text_overlay = (self._image_text_constraint == IMAGE_TEXT_NONE)
        self.image_generator = image_generator
        self.video_generator = video_generator
        # Generated-frame size -> aspect ratio (横屏/竖屏/方形). The video model
        # inherits aspect from the conditioning frames, so this one size governs it.
        self.image_size = (image_size or "1920x1080").strip()
        self.video_aspect_ratio = size_to_aspect_ratio(self.image_size)

        # Fixed-character support (optional, fully backward compatible when unset):
        # character_bindings maps a scene character identifier -> fixed asset_id,
        # resolved against asset_registry (a CharacterAssetRegistry).
        self.character_bindings: Dict[str, str] = character_bindings or {}
        self.asset_registry = asset_registry
        self.global_reference_images = [
            (str(path), str(description))
            for path, description in (global_reference_images or [])
            if path and os.path.isfile(path)
        ]
        self.reusable_assets = list(reusable_assets or [])
        self.inherited_continuity_ledger = dict(inherited_continuity_ledger or {})
        self.continuity_inheritance_source = dict(continuity_inheritance_source or {})
        self._continuity_characters = []
        # Optional SubtitleService; when set, __call__ burns subtitles after concat.
        self.subtitle_service = subtitle_service
        # Optional VoiceoverService; when set, __call__ synthesizes TTS voiceover
        # and muxes it onto the concatenated video (before subtitle burn-in).
        self.voiceover_service = voiceover_service
        # Optional transition between shots (e.g. {"type": "crossfade", "duration": 0.5});
        # None/"none" keeps the original hard-cut concatenation.
        self.transition = transition
        # Optional opening hook overlay (video.hook) and poster export (video.cover).
        self.hook = hook or {}
        self.cover = cover or {}
        # Transient-failure retry count for the video generation step.
        self.render_retries = max(1, int(render_retries or 1))
        # Limit active remote renders so a multi-shot scene does not flood one
        # provider group and trigger avoidable 429 responses.
        self.max_concurrent_video_generations = max(
            1, int(max_concurrent_video_generations or 1)
        )
        self.image_candidate_count = max(1, min(3, int(image_candidate_count or 1)))
        self.video_candidate_count = max(1, min(3, int(video_candidate_count or 1)))
        # Public, secret-free route metadata used by the production telemetry
        # ledger. Cost values remain estimates unless a provider explicitly
        # reports an actual charge during generation.
        self.generation_context = dict(generation_context or {})
        self._video_generation_semaphore = asyncio.Semaphore(
            self.max_concurrent_video_generations
        )
        # Optional character-consistency critic + auto-regeneration loop.
        self.consistency_critic = consistency_critic
        self.consistency_max_retries = max(0, int(consistency_max_retries or 0))
        # Per-shot corrective instruction injected into the next re-render of that
        # shot's first frame (directed regeneration: the critic's failure reason is
        # fed back so a rejected shot is fixed, not blindly re-rolled).
        self._shot_corrections: Dict[int, str] = {}
        # Optional persistent AIGC compliance label/watermark (compliance.aigc_label).
        self.aigc_label = aigc_label or {}

        self.chinese_instruction = (chinese_instruction or "").strip()
        # Domain/skill pack (短剧/解说/科普 或 用户上传的 skill) was resolved above so its
        # `video` style could feed the prompt suffix. Its storyboard snippet steers
        # shot design; its hook snippet is passed to the HookWriter (_auto_hook).
        self.character_extractor = CharacterExtractor(chat_model=self.chat_model)
        self.character_portraits_generator = CharacterPortraitsGenerator(image_generator=self.image_generator)
        _storyboard_instruction = "\n\n".join(filter(None, (
            self._domain_pack.instruction_for("storyboard", self.chinese_instruction),
            storyboard_duration_instruction(self.video_generator),
        )))
        self.storyboard_artist = StoryboardArtist(
            chat_model=self.chat_model,
            extra_system_instruction=_storyboard_instruction,
        )
        self.camera_image_generator = CameraImageGenerator(chat_model=self.chat_model, image_generator=self.image_generator, video_generator=self.video_generator, image_size=self.image_size)
        self.reference_image_selector = ReferenceImageSelector(chat_model=self.chat_model)
        self.best_image_selector = BestImageSelector(chat_model=self.chat_model)

        self.working_dir = working_dir
        os.makedirs(self.working_dir, exist_ok=True)


    async def plan_text_artifacts(
        self,
        script: str,
        user_requirement: str,
        style: str,
        characters: List[CharacterInScene] = None,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
        quiet: bool = False,
    ):
        """Generate only structured text artifacts required before rendering.

        This helper intentionally stops before character portraits, frame generation,
        video generation, and final concatenation so an agent loop can pause for
        user review after narrative planning.
        """
        self.character_portrait_events = {}
        self.shot_desc_events = {}
        self.frame_events = {}

        if characters is None:
            _emit_text_plan_progress(progress, "extract_characters", "Extracting characters from script")
            characters = await self.extract_characters(script=script, quiet=quiet)
        else:
            _emit_text_plan_progress(progress, "extract_characters", "Using provided characters", {"provided": True, "count": len(characters)})
            characters_path = os.path.join(self.working_dir, "characters.json")
            if not os.path.exists(characters_path):
                with open(characters_path, "w", encoding="utf-8") as f:
                    json.dump([character.model_dump() for character in characters], f, ensure_ascii=False, indent=4)
            for character in characters:
                self.character_portrait_events[character.idx] = asyncio.Event()

        _emit_text_plan_progress(progress, "design_storyboard", "Designing storyboard")
        storyboard = await self.design_storyboard(
            script=script,
            characters=characters,
            user_requirement=user_requirement,
            quiet=quiet,
        )
        _emit_text_plan_progress(progress, "decompose_shots", "Decomposing shot visual descriptions", {"shot_count": len(storyboard)})
        shot_descriptions = await self.decompose_visual_descriptions(
            shot_brief_descriptions=storyboard,
            characters=characters,
            quiet=quiet,
        )
        self._continuity_characters = list(characters or [])
        preflight_report = self._write_prompt_preflight(shot_descriptions)
        _emit_text_plan_progress(
            progress,
            "prompt_preflight",
            "Checking shot state and prompt consistency",
            preflight_report.get("summary", {}),
        )
        _emit_text_plan_progress(progress, "construct_camera_tree", "Constructing camera tree", {"shot_count": len(shot_descriptions)})
        camera_tree = await self.construct_camera_tree(
            shot_descriptions=shot_descriptions,
            quiet=quiet,
        )
        self._write_continuity_contracts(camera_tree, shot_descriptions)
        return {
            "characters": characters,
            "storyboard": storyboard,
            "shot_descriptions": shot_descriptions,
            "camera_tree": camera_tree,
            "prompt_preflight": preflight_report,
        }


    @classmethod
    def init_from_config(cls, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        chat_model_args = resolve_chat_model_config(config["chat_model"]["init_args"])
        chat_model = init_chat_model(**chat_model_args)
        backend = RenderBackend.from_config(config)

        from characters import AssetCatalog
        asset_registry = AssetCatalog.from_config(config)

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
        image_candidate_count = int((config.get("generation") or {}).get("image_candidates", 1))
        video_candidate_count = int((config.get("generation") or {}).get("video_candidates", 1))

        from prompting import runtime_language_instruction, image_text_constraint
        chinese_instruction = runtime_language_instruction(config)
        img_text = image_text_constraint(config)
        img_size = aspect_to_size((config.get("video") or {}).get("aspect_ratio"))

        domain = ((config.get("creative") or {}).get("domain") or "")

        return cls(
            chat_model=chat_model,
            image_generator=backend.image_generator,
            video_generator=backend.video_generator,
            working_dir=config["working_dir"],
            asset_registry=asset_registry,
            subtitle_service=subtitle_service,
            voiceover_service=voiceover_service,
            transition=transition,
            hook=hook,
            cover=cover,
            aigc_label=aigc_label,
            consistency_critic=consistency_critic,
            consistency_max_retries=consistency_max_retries,
            image_candidate_count=image_candidate_count,
            video_candidate_count=video_candidate_count,
            chinese_instruction=chinese_instruction,
            domain=domain,
            image_text_constraint=img_text,
            image_size=img_size,
        )

    async def generate_keyframes(
        self,
        script: str,
        user_requirement: str,
        style: str,
        characters: List[CharacterInScene] = None,
        character_portraits_registry: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
        quiet: bool = False,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
        shot_indexes: Optional[List[int]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Render reusable shot keyframes without submitting full video jobs."""
        self.character_portrait_events = {}
        self.shot_desc_events = {}
        self.frame_events = {}
        if characters is None:
            characters = await self.extract_characters(script=script, quiet=quiet)
        else:
            for character in characters:
                self.character_portrait_events[character.idx] = asyncio.Event()

        if character_portraits_registry is None:
            character_portraits_registry = await self.generate_character_portraits(
                characters=characters,
                character_portraits_registry=None,
                style=style,
                progress=progress,
            )

        storyboard = await self.design_storyboard(
            script=script,
            characters=characters,
            user_requirement=user_requirement,
            quiet=quiet,
        )
        shot_descriptions = await self.decompose_visual_descriptions(
            shot_brief_descriptions=storyboard,
            characters=characters,
            quiet=quiet,
        )
        self._continuity_characters = list(characters or [])
        self._write_prompt_preflight(shot_descriptions)
        camera_tree = await self.construct_camera_tree(
            shot_descriptions=shot_descriptions,
            quiet=quiet,
        )
        self._write_continuity_contracts(camera_tree, shot_descriptions)
        requested_shots = None
        frame_backups = []
        if shot_indexes is not None:
            requested_shots = sorted({int(index) for index in shot_indexes})
            known_shots = {int(shot.idx) for shot in shot_descriptions}
            missing = [index for index in requested_shots if index not in known_shots]
            if missing:
                raise ValueError(f"unknown storyboard shot indexes: {missing}")
            if force:
                for shot_index in requested_shots:
                    shot_dir = os.path.join(self.working_dir, "shots", str(shot_index))
                    for name in ("first_frame.png", "last_frame.png"):
                        path = os.path.join(shot_dir, name)
                        if os.path.isfile(path):
                            backup = path + ".before-regenerate"
                            if os.path.isfile(backup):
                                os.unlink(backup)
                            os.replace(path, backup)
                            frame_backups.append((path, backup))
        priority_shot_idxs = _collect_priority_shot_idxs(camera_tree)
        _emit_render_progress(
            progress,
            "keyframes_start",
            "Generating keyframe preview",
            {"camera_count": len(camera_tree), "shot_count": len(shot_descriptions)},
        )
        try:
            await self._generate_preview_camera_frames(
                camera_tree=camera_tree,
                shot_descriptions=shot_descriptions,
                characters=characters,
                character_portraits_registry=character_portraits_registry,
                priority_shot_idxs=priority_shot_idxs,
                progress=progress,
                target_shot_idxs=requested_shots,
            )
        except Exception:
            for live_path, backup_path in frame_backups:
                if os.path.isfile(backup_path):
                    if os.path.isfile(live_path):
                        os.unlink(live_path)
                    os.replace(backup_path, live_path)
            raise
        else:
            for _live_path, backup_path in frame_backups:
                if os.path.isfile(backup_path):
                    os.unlink(backup_path)
        _emit_render_progress(
            progress,
            "keyframes_done",
            "Keyframe preview ready",
            {"shot_count": len(requested_shots) if requested_shots is not None else len(shot_descriptions)},
        )
        return {
            "shot_count": len(requested_shots) if requested_shots is not None else len(shot_descriptions),
            "total_shot_count": len(shot_descriptions),
            "camera_count": len(camera_tree),
            "character_portraits_registry": character_portraits_registry,
        }

    async def _generate_preview_camera_frames(
        self,
        camera_tree,
        shot_descriptions,
        characters,
        character_portraits_registry,
        priority_shot_idxs,
        progress=None,
        target_shot_idxs=None,
    ) -> None:
        """Render image-only camera previews in dependency layers.

        Child cameras use the parent shot as a same-world reference.  This keeps
        preview generation inexpensive while preserving multi-camera spatial
        continuity instead of independently inventing every camera background.
        """
        shots_by_idx = {int(shot.idx): shot for shot in shot_descriptions}
        has_bound_scene = _has_bound_scene_reference(self.global_reference_images)
        if target_shot_idxs is not None:
            for shot_idx in target_shot_idxs:
                camera = next(
                    (item for item in camera_tree if int(shot_idx) in [int(value) for value in item.active_shot_idxs]),
                    None,
                )
                if camera is None:
                    raise ValueError(f"camera not found for storyboard shot {shot_idx}")
                original_anchor = int(camera.active_shot_idxs[0])
                reference_idx = original_anchor if original_anchor != int(shot_idx) else camera.parent_shot_idx
                world_reference_pair = None
                if not has_bound_scene and reference_idx is not None:
                    reference_idx = int(reference_idx)
                    reference_path = os.path.join(
                        self.working_dir, "shots", str(reference_idx), "first_frame.png")
                    reference_shot = shots_by_idx.get(reference_idx)
                    if os.path.isfile(reference_path) and reference_shot is not None:
                        world_reference_pair = (
                            reference_path,
                            _frame_target_description(reference_shot, "first_frame"),
                        )
                preview_camera = camera.model_copy(update={
                    "active_shot_idxs": [int(shot_idx)],
                    "parent_shot_idx": None,
                })
                await self.generate_frames_for_single_camera(
                    camera=preview_camera,
                    shot_descriptions=shot_descriptions,
                    characters=characters,
                    character_portraits_registry=character_portraits_registry,
                    priority_shot_idxs=priority_shot_idxs,
                    progress=progress,
                    world_reference_pair=world_reference_pair,
                    first_frames_only=True,
                )
            return

        pending = list(camera_tree)
        completed_shots = set()
        while pending:
            ready = [
                camera for camera in pending
                if camera.parent_shot_idx is None or camera.parent_shot_idx in completed_shots
            ]
            if not ready:
                # Malformed/cyclic imported camera trees fail open: render one
                # camera without a parent reference rather than stalling forever.
                ready = [pending[0]]

            tasks = []
            for camera in ready:
                world_reference_pair = None
                if (
                    not has_bound_scene
                    and camera.parent_shot_idx is not None
                    and camera.parent_shot_idx in completed_shots
                ):
                    parent_idx = int(camera.parent_shot_idx)
                    parent_shot = shots_by_idx.get(parent_idx)
                    world_reference_pair = (
                        os.path.join(self.working_dir, "shots", str(parent_idx), "first_frame.png"),
                        _frame_target_description(parent_shot, "first_frame"),
                    )
                preview_camera = camera.model_copy(update={"parent_shot_idx": None})
                tasks.append(self.generate_frames_for_single_camera(
                    camera=preview_camera,
                    shot_descriptions=shot_descriptions,
                    characters=characters,
                    character_portraits_registry=character_portraits_registry,
                    priority_shot_idxs=priority_shot_idxs,
                    progress=progress,
                    world_reference_pair=world_reference_pair,
                    first_frames_only=True,
                ))
            await asyncio.gather(*tasks)
            for camera in ready:
                completed_shots.update(int(idx) for idx in camera.active_shot_idxs)
                pending.remove(camera)

    async def __call__(
        self,
        script: str,
        user_requirement: str,
        style: str,
        characters: List[CharacterInScene] = None,
        character_portraits_registry: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
        quiet: bool = False,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
        hook_text: str = "",
    ):
        self._hook_text = (hook_text or "").strip()
        _emit_render_progress(progress, "render_start", "Starting script2video render")
        if not self._hook_text and self.hook.get("enabled") and self.hook.get("auto", True):
            self._hook_text = await self._auto_hook(script)
            if self._hook_text:
                cands = getattr(self, "_hook_candidates", []) or []
                msg = f"开场钩子已选定：{self._hook_text}" + (f"（{len(cands)} 选 1）" if len(cands) > 1 else "")
                _emit_render_progress(progress, "hook_generated", msg,
                                      {"hook": self._hook_text, "candidates": cands})
        self.character_portrait_events = {}
        self.shot_desc_events = {}
        self.frame_events = {}
        if characters is None:
            _emit_render_progress(progress, "extract_characters", "Extracting characters before render")
            characters = await self.extract_characters(script=script, quiet=quiet)

            # characters_path = os.path.join(self.working_dir, "characters.json")
            # if os.path.exists(characters_path):
            #     with open(characters_path, "r", encoding="utf-8") as f:
            #         characters = [CharacterInScene.model_validate(c) for c in json.load(f)]
            #     print(f"🚀 Loaded {len(characters)} characters from existing file.")
            # else:
            #     print(f"🔍 Extracting characters from script...")
            #     characters = await self.extract_characters(script=script)
            #     with open(characters_path, "w", encoding="utf-8") as f:
            #         json.dump([c.model_dump() for c in characters], f, ensure_ascii=False, indent=4)
            #     print(f"☑️ Extracted {len(characters)} characters from script and saved to {characters_path}.")
        else:
            _emit_render_progress(progress, "extract_characters", "Using provided characters for render", {"provided": True, "count": len(characters)})
            for character in characters:
                self.character_portrait_events[character.idx] = asyncio.Event()

        self._active_characters = list(characters or [])

        if character_portraits_registry is None:
            character_portraits_registry_path = os.path.join(self.working_dir, "character_portraits_registry.json")
            if os.path.exists(character_portraits_registry_path):
                with open(character_portraits_registry_path, "r", encoding="utf-8") as f:
                    character_portraits_registry = json.load(f)
                print(f"🚀 Loaded {len(character_portraits_registry)} character portraits from existing file.")
                _emit_render_progress(progress, "character_portraits_loaded", "Loaded existing character portraits", {"count": len(character_portraits_registry)})
            else:
                print(f"🔍 Generating character portraits...")
                _emit_render_progress(progress, "character_portraits_start", "Generating character portraits", {"character_count": len(characters)})
                character_portraits_registry = await self.generate_character_portraits(
                    characters=characters,
                    character_portraits_registry=None,
                    style=style,
                    progress=progress,
                )

                with open(character_portraits_registry_path, "w", encoding="utf-8") as f:
                    json.dump(character_portraits_registry, f, ensure_ascii=False, indent=4)
                print(f"☑️ Generated {len(character_portraits_registry)} character portraits and saved to {character_portraits_registry_path}.")
                _emit_render_progress(progress, "character_portraits_done", "Character portraits ready", {"count": len(character_portraits_registry)})



        # design shots
        _emit_render_progress(progress, "load_storyboard", "Loading or designing storyboard")
        storyboard = await self.design_storyboard(
            script=script,
            characters=characters,
            user_requirement=user_requirement,
            quiet=quiet,
        )
        _emit_render_progress(progress, "storyboard_ready", "Storyboard ready", {"shot_count": len(storyboard)})

        # decompose visual descriptions of shots
        _emit_render_progress(progress, "load_shot_descriptions", "Loading or decomposing shot descriptions", {"shot_count": len(storyboard)})
        shot_descriptions = await self.decompose_visual_descriptions(
            shot_brief_descriptions=storyboard,
            characters=characters,
            quiet=quiet,
        )
        self._continuity_characters = list(characters or [])
        preflight_report = self._write_prompt_preflight(shot_descriptions)
        _emit_render_progress(
            progress,
            "prompt_preflight",
            "Shot state and prompt consistency checked",
            preflight_report.get("summary", {}),
        )
        _emit_render_progress(progress, "shot_descriptions_ready", "Shot descriptions ready", {"shot_count": len(shot_descriptions)})

        # construct camera tree
        _emit_render_progress(progress, "load_camera_tree", "Loading or constructing camera tree", {"shot_count": len(shot_descriptions)})
        camera_tree = await self.construct_camera_tree(
            shot_descriptions=shot_descriptions,
            quiet=quiet,
        )
        self._write_continuity_contracts(camera_tree, shot_descriptions)
        _emit_render_progress(progress, "camera_tree_ready", "Camera tree ready", {"camera_count": len(camera_tree)})

        priority_shot_idxs = _collect_priority_shot_idxs(camera_tree)
        _emit_render_progress(progress, "frames_start", "Generating frames for cameras", {"camera_count": len(camera_tree), "shot_count": len(shot_descriptions)})
        tasks = [
            self.generate_frames_for_single_camera(
                camera=camera,
                shot_descriptions=shot_descriptions,
                characters=characters,
                character_portraits_registry=character_portraits_registry,
                priority_shot_idxs=priority_shot_idxs,
                progress=progress,
            )
            for camera in camera_tree
        ]

        _emit_render_progress(progress, "video_clips_start", "Generating video clips for shots", {"shot_count": len(shot_descriptions)})
        video_tasks = [
            self.generate_video_for_single_shot(
                shot_description=shot_description,
                progress=progress,
            )
            for shot_description in shot_descriptions
        ]
        tasks.extend(video_tasks)
        await asyncio.gather(*tasks)

        final_video_path = os.path.join(self.working_dir, "final_video.mp4")
        if os.path.exists(final_video_path):
            print(f"🚀 Skipped concatenating videos, already exists.")
            _emit_render_progress(progress, "final_video_exists", "Final video already exists", {"path": final_video_path})
        else:
            print(f"🎬 Starting concatenating videos...")
            _emit_render_progress(progress, "concat_start", "Concatenating video clips", {"shot_count": len(shot_descriptions)})
            concatenate_video_files(
                [os.path.join(self.working_dir, "shots", f"{shot_description.idx}", "video.mp4") for shot_description in shot_descriptions],
                final_video_path,
                transition=self.transition,
            )
            print(f"☑️ Concatenated videos, saved to {final_video_path}.")
            _emit_render_progress(progress, "concat_done", "Final video concatenated", {"path": final_video_path})

        final_video_path = await self._finalize_video(
            script,
            user_requirement,
            style,
            characters,
            shot_descriptions,
            final_video_path,
            quiet,
            progress,
        )

        _emit_render_progress(progress, "render_done", "Script2video render complete", {"final_video_path": final_video_path})
        return final_video_path

    async def _finalize_video(
        self,
        script,
        user_requirement,
        style,
        characters,
        shot_descriptions,
        final_video_path,
        quiet=False,
        progress=None,
    ):
        # A consistency repair re-enters __call__ through regenerate_shot. That
        # nested render must stop at the raw concatenation; the outer render will
        # post-process once after every rejected shot has been replaced.
        if getattr(self, "_verifying", False):
            return final_video_path

        if self.consistency_critic is not None and self.consistency_max_retries > 0:
            final_video_path = await self._verify_and_autofix(
                script,
                user_requirement,
                style,
                characters,
                shot_descriptions,
                final_video_path,
                progress,
            )

        effective_track = None
        if self.voiceover_service is not None:
            final_video_path, effective_track = self._maybe_postprocess_audio(
                final_video_path,
                shot_descriptions,
                quiet=quiet,
                progress=progress,
            )

        hook = self._resolve_hook()
        label = self._resolve_label()
        if self.subtitle_service is not None or hook or label or getattr(self, "screen_text_overlay", False):
            final_video_path = self._maybe_burn_subtitles(
                final_video_path,
                shot_descriptions,
                precomputed_track=effective_track,
                hook=hook,
                label=label,
                quiet=quiet,
                progress=progress,
            )

        if self.cover.get("enabled"):
            self._export_poster(final_video_path, progress=progress)

        return final_video_path


    def _fixed_reference(self, character, frame_description=""):
        """Reference portrait path for a scene character bound to a fixed asset
        (None when not bound or the file is missing)."""
        try:
            ident = getattr(character, "identifier_in_scene", None)
            asset_id = self.character_bindings.get(ident) if ident else None
            if asset_id and self.asset_registry is not None:
                if hasattr(self.asset_registry, "select_references"):
                    selections = self.asset_registry.select_references(
                        asset_id, frame_description, max_references=1
                    )
                    if selections:
                        return selections[0].path, (ident or "the character")
                asset = self.asset_registry.get(asset_id)
                front = (getattr(asset, "assets", None) or {}).get("front") if asset else None
                if front and os.path.exists(front):
                    return front, (ident or "the character")
        except Exception:
            pass
        return None, None

    def _minimum_reusable_references(self, target: Any) -> List[Tuple[str, str]]:
        """Keep scene anchors and only the prop sheets relevant to this shot.

        Untyped global references remain available for backward compatibility.
        Reusable prop sheets are appearance references, so pinning unrelated
        props wastes provider slots and can introduce duplicate foreground items.
        """
        reusable_assets = list(getattr(self, "reusable_assets", []) or [])
        global_references = list(getattr(self, "global_reference_images", []) or [])
        target_idx = int(getattr(target, "idx", 0) or 0)
        global_references = [
            pair for pair in global_references
            if "[continuity]" not in str(pair[1]).casefold() or target_idx == 0
        ]
        if not reusable_assets:
            return global_references
        text = _continuity_target_text(target).casefold()
        typed_paths = set()
        selected_paths = set()
        for asset in reusable_assets:
            path = str((getattr(asset, "assets", None) or {}).get("reference") or "")
            if not path:
                continue
            typed_paths.add(path)
            kind = str(getattr(asset, "asset_type", "") or "")
            if kind == "scene" or _reusable_asset_matches_text(asset, text):
                selected_paths.add(path)
        return [
            pair for pair in global_references
            if (
                str(pair[0]) not in typed_paths or str(pair[0]) in selected_paths
            )
        ]

    def _reference_for_shot(self, shot_description, characters):
        references = self._references_for_shot(shot_description, characters)
        return references[0] if references else (None, None)

    def _references_for_shot(self, shot_description, characters):
        references = []
        seen = set()
        description = _frame_target_description(shot_description, "first_frame")
        for ci in (getattr(shot_description, "ff_vis_char_idxs", None) or []):
            if ci is None or ci < 0 or ci >= len(characters):
                continue
            ref, name = self._fixed_reference(characters[ci], description)
            if ref and ref not in seen:
                seen.add(ref)
                references.append((ref, name))
        return references

    async def _failing_shots(self, shot_descriptions, characters):
        """Score each shot's first frame and return [(shot_idx, verdict)] for shots
        that fail review. Identity is judged against the bound character reference;
        when the critic's extra dimensions (aesthetic/adherence) are on they need no
        reference, so reference-less shots are scored too."""
        failing = []
        scored = {}
        anchor_scene_failures = {}
        extra = getattr(self.consistency_critic, "extra_dims_enabled", False)
        contracts = self._load_continuity_contracts(shot_descriptions)
        contract_items = contracts.get("shots") or {}
        scene_enabled = getattr(self.consistency_critic, "scene_threshold", 0.0) > 0
        from quality import continuity_reference_for_shot
        for sd in shot_descriptions:
            references = self._references_for_shot(sd, characters or [])
            if not references and not extra:
                continue  # nothing this critic can judge for this shot
            frame = os.path.join(self.working_dir, "shots", f"{sd.idx}", "first_frame.png")
            if not os.path.exists(frame):
                continue
            desc = _quality_target_description(sd)
            # The last frame (if this shot has one) lets the critic judge in-shot
            # temporal coherence (first vs last frame).
            last_frame = os.path.join(self.working_dir, "shots", f"{sd.idx}", "last_frame.png")
            video_path = os.path.join(self.working_dir, "shots", f"{sd.idx}", "video.mp4")
            if getattr(self.consistency_critic, "video_sampling_enabled", False) and os.path.exists(video_path):
                from quality import VideoConsistencyAuditor
                prompt_state = preflight_shot(sd)
                verdict = await VideoConsistencyAuditor(self.consistency_critic).audit(
                    video_path,
                    references,
                    description=desc,
                    output_dir=os.path.join(self.working_dir, "shots", f"{sd.idx}", "quality_samples"),
                    camera_locked=_camera_is_locked(sd),
                    expected_character_count=len(set(getattr(sd, "ff_vis_char_idxs", None) or [])),
                    asset_references=self._minimum_reusable_references(sd),
                    prop_motion_allowed=any(
                        transition.kind in {"pickup", "put_down"}
                        for transition in prompt_state.transitions
                    ),
                )
            else:
                character_verdicts = {}
                if references:
                    for ref, name in references:
                        character_verdicts[name] = await self.consistency_critic.score(
                            ref, frame, name=name, description=desc,
                            second_frame_path=last_frame,
                        )
                else:
                    character_verdicts["scene"] = await self.consistency_critic.score(
                        "", frame, name="the scene", description=desc,
                        second_frame_path=last_frame,
                    )
                verdict = self._aggregate_character_verdicts(character_verdicts)

            reference_idx, continuity_mode = (
                continuity_reference_for_shot(contracts, sd.idx)
                if scene_enabled else (None, "root")
            )
            if reference_idx is not None and reference_idx != sd.idx:
                anchor_frame = os.path.join(
                    self.working_dir, "shots", str(reference_idx), "first_frame.png"
                )
                contract = contract_items.get(str(sd.idx)) or {}
                scene_verdict = await self.consistency_critic.score_scene(
                    anchor_frame,
                    frame,
                    description=contract.get("expected_changes") or desc,
                    second_frame_path=last_frame,
                    same_camera=continuity_mode == "same_camera",
                    camera_relation=contract.get("camera_relation") or "",
                    anchor_description=(
                        (contract_items.get(str(reference_idx)) or {}).get("expected_changes")
                        or ""
                    ),
                )
                if (
                    not scene_verdict.get("consistent", True)
                    and scene_verdict.get("repair_target") == "anchor"
                ):
                    anchor_scene_failures.setdefault(reference_idx, []).append(
                        (sd.idx, getattr(sd, "cam_idx", None), scene_verdict)
                    )
                else:
                    verdict = self._merge_quality_verdicts(
                        verdict, scene_verdict, extra_name="scene"
                    )
            scored[sd.idx] = verdict

        # A child camera can reveal that the shared root frame is wrong (for
        # example, a prop sits on the floor in the anchor although the script puts
        # it on a bench). Repair the source anchor and let dependency invalidation
        # rebuild its child cameras, instead of repeatedly damaging correct child
        # shots to match a bad root. One child-camera verdict is only advisory:
        # multi-camera VLM comparisons can confuse a newly revealed wall/window for
        # changed architecture. Require corroboration from two independent cameras
        # before replacing a root and cascading through all its dependants.
        for anchor_idx, failures in anchor_scene_failures.items():
            independent_cameras = {
                camera_idx for _child_idx, camera_idx, _verdict in failures
                if camera_idx is not None
            }
            if len(independent_cameras) < 2:
                for child_idx, _camera_idx, scene_verdict in failures:
                    child_verdict = scored.get(child_idx) or {}
                    checks = dict(child_verdict.get("checks") or {})
                    checks["scene_anchor_advisory"] = {
                        **scene_verdict,
                        "consistent": True,
                        "failed": [],
                        "repair_target": "none",
                        "advisory": True,
                        "reason": (
                            "Single-camera anchor suspicion; no automatic root rebuild. "
                            + str(scene_verdict.get("reason") or "")
                        ),
                    }
                    scored[child_idx] = {**child_verdict, "checks": checks}
                continue
            anchor_verdict = scored.get(anchor_idx) or {
                "consistent": True,
                "score": 1.0,
                "dims": {},
                "failed": [],
                "failed_characters": [],
                "characters": {},
                "reason": "",
            }
            for child_idx, _camera_idx, scene_verdict in failures:
                anchor_verdict = self._merge_quality_verdicts(
                    anchor_verdict,
                    scene_verdict,
                    extra_name=f"scene_anchor_for_shot_{child_idx}",
                )
            scored[anchor_idx] = anchor_verdict

        failing = [
            (sd.idx, scored[sd.idx])
            for sd in shot_descriptions
            if sd.idx in scored and not scored[sd.idx].get("consistent", True)
        ]
        if scored:
            self._save_quality(scored)  # persist all verdicts so the UI can badge每镜
        return failing

    @staticmethod
    def _aggregate_character_verdicts(character_verdicts):
        verdicts = list(character_verdicts.values())
        failed = sorted({dim for verdict in verdicts for dim in (verdict.get("failed") or [])})
        dims = {}
        for key in {key for verdict in verdicts for key in (verdict.get("dims") or {})}:
            values = [verdict["dims"][key] for verdict in verdicts if key in (verdict.get("dims") or {})]
            if values:
                dims[key] = min(values)
        failed_characters = [name for name, verdict in character_verdicts.items()
                             if not verdict.get("consistent", True)]
        reasons = [f"{name}: {verdict.get('reason', '')}" for name, verdict in character_verdicts.items()
                   if not verdict.get("consistent", True)]
        return {
            "consistent": not failed_characters,
            "score": min((float(verdict.get("score", 1.0)) for verdict in verdicts), default=1.0),
            "dims": dims,
            "failed": failed,
            "failed_characters": failed_characters,
            "characters": character_verdicts,
            "reason": "; ".join(reasons),
        }

    @staticmethod
    def _merge_quality_verdicts(primary, extra, extra_name="extra"):
        primary = primary or {}
        extra = extra or {}
        dimensions = {}
        for verdict in (primary, extra):
            for key, value in (verdict.get("dims") or {}).items():
                dimensions[key] = min(dimensions.get(key, value), value)
        failed = sorted(set(primary.get("failed") or []) | set(extra.get("failed") or []))
        failed_characters = list(primary.get("failed_characters") or [])
        if not extra.get("consistent", True) and extra_name not in failed_characters:
            failed_characters.append(extra_name)
        reasons = [
            str(verdict.get("reason") or "").strip()
            for verdict in (primary, extra)
            if not verdict.get("consistent", True) and str(verdict.get("reason") or "").strip()
        ]
        checks = dict(primary.get("checks") or {})
        checks[extra_name] = extra
        return {
            **primary,
            "consistent": bool(primary.get("consistent", True) and extra.get("consistent", True)),
            "score": min(float(primary.get("score", 1.0)), float(extra.get("score", 1.0))),
            "dims": dimensions,
            "failed": failed,
            "failed_characters": failed_characters,
            "reason": "; ".join(reasons),
            "checks": checks,
        }

    def _save_quality(self, verdicts: dict) -> None:
        """Persist per-shot critic verdicts to ``quality.json`` (merged) so the
        review UI can show quality badges. Best-effort; never fatal."""
        path = os.path.join(self.working_dir, "quality.json")
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        for idx, v in verdicts.items():
            data[str(idx)] = {"ok": bool(v.get("consistent", True)), "score": v.get("score"),
                              "dims": v.get("dims") or {}, "failed": list(v.get("failed") or []),
                              "failed_characters": list(v.get("failed_characters") or []),
                              "characters": v.get("characters") or {},
                              "samples": v.get("samples") or [],
                              "deterministic": v.get("deterministic") or {},
                              "identity_signal": v.get("identity_signal") or {},
                              "prop_signal": v.get("prop_signal") or {},
                              "reason": str(v.get("reason") or ""),
                              "checks": v.get("checks") or {}}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def _verify_and_autofix(self, script, user_requirement, style, characters, shot_descriptions, final_video_path, progress):
        self._verifying = True
        try:
            for _ in range(self.consistency_max_retries):
                failing = await self._failing_shots(shot_descriptions, characters)
                if not failing:
                    _emit_render_progress(progress, "consistency_ok", "Character consistency check passed", {})
                    return final_video_path
                idxs = [i for i, _v in failing]
                _emit_render_progress(progress, "consistency_fix", "Regenerating inconsistent shots", {"shots": idxs})
                for shot_idx, verdict in failing:
                    # Record the failure reason so the re-render of this shot's first
                    # frame is a targeted correction rather than a blind re-roll.
                    self._shot_corrections[shot_idx] = self._correction_hint(verdict)
                    print(f"🔁 Shot {shot_idx} rejected (score={verdict.get('score')}; {verdict.get('reason','')[:80]}); regenerating.")
                    failed_dims = set(verdict.get("failed") or [])
                    sampled_motion_failure = bool(verdict.get("samples")) and bool(
                        failed_dims
                    ) and failed_dims.issubset({"temporal", "adherence"})
                    if failed_dims and (
                        failed_dims.issubset({"temporal"}) or sampled_motion_failure
                    ):
                        final_video_path = await self.regenerate_video_clip(
                            shot_idx,
                            shot_descriptions,
                            progress=progress,
                        )
                    else:
                        final_video_path = await self.regenerate_shot(
                            shot_idx,
                            script,
                            user_requirement,
                            style,
                            characters,
                        )
            # report remaining failures (kept the best effort result)
            remaining = await self._failing_shots(shot_descriptions, characters)
            if remaining:
                _emit_render_progress(progress, "consistency_unresolved", "Some shots still inconsistent",
                                      {"shots": [i for i, _v in remaining]})
            return final_video_path
        finally:
            self._verifying = False

    # Per-dimension corrective guidance, injected only for the dimensions that
    # actually failed so the re-render targets the real defect.
    _DIM_FIX = {
        "score": "keep the character STRICTLY consistent with the reference portrait — same face, hairstyle, age and signature clothing",
        "aesthetic": "render a clean, high-quality frame — no distortion, no garbled or extra faces/hands/limbs, no warped text, no artifacts",
        "adherence": "faithfully depict the intended shot description; do not drift off-topic",
        "scene": "preserve scene continuity according to the camera contract: keep the same world, spatial topology, architecture, materials, motivated lighting and screen direction; preserve every movable prop's script-defined location, support surface, held/placed state, count and ownership; for the same camera preserve its side and axis while allowing only explicitly described reframing, lens changes or camera movement",
        "temporal": "keep the shot's start and end visually coherent — same character and scene, no morphing or jarring jump",
    }

    @classmethod
    def _correction_hint(cls, verdict) -> str:
        """Turn a critic verdict into a corrective instruction for the re-render,
        scoped to the dimensions that failed."""
        verdict = verdict or {}
        reason = str(verdict.get("reason") or "").strip()
        failed = list(verdict.get("failed") or [])
        bits = ["The previous version of this shot was REJECTED by automatic quality review."]
        if reason:
            bits.append("Problems found: " + reason[:300] + ".")
        fixes = [cls._DIM_FIX[k] for k in failed if k in cls._DIM_FIX]
        if not fixes:  # unknown/empty -> fall back to the full checklist
            fixes = list(cls._DIM_FIX.values())
        bits.append("Fix these issues now: " + "; ".join(fixes) + ".")
        return " ".join(bits)


    async def _auto_hook(self, source):
        """Auto-generate a hook line from the script via the chat model. With
        video.hook.candidates>1, generate N options and auto-select the best
        (records the alternatives on self._hook_candidates). Never fatal."""
        self._hook_candidates = []
        try:
            from agents.hook_writer import HookWriter
            n = int(self.hook.get("candidates", 1) or 1)
            writer = HookWriter(self.chat_model, extra_instruction=self._domain_pack.hook)
            if n > 1:
                result = await writer.best(source, n=n, chinese=bool(self.chinese_instruction))
                self._hook_candidates = result.get("candidates", [])
                return result.get("chosen", "")
            return await writer.generate(source, chinese=bool(self.chinese_instruction))
        except Exception:
            return ""

    def _resolve_hook(self):
        """Return the opening-hook spec (with resolved text) when enabled and a
        text is available (per-render ``hook_text`` wins over config text)."""
        if not self.hook.get("enabled"):
            return None
        text = (getattr(self, "_hook_text", "") or self.hook.get("text") or "").strip()
        if not text:
            return None
        hook = dict(self.hook)
        hook["text"] = text
        return hook

    def _resolve_label(self):
        """Return the persistent AIGC compliance label spec when enabled."""
        if not self.aigc_label.get("enabled"):
            return None
        text = (self.aigc_label.get("text") or "AI生成").strip()
        if not text:
            return None
        label = dict(self.aigc_label)
        label["text"] = text
        return label

    def _export_poster(self, final_video_path, progress=None):
        try:
            from video import export_poster
            out = os.path.join(self.working_dir, self.cover.get("filename", "poster.jpg"))
            poster = export_poster(final_video_path, out, at_seconds=float(self.cover.get("at", 0.0)))
            if poster:
                print(f"🖼️ Poster exported -> {poster}.")
                _emit_render_progress(progress, "poster_exported", "Poster exported", {"path": poster})
        except Exception:
            pass


    def _maybe_postprocess_audio(self, final_video_path, shot_descriptions, quiet=False, progress=None):
        """Add TTS voiceover + sound effects + background music + loudness
        normalization in one ffmpeg pass, muxed onto the final video. Degrades to
        the original video on any failure (no key, missing assets, no ffmpeg) so
        it never breaks a render.

        Voiceover reuses the subtitle extraction + timeline so audio and
        subtitles share identical timing; when no SubtitleService is configured a
        throwaway one is used purely for the timeline math. BGM/SFX apply even
        when there is no spoken dialogue.
        """
        service = self.voiceover_service
        timeline_source = self.subtitle_service
        if timeline_source is None:
            from subtitles import SubtitleService
            timeline_source = SubtitleService(enabled=True, burn_in_enabled=False)

        video_paths = [os.path.join(self.working_dir, "shots", f"{sd.idx}", "video.mp4") for sd in shot_descriptions]
        track = timeline_source.build_track(shot_descriptions, video_paths)

        processed_path = os.path.join(self.working_dir, "final_video_audio.mp4")
        track_cache = os.path.join(self.working_dir, "audio", "voiced_track.json")
        if os.path.exists(processed_path) and os.path.getmtime(processed_path) >= os.path.getmtime(final_video_path):
            print("🚀 Skipped audio post, already exists.")
            # Reload the retimed track persisted on the first pass so the subtitle
            # step still aligns to the audio timeline without re-synthesizing
            # (which would re-bill TTS). Falls back to the estimate if missing.
            return processed_path, self._load_track_cache(track_cache, fallback=track)

        _emit_render_progress(progress, "audio_post_start", "Post-processing audio (voiceover/BGM/SFX)", {"line_count": len(track)})
        processed, effective_track = service.render_audio(final_video_path, track,
                                                          shot_descriptions=shot_descriptions, video_paths=video_paths,
                                                          working_dir=self.working_dir, transition=self.transition)
        if processed:
            print(f"🔊 Added voiceover/BGM/SFX -> {processed}.")
            self._save_track_cache(track_cache, effective_track)
            _emit_render_progress(progress, "audio_post_done", "Audio post-processing complete", {"path": processed})
            return processed, effective_track
        _pipeline_print(quiet, "🈳 No added audio (no speech/BGM/SFX or unavailable); keeping original audio.")
        _emit_render_progress(progress, "audio_post_skipped", "Audio post-processing skipped", {})
        return final_video_path, effective_track


    @staticmethod
    def _save_track_cache(path, track):
        """Persist a retimed SubtitleTrack so subtitle burn-in can reuse the
        exact audio timing on resume without re-synthesizing TTS."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(track.model_dump_json())
        except Exception:
            pass

    @staticmethod
    def _load_track_cache(path, fallback=None):
        try:
            from subtitles.models import SubtitleTrack
            with open(path, "r", encoding="utf-8") as f:
                return SubtitleTrack.model_validate_json(f.read())
        except Exception:
            return fallback

    def _build_screen_text_events(self, shot_descriptions, final_video_path):
        """Timed on-screen-text overlay events (one per shot carrying ``screen_text``),
        aligned to the final video timeline. Empty when the overlay is off (picture
        isn't text-free) or no shot needs text.

        Timing: raw per-shot windows from the concat timeline (cumulative shot
        durations), then scaled to the actual final-video duration so it stays
        aligned even after audio post-processing retimed/extended shots."""
        if not getattr(self, "screen_text_overlay", False):
            return []
        items = [sd for sd in shot_descriptions if (getattr(sd, "screen_text", None) or "").strip()]
        if not items:
            return []
        from subtitles.timeline import probe_duration
        windows, cursor = {}, 0.0
        for sd in shot_descriptions:
            vp = os.path.join(self.working_dir, "shots", f"{sd.idx}", "video.mp4")
            try:
                dur = max(0.0, float(probe_duration(vp))) if os.path.exists(vp) else 0.0
            except Exception:
                dur = 0.0
            windows[sd.idx] = (cursor, cursor + dur)
            cursor += dur
        raw_total = cursor
        scale = 1.0
        try:
            final_total = float(probe_duration(final_video_path))
            if raw_total > 0 and final_total > 0:
                scale = final_total / raw_total
        except Exception:
            scale = 1.0
        events = []
        for sd in items:
            start, end = windows.get(sd.idx, (0.0, 0.0))
            if end <= start:
                continue
            events.append({"text": (sd.screen_text or "").strip(),
                           "start": start * scale, "end": end * scale,
                           "position": getattr(sd, "screen_text_pos", None) or "center"})
        return events

    def _maybe_burn_subtitles(self, final_video_path, shot_descriptions, precomputed_track=None, hook=None, label=None, quiet=False, progress=None):
        """Extract spoken content, render .ass/.srt, and burn subtitles (and an
        optional opening hook overlay) into a copy of the final video. Degrades
        to the original video on any failure (no speech, missing ffmpeg, missing
        font) so it never breaks a render.

        ``precomputed_track`` (when given) is reused verbatim so subtitles share
        the exact timing the voiceover used; otherwise the track is built fresh.
        ``hook`` (when given) burns a big opening overlay in the same pass; it can
        run even with no dialogue subtitles (e.g. narration-only hook).
        """
        from subtitles import SubtitleService
        service = self.subtitle_service or SubtitleService(enabled=True, burn_in_enabled=True)
        subtitled_path = os.path.join(self.working_dir, "final_video_with_subtitles.mp4")
        if os.path.exists(subtitled_path) and os.path.getmtime(subtitled_path) > os.path.getmtime(final_video_path):
            print(f"🚀 Skipped subtitle burn-in, already exists.")
            return subtitled_path

        if precomputed_track is not None and len(precomputed_track) > 0:
            track = precomputed_track
        elif self.subtitle_service is not None:
            video_paths = [os.path.join(self.working_dir, "shots", f"{sd.idx}", "video.mp4") for sd in shot_descriptions]
            track = service.build_track(shot_descriptions, video_paths)
        else:
            from subtitles.models import SubtitleTrack
            track = SubtitleTrack(lines=[])
        screen_texts = (self._build_screen_text_events(shot_descriptions, final_video_path)
                        if getattr(self, "screen_text_overlay", False) else [])
        if len(track) == 0 and not hook and not label and not screen_texts:
            _pipeline_print(quiet, "🈳 No spoken content found; skipping subtitles.")
            return final_video_path

        subtitles_dir = os.path.join(self.working_dir, "subtitles")
        ass_path = service.render_ass(track, os.path.join(subtitles_dir, "final.ass"), hook=hook, label=label,
                                      screen_texts=screen_texts)
        service.render_srt(track, os.path.join(subtitles_dir, "final.srt"))
        _emit_render_progress(progress, "subtitle_burn_start", "Burning subtitles", {"line_count": len(track), "hook": bool(hook), "label": bool(label)})
        metadata = {"comment": f"AIGC {label['text']}", "AIGC": "true"} if label else None
        burned = service.burn_in(final_video_path, ass_path, subtitled_path, metadata=metadata)
        if burned:
            print(f"💬 Burned {len(track)} subtitle line(s) -> {burned}.")
            _emit_render_progress(progress, "subtitle_burn_done", "Subtitles burned", {"path": burned})
            return burned
        _pipeline_print(quiet, "⚠️ Subtitle burn-in failed; returning video without subtitles.")
        _emit_render_progress(progress, "subtitle_burn_skipped", "Subtitle burn-in unavailable", {})
        return final_video_path


    async def regenerate_shot(
        self,
        shot_idx: int,
        script: str,
        user_requirement: str,
        style: str,
        characters: List[CharacterInScene] = None,
        keep_description: bool = True,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ):
        """Regenerate a single shot (and every shot that depends on it) without
        rerunning the whole film.

        Strategy: archive the affected shots' artifacts under
        ``shots/<idx>/_archive/v<n>/`` (never overwrite old versions), drop the
        stale ``final_video.mp4``, then reuse ``__call__`` whose ``os.path.exists``
        resume logic regenerates exactly the archived (now-missing) shots and
        re-concatenates the final video.

        ``keep_description=True`` re-renders the shot from its existing
        ``shot_description.json`` (画面/视频重出，剧本不变). Pass ``False`` to also
        re-decompose the shot's visual description from the storyboard. The camera
        grouping (camera_tree) is intentionally preserved either way.
        """
        camera_tree = self._load_camera_tree()
        if camera_tree is None:
            raise RuntimeError(
                "Cannot regenerate a shot before a full render exists "
                "(camera_tree.json is missing)."
            )
        affected = self._collect_dependent_shots(shot_idx, camera_tree)
        self._last_regenerated_shots = list(affected)
        _emit_render_progress(
            progress,
            "regenerate_shot_start",
            f"Regenerating shot {shot_idx} and {len(affected) - 1} dependent shot(s)",
            {"shot_idx": shot_idx, "affected_shots": affected, "keep_description": keep_description},
        )
        for idx in affected:
            shot_dir = os.path.join(self.working_dir, "shots", str(idx))
            archive_dir = self._archive_shot_dir(shot_dir, keep_description=keep_description)
            if archive_dir is not None:
                print(f"🗄️ Archived shot {idx} artifacts to {archive_dir}.")
                _emit_render_progress(progress, "shot_archived", f"Archived shot {idx}", {"shot_idx": idx, "archive_dir": archive_dir})

        final_video_path = os.path.join(self.working_dir, "final_video.mp4")
        if os.path.exists(final_video_path):
            os.remove(final_video_path)

        return await self.__call__(
            script=script,
            user_requirement=user_requirement,
            style=style,
            characters=characters,
            character_portraits_registry=None,
            quiet=True,
            progress=progress,
        )

    async def regenerate_video_clip(
        self,
        shot_idx: int,
        shot_descriptions: List[ShotDescription],
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ) -> str:
        """Re-render only one temporal clip while preserving approved keyframes.

        Ghosting and static-world drift are video interpolation defects, not
        keyframe defects. Rebuilding character/scene frames for those failures
        adds cost and can destabilize every dependent camera, so this path archives
        only the clip, its samples, and aggregate renders before reusing time-zero.
        """
        description = next(
            (item for item in shot_descriptions if int(item.idx) == int(shot_idx)),
            None,
        )
        if description is None:
            raise ValueError(f"Unknown shot index: {shot_idx}")
        shot_dir = os.path.join(self.working_dir, "shots", str(shot_idx))
        first_frame = os.path.join(shot_dir, "first_frame.png")
        if not _is_reusable_image(first_frame):
            raise RuntimeError("Cannot re-render video only: first frame is missing or invalid")
        if _requires_last_frame(description):
            last_frame = os.path.join(shot_dir, "last_frame.png")
            if not _is_reusable_image(last_frame):
                raise RuntimeError("Cannot re-render video only: required last frame is missing or invalid")

        version = 1
        while os.path.exists(os.path.join(shot_dir, "_archive", f"clip_v{version}")):
            version += 1
        clip_archive = os.path.join(shot_dir, "_archive", f"clip_v{version}")
        os.makedirs(clip_archive, exist_ok=True)
        for name in ("video.mp4", "render_plan.json", "quality_samples", "candidates"):
            source = os.path.join(shot_dir, name)
            if os.path.exists(source):
                shutil.move(source, os.path.join(clip_archive, name))

        aggregate_version = 1
        aggregate_base = os.path.join(self.working_dir, "_archive", "video_clip_rerenders")
        while os.path.exists(os.path.join(aggregate_base, f"v{aggregate_version}")):
            aggregate_version += 1
        aggregate_archive = os.path.join(aggregate_base, f"v{aggregate_version}")
        aggregate_sources = [
            os.path.join(self.working_dir, name)
            for name in os.listdir(self.working_dir)
            if name.startswith("final_video") and name.endswith(".mp4")
        ]
        quality_path = os.path.join(self.working_dir, "quality.json")
        if os.path.exists(quality_path):
            aggregate_sources.append(quality_path)
        if aggregate_sources:
            os.makedirs(aggregate_archive, exist_ok=True)
            for source in aggregate_sources:
                shutil.move(source, os.path.join(aggregate_archive, os.path.basename(source)))

        first_ready = asyncio.Event()
        first_ready.set()
        events = {"first_frame": first_ready}
        if _requires_last_frame(description):
            last_ready = asyncio.Event()
            last_ready.set()
            events["last_frame"] = last_ready
        self.frame_events[shot_idx] = events
        _emit_render_progress(
            progress,
            "video_clip_regenerate_start",
            f"Re-rendering video clip {shot_idx} from approved keyframes",
            {"shot_idx": shot_idx, "clip_archive": clip_archive},
        )
        try:
            await self.generate_video_for_single_shot(description, progress=progress)
        except Exception:
            archived_video = os.path.join(clip_archive, "video.mp4")
            active_video = os.path.join(shot_dir, "video.mp4")
            if os.path.exists(archived_video) and not os.path.exists(active_video):
                shutil.copy2(archived_video, active_video)
            raise

        final_video_path = os.path.join(self.working_dir, "final_video.mp4")
        concatenate_video_files(
            [
                os.path.join(self.working_dir, "shots", str(item.idx), "video.mp4")
                for item in shot_descriptions
            ],
            final_video_path,
            transition=self.transition,
        )
        _emit_render_progress(
            progress,
            "video_clip_regenerate_done",
            f"Re-rendered video clip {shot_idx}",
            {"shot_idx": shot_idx, "path": os.path.join(shot_dir, "video.mp4")},
        )
        return final_video_path

    def _load_camera_tree(self) -> Optional[List[Camera]]:
        camera_tree_path = os.path.join(self.working_dir, "camera_tree.json")
        if not os.path.exists(camera_tree_path):
            return None
        with open(camera_tree_path, "r", encoding="utf-8") as f:
            return [Camera.model_validate(camera) for camera in json.load(f)]

    def _write_continuity_contracts(self, camera_tree, shot_descriptions) -> dict:
        from quality import (
            build_continuity_contracts,
            build_continuity_ledger,
            save_continuity_contracts,
            save_continuity_ledger,
        )

        contracts = build_continuity_contracts(camera_tree, shot_descriptions)
        save_continuity_contracts(
            os.path.join(self.working_dir, "continuity_contracts.json"),
            contracts,
        )
        preflight_path = os.path.join(self.working_dir, "prompt_preflight.json")
        try:
            with open(preflight_path, "r", encoding="utf-8") as stream:
                preflight_report = json.load(stream)
        except (OSError, TypeError, ValueError):
            preflight_report = preflight_storyboard(shot_descriptions)
        ledger = build_continuity_ledger(
            shot_descriptions,
            contracts=contracts,
            preflight_report=preflight_report,
            characters=self._continuity_characters,
            character_bindings=self.character_bindings,
            character_assets=self.asset_registry,
            reusable_assets=self.reusable_assets,
            inherited_ledger=getattr(self, "inherited_continuity_ledger", None),
            inheritance_source=getattr(self, "continuity_inheritance_source", None),
        )
        save_continuity_ledger(
            os.path.join(self.working_dir, "continuity_ledger.json"),
            ledger,
        )
        return contracts

    def _write_prompt_preflight(self, shot_descriptions) -> dict:
        """Persist semantic checks before any paid image/video generation starts."""
        report = preflight_storyboard(shot_descriptions)
        save_prompt_preflight_report(
            os.path.join(self.working_dir, "prompt_preflight.json"),
            report,
        )
        for shot_idx, shot_report in report.get("shots", {}).items():
            shot_dir = os.path.join(self.working_dir, "shots", str(shot_idx))
            os.makedirs(shot_dir, exist_ok=True)
            atomic_write_text(
                os.path.join(shot_dir, "prompt_preflight.json"),
                json.dumps(shot_report, ensure_ascii=False, indent=2),
            )
        return report

    def _load_continuity_contracts(self, shot_descriptions=None) -> dict:
        from quality import build_continuity_contracts, load_continuity_contracts

        path = os.path.join(self.working_dir, "continuity_contracts.json")
        contracts = load_continuity_contracts(path)
        if contracts.get("shots") or not shot_descriptions:
            return contracts
        return build_continuity_contracts(self._load_camera_tree() or [], shot_descriptions)

    @staticmethod
    def _collect_dependent_shots(shot_idx: int, camera_tree: List[Camera]) -> List[int]:
        """Transitive closure of shots whose frames are derived from ``shot_idx``.

        Two kinds of dependency edges exist in the rendering graph:

        1. Same-camera: every shot in a camera reuses the camera's first shot
           (``active_shot_idxs[0]``, the "anchor") first_frame as a reference, so
           the anchor's siblings depend on the anchor.
        2. Cross-camera: a child camera derives its anchor's first_frame from its
           ``parent_shot_idx`` (via a transition video), so that child anchor
           depends on ``parent_shot_idx``.

        Regenerating ``shot_idx`` therefore invalidates the whole reachable set.
        Note edges flow only anchor->sibling and parent_shot->child_anchor, so
        regenerating a non-anchor sibling affects nothing unless another camera
        explicitly points its ``parent_shot_idx`` at it.
        """
        siblings_of_anchor = {}
        children_of_parent_shot = {}
        for camera in camera_tree:
            if not camera.active_shot_idxs:
                continue
            anchor = camera.active_shot_idxs[0]
            siblings_of_anchor.setdefault(anchor, []).extend(camera.active_shot_idxs[1:])
            if camera.parent_shot_idx is not None:
                children_of_parent_shot.setdefault(camera.parent_shot_idx, []).append(anchor)

        affected = set()
        stack = [shot_idx]
        while stack:
            current = stack.pop()
            if current in affected:
                continue
            affected.add(current)
            stack.extend(siblings_of_anchor.get(current, []))
            stack.extend(children_of_parent_shot.get(current, []))
        return sorted(affected)

    @staticmethod
    def _archive_shot_dir(shot_dir: str, keep_description: bool = True) -> Optional[str]:
        """Move a shot's artifacts into ``shots/<idx>/_archive/v<n>/`` instead of
        deleting them, so old versions are never overwritten.

        Returns the archive directory path, or ``None`` if the shot dir does not
        exist. When ``keep_description`` is True the shot's ``shot_description.json``
        is left in place so the re-render reuses the same plan.
        """
        if not os.path.isdir(shot_dir):
            return None
        version = 1
        while os.path.exists(os.path.join(shot_dir, "_archive", f"v{version}")):
            version += 1
        archive_dir = os.path.join(shot_dir, "_archive", f"v{version}")
        os.makedirs(archive_dir, exist_ok=True)
        for name in os.listdir(shot_dir):
            if name == "_archive":
                continue
            if keep_description and name == "shot_description.json":
                continue
            shutil.move(os.path.join(shot_dir, name), os.path.join(archive_dir, name))
        return archive_dir


    async def generate_frames_for_single_camera(
        self,
        camera: Camera,
        shot_descriptions: List[ShotDescription],
        characters: List[CharacterInScene],
        character_portraits_registry: Dict[str, Dict[str, Dict[str, str]]],
        priority_shot_idxs: List[int],
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
        world_reference_pair: Optional[Tuple[str, str]] = None,
        first_frames_only: bool = False,
    ):
        # 1. generate the first_frame of the first shot of the camera
        first_shot_idx = camera.active_shot_idxs[0]
        first_shot_ff_path = os.path.join(self.working_dir, "shots", f"{first_shot_idx}", "first_frame.png")
        _emit_render_progress(progress, "camera_frames_start", f"Generating frames for camera {camera.idx}", {"camera_idx": camera.idx, "active_shot_idxs": camera.active_shot_idxs})

        if _is_reusable_image(first_shot_ff_path):
            print(f"🚀 Skipped generating first_frame for shot {first_shot_idx}, already exists.")
            self.frame_events[first_shot_idx]["first_frame"].set()
            _emit_render_progress(progress, "frame_exists", f"First frame for shot {first_shot_idx} already exists", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame", "path": first_shot_ff_path})

        else:
            print(f"🖼️ Starting first_frame generation for shot {first_shot_idx}...")
            _emit_render_progress(progress, "frame_start", f"Generating first frame for shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame"})
            available_image_path_and_text_pairs = []
            continuity_reference_paths = []

            for character_idx in shot_descriptions[first_shot_idx].ff_vis_char_idxs:
                identifier_in_scene = characters[character_idx].identifier_in_scene
                registry_item = character_portraits_registry[identifier_in_scene]
                for view, item in registry_item.items():
                    available_image_path_and_text_pairs.append((item["path"], item["description"]))
            shot_reusable_references = self._minimum_reusable_references(
                shot_descriptions[first_shot_idx]
            )
            available_image_path_and_text_pairs.extend(shot_reusable_references)
            if world_reference_pair and os.path.exists(str(world_reference_pair[0])):
                available_image_path_and_text_pairs.append(world_reference_pair)
            
            # A bound scene model is a cleaner multi-camera world anchor than a
            # parent frame containing actors in an earlier blocking position.
            # Reusing that parent frame as pixels often copies the old actor pose
            # and turns canonical prop sheets into oversized foreground cutouts.
            use_parent_camera_reference = (
                camera.parent_shot_idx is not None
                and not _has_bound_scene_reference(self.global_reference_images)
            )

            # generate the first_frame based on the shot_description.ff_desc
            if use_parent_camera_reference:
                # generate the first_frame based on the transition video
                parent_shot_idx = camera.parent_shot_idx
                await self.frame_events[parent_shot_idx]["first_frame"].wait()
                parent_shot_ff_path = os.path.join(self.working_dir, "shots", f"{parent_shot_idx}", "first_frame.png")
                transition_video_path = os.path.join(self.working_dir, "shots", f"{first_shot_idx}", f"transition_video_from_shot_{parent_shot_idx}.mp4")

                if os.path.exists(transition_video_path):
                    print(f"🚀 Skipped generating transition video for shot {first_shot_idx} from shot {parent_shot_idx}, already exists.")
                    _emit_render_progress(progress, "transition_video_exists", f"Transition video for shot {first_shot_idx} already exists", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "parent_shot_idx": parent_shot_idx, "path": transition_video_path})
                else:
                    print(f"🖼️ Starting transition video generation for shot {first_shot_idx} from shot {parent_shot_idx}...")
                    _emit_render_progress(progress, "transition_video_start", f"Generating transition video for shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "parent_shot_idx": parent_shot_idx})
                    transition_video_output = await self.camera_image_generator.generate_transition_video(
                        first_shot_visual_desc=shot_descriptions[parent_shot_idx].visual_desc,
                        second_shot_visual_desc=shot_descriptions[first_shot_idx].visual_desc,
                        first_shot_ff_path=parent_shot_ff_path,
                        progress=_scoped_progress(progress, camera_idx=camera.idx, shot_idx=first_shot_idx, parent_shot_idx=parent_shot_idx, artifact="transition_video"),
                    )
                    transition_video_output.save(transition_video_path)
                    print(f"☑️ Generated transition video for shot {first_shot_idx} from shot {parent_shot_idx}, saved to {transition_video_path}.")
                    _emit_render_progress(progress, "transition_video_done", f"Transition video for shot {first_shot_idx} generated", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "parent_shot_idx": parent_shot_idx, "path": transition_video_path})

                new_camera_image_path = os.path.join(self.working_dir, "shots", f"{first_shot_idx}", f"new_camera_{camera.idx}.png")
                if os.path.exists(new_camera_image_path):
                    print(f"🚀 Skipped generating new camera image for shot {first_shot_idx}, already exists.")
                    _emit_render_progress(progress, "new_camera_image_exists", f"New camera image for shot {first_shot_idx} already exists", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "path": new_camera_image_path})
                else:
                    print(f"🖼️ Starting new camera image generation for shot {first_shot_idx}...")
                    _emit_render_progress(progress, "new_camera_image_start", f"Extracting new camera image for shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx})
                    new_camera_image = self.camera_image_generator.get_new_camera_image(transition_video_path)
                    new_camera_image.save(new_camera_image_path)
                    print(f"☑️ Generated new camera image for shot {first_shot_idx} (not completed), saved to {new_camera_image_path}.")
                    _emit_render_progress(progress, "new_camera_image_done", f"New camera image for shot {first_shot_idx} extracted", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "path": new_camera_image_path})

                # Offer the new-camera image regardless of whether it was just
                # generated or already on disk: when this sat in the else branch
                # above, resumed runs silently dropped the key composition
                # reference and produced different frames than fresh runs.
                available_image_path_and_text_pairs.append(
                    (
                        new_camera_image_path,
                        f"The composition and background are correct but some elements may be wrong. The wrong elements should be replaced.\nWrong elements: {camera.missing_info}.\nYou must select this image as the main reference and replace the characters in the image with the provided character portraits. Don't change the background."
                    )
                )
                continuity_reference_paths.append(new_camera_image_path)


            # 如果子镜头缺少信息，则需要选择参考图像生成
            if not use_parent_camera_reference or camera.missing_info is not None:
                ff_selector_output_path = os.path.join(self.working_dir, "shots", f"{first_shot_idx}", "first_frame_selector_output.json")
                if os.path.exists(ff_selector_output_path):
                    with open(ff_selector_output_path, 'r', encoding='utf-8') as f:
                        ff_selector_output = json.load(f)
                    print(f"🚀 Loaded existing reference image selection and prompt for first_frame of shot {first_shot_idx} from {ff_selector_output_path}.")
                    _emit_render_progress(progress, "frame_prompt_exists", f"First frame prompt for shot {first_shot_idx} already exists", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame", "path": ff_selector_output_path})
                else:
                    print(f"🔍 Selecting reference images and generating prompt for first_frame of shot {first_shot_idx}...")
                    _emit_render_progress(progress, "frame_prompt_start", f"Selecting references for first frame of shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame"})
                    ff_selector_output = await self.reference_image_selector.select_reference_images_and_generate_prompt(
                        available_image_path_and_text_pairs=available_image_path_and_text_pairs,
                        frame_description=_frame_target_description(
                            shot_descriptions[first_shot_idx], "first_frame"
                        ),
                        pinned_reference_paths=[path for path, _text in shot_reusable_references],
                        continuity_reference_paths=continuity_reference_paths,
                        world_reference_paths=(
                            [world_reference_pair[0]] if world_reference_pair else []
                        ),
                    )
                    with open(ff_selector_output_path, 'w', encoding='utf-8') as f:
                        json.dump(ff_selector_output, f, ensure_ascii=False, indent=4)

                    print(f"☑️ Selected reference images and generated prompt for first_frame of shot {first_shot_idx}, saved to {ff_selector_output_path}.")
                    _emit_render_progress(progress, "frame_prompt_done", f"Selected references for first frame of shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame", "path": ff_selector_output_path})

                reference_image_path_and_text_pairs, prompt = _prepare_frame_references(
                    ff_selector_output["reference_image_path_and_text_pairs"],
                    shot_reusable_references,
                    ff_selector_output["text_prompt"],
                    self.image_size,
                )
                prefix_prompt = ""
                for i, (image_path, text) in enumerate(reference_image_path_and_text_pairs):
                    prefix_prompt += f"Image {i}: {text}\n"
                prompt = f"{prefix_prompt}\n{prompt}"
                # Directed regeneration: if this shot was flagged by the consistency
                # critic, fold its failure reason into the prompt (consumed once).
                correction = getattr(self, "_shot_corrections", {}).get(first_shot_idx)
                if correction:
                    prompt = f"{prompt}\n\n[Correction] {correction}"
                reference_image_paths = [item[0] for item in reference_image_path_and_text_pairs]
                await self._generate_best_frame(
                    shot_idx=first_shot_idx,
                    frame_type="first_frame",
                    output_path=first_shot_ff_path,
                    prompt=prompt,
                    reference_image_paths=reference_image_paths,
                    reference_image_path_and_text_pairs=reference_image_path_and_text_pairs,
                    target_description=_frame_target_description(
                        shot_descriptions[first_shot_idx], "first_frame"
                    ),
                    progress=progress,
                    camera_idx=camera.idx,
                )
                self.frame_events[first_shot_idx]["first_frame"].set()
                print(f"☑️ Generated first_frame for shot {first_shot_idx}, saved to {first_shot_ff_path}.")
                _emit_render_progress(progress, "frame_done", f"Generated first frame for shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame", "path": first_shot_ff_path})
            else:
                shutil.copy(new_camera_image_path, first_shot_ff_path)
                self.frame_events[first_shot_idx]["first_frame"].set()
                print(f"☑️ Generated first_frame for shot {first_shot_idx}, saved to {first_shot_ff_path}.")
                _emit_render_progress(progress, "frame_done", f"Generated first frame for shot {first_shot_idx}", {"camera_idx": camera.idx, "shot_idx": first_shot_idx, "frame_type": "first_frame", "path": first_shot_ff_path})


        # 2. generate the following frames of the camera
        priority_tasks = []
        normal_tasks = []

        if not first_frames_only and _requires_last_frame(shot_descriptions[first_shot_idx]):
            task = self.generate_frame_for_single_shot(
                shot_idx=first_shot_idx, 
                frame_type="last_frame", 
                first_shot_ff_path_and_text_pair=(
                    first_shot_ff_path,
                    _frame_target_description(shot_descriptions[first_shot_idx], "first_frame"),
                ),
                frame_desc=_frame_target_description(
                    shot_descriptions[first_shot_idx], "last_frame"
                ),
                visible_characters=[characters[idx] for idx in shot_descriptions[first_shot_idx].lf_vis_char_idxs],
                character_portraits_registry=character_portraits_registry,
                progress=progress,
            )
            normal_tasks.append(task)

        for shot_idx in camera.active_shot_idxs[1:]:
            first_frame_task = self.generate_frame_for_single_shot(
                    shot_idx=shot_idx, 
                    frame_type="first_frame", 
                    first_shot_ff_path_and_text_pair=(
                        first_shot_ff_path,
                        _frame_target_description(shot_descriptions[first_shot_idx], "first_frame"),
                    ),
                    frame_desc=_frame_target_description(
                        shot_descriptions[shot_idx], "first_frame"
                    ),
                    visible_characters=[characters[idx] for idx in shot_descriptions[shot_idx].ff_vis_char_idxs],
                    character_portraits_registry=character_portraits_registry,
                    progress=progress,
                )
            if shot_idx in priority_shot_idxs:
                priority_tasks.append(first_frame_task)
            else:
                normal_tasks.append(first_frame_task)


            if not first_frames_only and _requires_last_frame(shot_descriptions[shot_idx]):
                last_frame_task = self.generate_frame_for_single_shot(
                    shot_idx=shot_idx, 
                    frame_type="last_frame", 
                    first_shot_ff_path_and_text_pair=(
                        first_shot_ff_path,
                        _frame_target_description(shot_descriptions[first_shot_idx], "first_frame"),
                    ),
                    frame_desc=_frame_target_description(
                        shot_descriptions[shot_idx], "last_frame"
                    ),
                    visible_characters=[characters[idx] for idx in shot_descriptions[shot_idx].lf_vis_char_idxs],
                    character_portraits_registry=character_portraits_registry,
                    progress=progress,
                )
                normal_tasks.append(last_frame_task)


        await asyncio.gather(*priority_tasks)
        await asyncio.gather(*normal_tasks)
        _emit_render_progress(progress, "camera_frames_done", f"Frames for camera {camera.idx} ready", {"camera_idx": camera.idx, "active_shot_idxs": camera.active_shot_idxs})



    async def generate_video_for_single_shot(
        self,
        shot_description: ShotDescription,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ):
        video_path = os.path.join(self.working_dir, "shots", f"{shot_description.idx}", "video.mp4")
        if os.path.exists(video_path):
            print(f"🚀 Skipped generating video for shot {shot_description.idx}, already exists.")
            _emit_render_progress(progress, "video_clip_exists", f"Video clip for shot {shot_description.idx} already exists", {"shot_idx": shot_description.idx, "path": video_path})
        else:
            _emit_render_progress(progress, "video_clip_waiting_for_frames", f"Waiting for frames before video clip {shot_description.idx}", {"shot_idx": shot_description.idx})
            await self.frame_events[shot_description.idx]["first_frame"].wait()
            use_last_frame = _requires_last_frame(shot_description)
            if use_last_frame:
                await self.frame_events[shot_description.idx]["last_frame"].wait()

            frame_paths = []
            frame_paths.append(os.path.join(self.working_dir, "shots", f"{shot_description.idx}", "first_frame.png"))
            if use_last_frame:
                frame_paths.append(os.path.join(self.working_dir, "shots", f"{shot_description.idx}", "last_frame.png"))

            duration_plan = plan_video_duration(self.video_generator, shot_description.duration_sec)
            duration_metadata = duration_plan.to_dict()
            _vid_prompt, prompt_rewritten = _compile_reference_aware_video_prompt(
                shot_description
            )
            prompt_preflight = preflight_shot(shot_description)
            fixed_issue_codes = [
                issue.code for issue in prompt_preflight.issues if issue.auto_fixed
            ]
            render_plan_path = os.path.join(
                self.working_dir, "shots", f"{shot_description.idx}", "render_plan.json"
            )
            from services.production_metrics import append_generation, utc_now
            from uuid import uuid4

            generation_id = uuid4().hex
            telemetry_started = time.perf_counter()
            started_at = utc_now()
            route_context = dict(getattr(self, "generation_context", {}) or {})
            attempt_records: List[Dict[str, Any]] = []
            remote_tasks: Dict[str, Dict[str, Any]] = {}
            actual_costs: Dict[str, float] = {}
            billing_currency = ""
            queue_seconds = 0.0
            generation_seconds = 0.0
            download_seconds = 0.0
            successful_candidates = []
            candidate_errors = []
            candidate_selection = None
            production_request = {
                "generation_id": generation_id,
                "shot_index": int(shot_description.idx),
                "status": "requested",
                "started_at": started_at,
                "route": route_context,
                "request": {
                    "reference_image_paths": list(frame_paths),
                    "reference_count": len(frame_paths),
                },
            }
            reference_strategy = {
                "camera_locked": _camera_is_locked(shot_description),
                "use_last_frame": use_last_frame,
                "reference_frame_count": len(frame_paths),
                "reference_entry_conflict_rewritten": (
                    "actor_already_visible_before_entry" in fixed_issue_codes
                ),
                "reference_state_conflict_rewritten": prompt_rewritten,
                "prompt_preflight_status": prompt_preflight.status.value,
                "prompt_preflight_issue_codes": [
                    issue.code for issue in prompt_preflight.issues
                ],
                "video_candidate_count": getattr(self, "video_candidate_count", 1),
            }
            atomic_write_text(
                render_plan_path,
                json.dumps({
                    **duration_metadata,
                    **reference_strategy,
                    "status": "requested",
                    "production": production_request,
                }, ensure_ascii=False, indent=2),
            )

            print(f"🎬 Starting video generation for shot {shot_description.idx}...")
            _emit_render_progress(
                progress,
                "video_clip_start",
                f"Generating video clip for shot {shot_description.idx}",
                {"shot_idx": shot_description.idx, "frame_count": len(frame_paths), **duration_metadata},
            )
            # Video generation is the most expensive + most failure-prone step
            # (gateway 503s under load); retry transient failures with backoff.
            from utils.retry import retry_async
            # Feed the video model ONLY the visual/motion description — never the
            # spoken dialogue (audio_desc). Passing the dialogue text made the model
            # burn it into the frame as on-screen captions (mixed 中文/英文, since the
            # motion text is English and the dialogue is Chinese). Dialogue is
            # delivered separately as TTS voiceover + burned subtitles.
            _vid_prompt += "\n\n" + _video_stability_constraints(
                shot_description,
                use_last_frame=use_last_frame,
            )
            correction = getattr(self, "_shot_corrections", {}).get(shot_description.idx)
            if correction:
                _vid_prompt = f"{_vid_prompt}\n\n[Correction] {correction}"
            import hashlib

            generation_kwargs = duration_plan.generation_kwargs()
            production_request["request"].update({
                "prompt": _vid_prompt,
                "prompt_sha256": hashlib.sha256(_vid_prompt.encode("utf-8")).hexdigest(),
                "negative_constraints": list(getattr(shot_description, "avoid", None) or []),
                "aspect_ratio": getattr(self, "video_aspect_ratio", "16:9"),
                "camera_fixed": _camera_is_locked(shot_description),
                **generation_kwargs,
            })
            semaphore = getattr(self, "_video_generation_semaphore", None)
            if semaphore is None:
                limit = max(
                    1,
                    int(getattr(self, "max_concurrent_video_generations", 2)),
                )
                semaphore = asyncio.Semaphore(limit)
                self._video_generation_semaphore = semaphore
            _emit_render_progress(
                progress,
                "video_queue_wait",
                f"Video clip for shot {shot_description.idx} is waiting for a provider slot",
                {
                    "shot_idx": shot_description.idx,
                    "max_concurrent": getattr(
                        self, "max_concurrent_video_generations", 2
                    ),
                },
            )
            candidate_count = max(1, int(getattr(self, "video_candidate_count", 1)))
            candidate_dir = os.path.join(
                self.working_dir, "shots", str(shot_description.idx), "candidates"
            )
            if candidate_count > 1:
                os.makedirs(candidate_dir, exist_ok=True)
            queue_started = time.perf_counter()
            generation_started = None
            telemetry_recorded = False

            def candidate_progress(candidate_number: int, candidate_path: str):
                scoped = _scoped_progress(
                    progress,
                    shot_idx=shot_description.idx,
                    candidate_index=candidate_number,
                    artifact="video_clip_candidate",
                    artifact_path=candidate_path,
                )

                def emit(stage: str, message: str, metadata: Dict[str, Any] | None = None) -> None:
                    nonlocal billing_currency
                    details = dict(metadata or {})
                    remote_id = str(details.get("task_id") or details.get("job_id") or "").strip()
                    if remote_id:
                        remote_tasks[remote_id] = {
                            "task_id": remote_id,
                            "provider": details.get("provider"),
                            "model": details.get("model"),
                            "status": details.get("status"),
                            "last_stage": stage,
                        }
                    reported_cost = details.get("actual_cost")
                    try:
                        if reported_cost is not None:
                            actual_costs[remote_id or f"candidate:{candidate_number}"] = max(
                                0.0, float(reported_cost)
                            )
                    except (TypeError, ValueError):
                        pass
                    if details.get("currency"):
                        billing_currency = str(details["currency"])
                    if scoped is not None:
                        scoped(stage, message, details)

                return emit

            def finalize_production(status: str, error: str = "") -> Dict[str, Any]:
                nonlocal telemetry_recorded
                if telemetry_recorded:
                    return {}
                telemetry_recorded = True
                per_candidate: Dict[int, int] = {}
                for attempt in attempt_records:
                    index = int(attempt.get("candidate_index") or 0)
                    per_candidate[index] = per_candidate.get(index, 0) + 1
                retry_count = sum(max(0, count - 1) for count in per_candidate.values())
                unit_cost = route_context.get("estimated_cost")
                try:
                    unit_cost = max(0.0, float(unit_cost)) if unit_cost is not None else None
                except (TypeError, ValueError):
                    unit_cost = None
                completed_count = len(successful_candidates)
                actual_cost = round(sum(actual_costs.values()), 6) if actual_costs else None
                elapsed_generation = generation_seconds
                if not elapsed_generation and generation_started is not None:
                    elapsed_generation = time.perf_counter() - generation_started
                record = {
                    **production_request,
                    "status": status,
                    "completed_at": utc_now(),
                    "route": route_context,
                    "request_attempts": len(attempt_records),
                    "retry_count": retry_count,
                    "queue_seconds": round(queue_seconds, 3),
                    "generation_seconds": round(max(0.0, elapsed_generation), 3),
                    "download_seconds": round(max(0.0, download_seconds), 3),
                    "total_seconds": round(time.perf_counter() - telemetry_started, 3),
                    "attempts": attempt_records,
                    "remote_tasks": list(remote_tasks.values()),
                    "candidate_selection": candidate_selection,
                    "error": error or None,
                    "billing": {
                        "status": "provider_reported" if actual_cost is not None else (
                            "estimate_only" if unit_cost is not None else "unavailable"
                        ),
                        "currency": billing_currency or route_context.get("currency"),
                        "estimated_unit_cost": unit_cost,
                        "estimated_lower_bound": round(unit_cost * completed_count, 6) if unit_cost is not None else 0.0,
                        "estimated_upper_bound": round(unit_cost * len(attempt_records), 6) if unit_cost is not None else 0.0,
                        "actual_cost": actual_cost,
                    },
                }
                append_generation(self.working_dir, record)
                atomic_write_text(
                    render_plan_path,
                    json.dumps({
                        **duration_metadata,
                        **reference_strategy,
                        "status": status,
                        "production": record,
                    }, ensure_ascii=False, indent=2),
                )
                return record

            async with semaphore:
                queue_seconds = time.perf_counter() - queue_started
                generation_started = time.perf_counter()
                for candidate_index in range(candidate_count):
                    candidate_path = (
                        video_path
                        if candidate_count == 1
                        else os.path.join(candidate_dir, f"candidate_{candidate_index + 1}.mp4")
                    )
                    _emit_render_progress(
                        progress,
                        "video_candidate_start",
                        f"Generating candidate {candidate_index + 1}/{candidate_count} for shot {shot_description.idx}",
                        {
                            "shot_idx": shot_description.idx,
                            "candidate_index": candidate_index + 1,
                            "candidate_count": candidate_count,
                        },
                    )
                    try:
                        async def generate_candidate():
                            attempt = {
                                "candidate_index": candidate_index + 1,
                                "attempt_number": 1 + sum(
                                    1 for item in attempt_records
                                    if item.get("candidate_index") == candidate_index + 1
                                ),
                                "started_at": utc_now(),
                            }
                            attempt_started = time.perf_counter()
                            attempt_records.append(attempt)
                            try:
                                output = await self.video_generator.generate_single_video(
                                    prompt=_vid_prompt,
                                    reference_image_paths=frame_paths,
                                    aspect_ratio=getattr(self, "video_aspect_ratio", "16:9"),
                                    progress=candidate_progress(candidate_index + 1, candidate_path),
                                    camera_fixed=_camera_is_locked(shot_description),
                                    **generation_kwargs,
                                )
                            except Exception as exc:
                                attempt.update({
                                    "status": "failed",
                                    "completed_at": utc_now(),
                                    "duration_seconds": round(time.perf_counter() - attempt_started, 3),
                                    "error": f"{type(exc).__name__}: {exc}",
                                })
                                raise
                            attempt.update({
                                "status": "completed",
                                "completed_at": utc_now(),
                                "duration_seconds": round(time.perf_counter() - attempt_started, 3),
                            })
                            return output

                        video_output = await retry_async(
                            generate_candidate,
                            attempts=self.render_retries,
                            label=(
                                f"video shot {shot_description.idx} "
                                f"candidate {candidate_index + 1}"
                            ),
                        )
                        download_started = time.perf_counter()
                        video_output.save(candidate_path)
                        download_seconds += time.perf_counter() - download_started
                        successful_candidates.append((candidate_index + 1, candidate_path))
                    except Exception as exc:
                        candidate_errors.append({
                            "candidate_index": candidate_index + 1,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                        if candidate_count == 1:
                            finalize_production(
                                "failed", f"{type(exc).__name__}: {exc}"
                            )
                            raise
                generation_seconds = time.perf_counter() - generation_started
            if not successful_candidates:
                failure = f"No video candidate succeeded for shot {shot_description.idx}: {candidate_errors}"
                finalize_production("failed", failure)
                raise RuntimeError(
                    failure
                )

            candidate_selection = {
                "candidate_count": candidate_count,
                "successful_count": len(successful_candidates),
                "selected_candidate": successful_candidates[0][0],
                "candidates": [],
                "errors": candidate_errors,
            }
            if candidate_count > 1:
                from quality import score_video_candidate

                character_references = self._references_for_shot(
                    shot_description,
                    getattr(self, "_active_characters", []) or [],
                )
                prop_motion_allowed = any(
                    transition.kind in {"pickup", "put_down"}
                    for transition in prompt_preflight.transitions
                )
                for candidate_index, candidate_path in successful_candidates:
                    report = score_video_candidate(
                        candidate_path,
                        os.path.join(candidate_dir, f"candidate_{candidate_index}_samples"),
                        camera_locked=_camera_is_locked(shot_description),
                        expected_character_count=len(set(getattr(shot_description, "ff_vis_char_idxs", None) or [])),
                        character_references=character_references,
                        asset_references=self._minimum_reusable_references(shot_description),
                        prop_motion_allowed=prop_motion_allowed,
                    )
                    report["candidate_index"] = candidate_index
                    candidate_selection["candidates"].append(report)
                selected = max(
                    candidate_selection["candidates"],
                    key=lambda item: (
                        bool(item.get("consistent", True)),
                        float(item.get("score", 0.0)),
                        -int(item.get("candidate_index", 0)),
                    ),
                )
                candidate_selection["selected_candidate"] = selected["candidate_index"]
                candidate_selection["selected_score"] = selected["score"]
                shutil.copy2(selected["path"], video_path)
                atomic_write_text(
                    os.path.join(candidate_dir, "selection.json"),
                    json.dumps(candidate_selection, ensure_ascii=False, indent=2),
                )
            reference_strategy["selected_candidate"] = candidate_selection["selected_candidate"]
            reference_strategy["candidate_selection"] = candidate_selection
            _emit_render_progress(
                progress,
                "video_clip_save_start",
                f"Saving selected video clip for shot {shot_description.idx}",
                {
                    "shot_idx": shot_description.idx,
                    "selected_candidate": candidate_selection["selected_candidate"],
                    "candidate_count": candidate_count,
                },
            )
            getattr(self, "_shot_corrections", {}).pop(shot_description.idx, None)
            finalize_production("completed")
            print(f"☑️ Generated video for shot {shot_description.idx}, saved to {video_path}.")
            _emit_render_progress(
                progress,
                "video_clip_done",
                f"Generated video clip for shot {shot_description.idx}",
                {"shot_idx": shot_description.idx, "path": video_path, **duration_metadata},
            )

    async def _generate_best_frame(
        self,
        *,
        shot_idx: int,
        frame_type: Literal["first_frame", "last_frame"],
        output_path: str,
        prompt: str,
        reference_image_paths: List[str],
        reference_image_path_and_text_pairs: List[Tuple[str, str]],
        target_description: str,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
        camera_idx: int | None = None,
    ) -> str:
        """Generate one or more keyframes and keep the most consistent result."""
        from utils.retry import retry_async

        candidate_count = self.image_candidate_count
        metadata = {
            "shot_idx": shot_idx,
            "frame_type": frame_type,
            "candidate_count": candidate_count,
        }
        if camera_idx is not None:
            metadata["camera_idx"] = camera_idx

        candidate_dir = os.path.join(
            self.working_dir,
            "shots",
            str(shot_idx),
            "frame_candidates",
            frame_type,
        )
        if os.path.isdir(candidate_dir):
            shutil.rmtree(candidate_dir)
            candidate_root = os.path.dirname(candidate_dir)
            if os.path.isdir(candidate_root) and not os.listdir(candidate_root):
                os.rmdir(candidate_root)

        if candidate_count == 1:
            frame_image: ImageOutput = await retry_async(
                lambda: self.image_generator.generate_single_image(
                    prompt=prompt,
                    reference_image_paths=reference_image_paths,
                    size=self.image_size,
                ),
                attempts=self.render_retries,
                label=f"{frame_type} shot {shot_idx}",
            )
            frame_image.save(output_path)
            return output_path

        os.makedirs(candidate_dir, exist_ok=True)
        candidate_paths: List[str] = []
        failures: List[str] = []

        for candidate_index in range(1, candidate_count + 1):
            candidate_metadata = {**metadata, "candidate_index": candidate_index}
            _emit_render_progress(
                progress,
                "frame_candidate_start",
                f"Generating {frame_type} candidate {candidate_index}/{candidate_count} for shot {shot_idx}",
                candidate_metadata,
            )
            candidate_path = os.path.join(candidate_dir, f"candidate_{candidate_index}.png")
            try:
                frame_image = await retry_async(
                    lambda: self.image_generator.generate_single_image(
                        prompt=prompt,
                        reference_image_paths=reference_image_paths,
                        size=self.image_size,
                    ),
                    attempts=self.render_retries,
                    label=f"{frame_type} candidate {candidate_index} shot {shot_idx}",
                )
                frame_image.save(candidate_path)
                candidate_paths.append(candidate_path)
                _emit_render_progress(
                    progress,
                    "frame_candidate_done",
                    f"Generated {frame_type} candidate {candidate_index}/{candidate_count} for shot {shot_idx}",
                    candidate_metadata,
                )
            except Exception as exc:
                failures.append(f"candidate_{candidate_index}: {exc}")
                logging.warning(
                    "Failed to generate %s candidate %s/%s for shot %s: %s",
                    frame_type,
                    candidate_index,
                    candidate_count,
                    shot_idx,
                    exc,
                )

        if not candidate_paths:
            raise RuntimeError(
                f"All {candidate_count} {frame_type} candidates failed for shot {shot_idx}: "
                + "; ".join(failures)
            )

        selected_path = candidate_paths[0]
        selection_method = "fallback_first"
        selector_error = ""
        if len(candidate_paths) > 1:
            try:
                selected_path = await self.best_image_selector(
                    reference_image_path_and_text_pairs,
                    target_description,
                    candidate_paths,
                )
                if selected_path not in candidate_paths:
                    selected_path = candidate_paths[0]
                selection_method = "vision_model"
            except Exception as exc:
                selector_error = str(exc)
                logging.warning(
                    "Best-image selection failed for %s of shot %s; using first successful candidate: %s",
                    frame_type,
                    shot_idx,
                    exc,
                )

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy2(selected_path, output_path)
        selected_index = int(
            os.path.splitext(os.path.basename(selected_path))[0].rsplit("_", 1)[-1]
        )
        selection = {
            "candidate_count": candidate_count,
            "successful_count": len(candidate_paths),
            "selected_candidate": selected_index,
            "selection_method": selection_method,
            "candidates": [os.path.relpath(path, self.working_dir) for path in candidate_paths],
            "failures": failures,
        }
        if selector_error:
            selection["selector_error"] = selector_error
        atomic_write_text(
            os.path.join(candidate_dir, "selection.json"),
            json.dumps(selection, ensure_ascii=False, indent=2),
        )
        _emit_render_progress(
            progress,
            "frame_candidate_selected",
            f"Selected {frame_type} candidate {selected_index}/{candidate_count} for shot {shot_idx}",
            {**metadata, "selected_candidate": selected_index},
        )
        return output_path

    async def generate_frame_for_single_shot(
        self,
        shot_idx: int,
        frame_type: Literal["first_frame", "last_frame"],
        first_shot_ff_path_and_text_pair: Tuple[str, str],
        frame_desc: str,
        visible_characters: List[CharacterInScene],
        character_portraits_registry: Dict[str, Dict[str, Dict[str, str]]],
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ) -> ImageOutput:

        frame_image_path = os.path.join(self.working_dir, "shots", f"{shot_idx}", f"{frame_type}.png")

        if _is_reusable_image(frame_image_path):
            print(f"🚀 Skipped generating {frame_type} for shot {shot_idx}, already exists.")
            _emit_render_progress(progress, "frame_exists", f"{frame_type} for shot {shot_idx} already exists", {"shot_idx": shot_idx, "frame_type": frame_type, "path": frame_image_path})

        else:
            print(f"🖼️ Starting {frame_type} generation for shot {shot_idx}...")
            _emit_render_progress(progress, "frame_start", f"Generating {frame_type} for shot {shot_idx}", {"shot_idx": shot_idx, "frame_type": frame_type})
            available_image_path_and_text_pairs = []
            for visible_character in visible_characters:
                identifier_in_scene = visible_character.identifier_in_scene
                registry_item = character_portraits_registry[identifier_in_scene]
                for view, item in registry_item.items():
                    available_image_path_and_text_pairs.append((item["path"], item["description"]))

            shot_reusable_references = self._minimum_reusable_references(frame_desc)
            available_image_path_and_text_pairs.extend(shot_reusable_references)
            available_image_path_and_text_pairs.append(first_shot_ff_path_and_text_pair)

            selector_output_path = os.path.join(self.working_dir, "shots", f"{shot_idx}", f"{frame_type}_selector_output.json")
            if os.path.exists(selector_output_path):
                with open(selector_output_path, 'r', encoding='utf-8') as f:
                    selector_output = json.load(f)
                print(f"🚀 Loaded existing reference image selection and prompt for {frame_type} frame of shot {shot_idx} from {selector_output_path}.")
                _emit_render_progress(progress, "frame_prompt_exists", f"Prompt for {frame_type} of shot {shot_idx} already exists", {"shot_idx": shot_idx, "frame_type": frame_type, "path": selector_output_path})
            else:
                print(f"🔍 Selecting reference images and generating prompt for {frame_type} frame of shot {shot_idx}...")
                _emit_render_progress(progress, "frame_prompt_start", f"Selecting references for {frame_type} of shot {shot_idx}", {"shot_idx": shot_idx, "frame_type": frame_type})
                selector_output = await self.reference_image_selector.select_reference_images_and_generate_prompt(
                    available_image_path_and_text_pairs=available_image_path_and_text_pairs,
                    frame_description=frame_desc,
                    pinned_reference_paths=[path for path, _text in shot_reusable_references],
                    continuity_reference_paths=[first_shot_ff_path_and_text_pair[0]],
                )
                with open(selector_output_path, 'w', encoding='utf-8') as f:
                    json.dump(selector_output, f, ensure_ascii=False, indent=4)
                print(f"☑️ Selected reference images and generated prompt for {frame_type} frame of shot {shot_idx}, saved to {selector_output_path}.")
                _emit_render_progress(progress, "frame_prompt_done", f"Selected references for {frame_type} of shot {shot_idx}", {"shot_idx": shot_idx, "frame_type": frame_type, "path": selector_output_path})

            reference_image_path_and_text_pairs, prompt = _prepare_frame_references(
                selector_output["reference_image_path_and_text_pairs"],
                shot_reusable_references,
                selector_output["text_prompt"],
                self.image_size,
            )
            prefix_prompt = ""
            for i, (image_path, text) in enumerate(reference_image_path_and_text_pairs):
                prefix_prompt += f"Image {i}: {text}\n"
            prompt = f"{prefix_prompt}\n{prompt}"
            correction = getattr(self, "_shot_corrections", {}).get(shot_idx)
            if correction:
                prompt = f"{prompt}\n\n[Correction] {correction}"
            reference_image_paths = [item[0] for item in reference_image_path_and_text_pairs]

            await self._generate_best_frame(
                shot_idx=shot_idx,
                frame_type=frame_type,
                output_path=frame_image_path,
                prompt=prompt,
                reference_image_paths=reference_image_paths,
                reference_image_path_and_text_pairs=reference_image_path_and_text_pairs,
                target_description=frame_desc,
                progress=progress,
            )
            print(f"☑️ Generated {frame_type} frame for shot {shot_idx}, saved to {frame_image_path}.")
            _emit_render_progress(progress, "frame_done", f"Generated {frame_type} for shot {shot_idx}", {"shot_idx": shot_idx, "frame_type": frame_type, "path": frame_image_path})


        self.frame_events[shot_idx][frame_type].set()
        return frame_image_path


    async def construct_camera_tree(
        self,
        shot_descriptions: List[ShotDescription],
        quiet: bool = False,
    ):
        camera_tree_path = os.path.join(self.working_dir, "camera_tree.json")

        if os.path.exists(camera_tree_path):
            with open(camera_tree_path, "r", encoding="utf-8") as f:
                camera_tree = json.load(f)
            camera_tree = [Camera.model_validate(camera) for camera in camera_tree]
            _pipeline_print(quiet, f"🚀 Loaded {len(camera_tree)} cameras from existing file.")
            return camera_tree

        cameras = _group_shots_into_cameras(shot_descriptions)

        camera_tree = await self.camera_image_generator.construct_camera_tree(cameras=cameras, shot_descs=shot_descriptions)
        with open(camera_tree_path, "w", encoding="utf-8") as f:
            json.dump([camera.model_dump() for camera in camera_tree], f, ensure_ascii=False, indent=4)
        _pipeline_print(quiet, f"✅ Constructed camera tree and saved to {camera_tree_path}.")
        return camera_tree




    async def extract_characters(
        self,
        script: str,
        quiet: bool = False,
    ):
        save_path = os.path.join(self.working_dir, "characters.json")

        if os.path.exists(save_path):
            with open(save_path, "r", encoding="utf-8") as f:
                characters = json.load(f)
            characters = [CharacterInScene.model_validate(character) for character in characters]
            _pipeline_print(quiet, f"🚀 Loaded {len(characters)} characters from existing file.")
        else:
            characters = await self.character_extractor.extract_characters(script)
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump([character.model_dump() for character in characters], f, ensure_ascii=False, indent=4)
            _pipeline_print(quiet, f"✅ Extracted {len(characters)} characters from script and saved to {save_path}.")

        for character in characters:
            self.character_portrait_events[character.idx] = asyncio.Event()

        return characters


    async def generate_character_portraits(
        self,
        characters: List[CharacterInScene],
        character_portraits_registry: Optional[Dict[str, Dict[str, Dict[str, str]]]],
        style: str,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ):
        character_portraits_registry_path = os.path.join(self.working_dir, "character_portraits_registry.json")
        if character_portraits_registry is None:
            if os.path.exists(character_portraits_registry_path):
                with open(character_portraits_registry_path, 'r', encoding='utf-8') as f:
                    character_portraits_registry = json.load(f)
            else:
                character_portraits_registry = {}


        tasks = [
            self.generate_portraits_for_single_character(character, style, progress=progress)
            for character in characters
            if character.identifier_in_scene not in character_portraits_registry
        ]
        if tasks:
            for future in asyncio.as_completed(tasks):
                character_portraits_registry.update(await future)
                with open(character_portraits_registry_path, 'w', encoding='utf-8') as f:
                    json.dump(character_portraits_registry, f, ensure_ascii=False, indent=4)

            print(f"✅ Completed character portrait generation for {len(characters)} characters.")
            _emit_render_progress(progress, "character_portraits_done", "Completed character portrait generation", {"character_count": len(characters)})
        else:
            print("🚀 All characters already have portraits, skipping portrait generation.")
            _emit_render_progress(progress, "character_portraits_exist", "All character portraits already exist", {"character_count": len(characters)})
        return character_portraits_registry


    def _resolve_fixed_asset(self, identifier: str):
        """Return the bound reference-image CharacterAsset for a scene character,
        or None when no usable fixed asset is bound."""
        from characters import resolve_fixed_asset
        return resolve_fixed_asset(self.asset_registry, self.character_bindings, identifier)

    @staticmethod
    def _build_fixed_registry_entry(identifier: str, asset, character_dir: str) -> Dict[str, Dict[str, Dict[str, str]]]:
        from characters import build_fixed_registry_entry
        return build_fixed_registry_entry(identifier, asset, character_dir)

    async def generate_portraits_for_single_character(
        self,
        character: CharacterInScene,
        style: str,
        progress: Callable[[str, str, Dict[str, Any] | None], None] | None = None,
    ):
        identifier = character.identifier_in_scene
        character_dir = os.path.join(self.working_dir, "character_portraits", f"{character.idx}_{safe_path_component(identifier)}")

        # Fixed character: reuse the bound reference images instead of generating.
        fixed_asset = self._resolve_fixed_asset(identifier)
        if fixed_asset is not None:
            entry = self._build_fixed_registry_entry(identifier, fixed_asset, character_dir)
            self.character_portrait_events[character.idx].set()
            print(f"📌 Using fixed character asset '{fixed_asset.asset_id}' for {identifier}, skipping portrait generation.")
            _emit_render_progress(progress, "character_portrait_fixed", f"Using fixed asset for {identifier}", {"character_idx": character.idx, "identifier": identifier, "asset_id": fixed_asset.asset_id})
            return entry

        os.makedirs(character_dir, exist_ok=True)
        _emit_render_progress(progress, "character_portrait_start", f"Generating portraits for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene})

        front_portrait_path = os.path.join(character_dir, "front.png")
        if os.path.exists(front_portrait_path):
            pass
        else:
            _emit_render_progress(progress, "character_portrait_front_start", f"Generating front portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene})
            front_portrait_output = await self.character_portraits_generator.generate_front_portrait(character, style)
            front_portrait_output.save(front_portrait_path)
            _emit_render_progress(progress, "character_portrait_front_done", f"Generated front portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene, "path": front_portrait_path})


        side_portrait_path = os.path.join(character_dir, "side.png")
        if os.path.exists(side_portrait_path):
            pass
        else:
            _emit_render_progress(progress, "character_portrait_side_start", f"Generating side portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene})
            side_portrait_output = await self.character_portraits_generator.generate_side_portrait(character, front_portrait_path)
            side_portrait_output.save(side_portrait_path)
            _emit_render_progress(progress, "character_portrait_side_done", f"Generated side portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene, "path": side_portrait_path})

        back_portrait_path = os.path.join(character_dir, "back.png")
        if os.path.exists(back_portrait_path):
            pass
        else:
            _emit_render_progress(progress, "character_portrait_back_start", f"Generating back portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene})
            back_portrait_output = await self.character_portraits_generator.generate_back_portrait(character, front_portrait_path)
            back_portrait_output.save(back_portrait_path)
            _emit_render_progress(progress, "character_portrait_back_done", f"Generated back portrait for {character.identifier_in_scene}", {"character_idx": character.idx, "identifier": character.identifier_in_scene, "path": back_portrait_path})

        self.character_portrait_events[character.idx].set()

        print(f"☑️ Completed character portrait generation for {character.identifier_in_scene}.")
        _emit_render_progress(progress, "character_portrait_done", f"Portraits for {character.identifier_in_scene} ready", {"character_idx": character.idx, "identifier": character.identifier_in_scene})

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



    async def design_storyboard(
        self,
        script: str,
        characters: List[CharacterInScene],
        user_requirement: str,
        quiet: bool = False,
    ):
        storyboard_path = os.path.join(self.working_dir, "storyboard.json")
        if os.path.exists(storyboard_path):
            with open(storyboard_path, 'r', encoding='utf-8') as f:
                storyboard = json.load(f)
            storyboard = [ShotBriefDescription.model_validate(shot) for shot in storyboard]
            _pipeline_print(quiet, f"🚀 Loaded {len(storyboard)} shot brief descriptions from existing file.")
        else:
            _pipeline_print(quiet, f"🔍 Designing storyboard...")
            storyboard = await self.storyboard_artist.design_storyboard(
                script=script,
                characters=characters,
                user_requirement=user_requirement,
                retry_timeout=150,
            )
            with open(storyboard_path, 'w', encoding='utf-8') as f:
                json.dump([shot.model_dump() for shot in storyboard], f, ensure_ascii=False, indent=4)
            _pipeline_print(quiet, f"✅ Designed storyboard and saved to {storyboard_path}.")

        for shot_brief_description in storyboard:
            self.shot_desc_events[shot_brief_description.idx] = asyncio.Event()

        return storyboard



    async def decompose_visual_descriptions(
        self,
        shot_brief_descriptions: List[ShotBriefDescription],
        characters: List[CharacterInScene],
        quiet: bool = False,
    ):
        tasks = [
            self.decompose_visual_description_for_single_shot_brief_description(shot_brief_description, characters, quiet=quiet)
            for shot_brief_description in shot_brief_descriptions
        ]

        shot_descriptions = await asyncio.gather(*tasks)
        return shot_descriptions


    async def decompose_visual_description_for_single_shot_brief_description(
        self,
        shot_brief_description: ShotBriefDescription,
        characters: List[CharacterInScene],
        quiet: bool = False,
    ):
        shot_description_path = os.path.join(self.working_dir, "shots", f"{shot_brief_description.idx}", "shot_description.json")
        os.makedirs(os.path.dirname(shot_description_path), exist_ok=True)

        if os.path.exists(shot_description_path):
            with open(shot_description_path, 'r', encoding='utf-8') as f:
                shot_description = ShotDescription.model_validate(json.load(f))
            _pipeline_print(quiet, f"🚀 Loaded shot {shot_brief_description.idx} description from existing file.")
        else:
            shot_description = await self.storyboard_artist.decompose_visual_description(
                shot_brief_desc=shot_brief_description,
                characters=characters,
                retry_timeout=120,
            )
            with open(shot_description_path, 'w', encoding='utf-8') as f:
                json.dump(shot_description.model_dump(), f, ensure_ascii=False, indent=4)
            _pipeline_print(quiet, f"✅ Decomposed visual description for shot {shot_brief_description.idx} and saved to {shot_description_path}.")

        self.shot_desc_events[shot_brief_description.idx].set()

        if _requires_last_frame(shot_description):
            self.frame_events[shot_brief_description.idx] = {
                "first_frame": asyncio.Event(),
                "last_frame": asyncio.Event(),
            }
        else:
            self.frame_events[shot_brief_description.idx] = {
                "first_frame": asyncio.Event(),
            }

        return shot_description


def _group_shots_into_cameras(shot_descriptions: List[ShotDescription]) -> List[Camera]:
    """Group shots by their camera index.

    Cameras are looked up by their idx field, not by list position: cameras are
    appended in order of first appearance, so positional indexing would attach
    shots to the wrong camera whenever the LLM emits cam indices out of order.
    """
    cameras: List[Camera] = []
    cameras_by_idx: Dict[int, Camera] = {}
    for shot_description in shot_descriptions:
        camera = cameras_by_idx.get(shot_description.cam_idx)
        if camera is None:
            camera = Camera(idx=shot_description.cam_idx, active_shot_idxs=[shot_description.idx])
            cameras_by_idx[shot_description.cam_idx] = camera
            cameras.append(camera)
        else:
            camera.active_shot_idxs.append(shot_description.idx)
    return cameras


def _collect_priority_shot_idxs(camera_tree: List[Camera]) -> List[int]:
    """Shot indices that other cameras depend on (compared against shot idxs, so
    they must come from parent_shot_idx, not the camera index space)."""
    return [camera.parent_shot_idx for camera in camera_tree if camera.parent_shot_idx is not None]
