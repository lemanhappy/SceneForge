from typing import List, Optional, Literal
import asyncio
import re
from pydantic import BaseModel, Field
from langchain.chat_models.base import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from interfaces import CharacterInScene, PerformanceBeat, ShotDescription, ShotBriefDescription
from prompting import compile_video_prompt

from utils.retry import llm_retry



system_prompt_template_design_storyboard = \
"""
[Role]
You are a professional storyboard artist with the following core skills:
- Script Analysis: Ability to quickly interpret a script's text, identifying the setting, character actions, dialogue, emotions, and narrative pacing.
- Visualization: Expertise in translating written descriptions into visual frames, including composition, lighting, and spatial arrangement.
- Storyboarding: Proficiency in cinematic language, such as shot types (e.g., close-up, medium shot, wide shot), camera angles (e.g., high angle, eye-level), camera movements (e.g., zoom, pan), and transitions.
- Narrative Continuity: Ability to ensure the storyboard sequence is logically smooth, highlights key plot points, and maintains emotional consistency.
- Technical Knowledge: Understanding of basic storyboard formats and industry standards, such as using numbered shots and concise descriptions.

[Task]
Your task is to design a complete storyboard based on a user-provided script (which contains only one scene). The storyboard should be presented in text form, clearly displaying the visual elements and narrative flow of each shot to help the user visualize the scene.

[Input]
The user will provide the following input.
- Script:A complete scene script containing dialogue, action descriptions, and scene settings. The script focuses on only one scene; there is no need to handle multiple scene transitions. The script input is enclosed within <SCRIPT> and </SCRIPT>.
- Characters List: A list describing basic information for each character, such as name, personality traits, appearance (if relevant). The character list is enclosed within <CHARACTERS> and </CHARACTERS>.
- User requirement: The user requirement (optional) is enclosed within <USER_REQUIREMENT> and </USER_REQUIREMENT>, which may include:
    - Target audience (e.g., children, teenagers, adults).
    - Storyboard style (e.g., realistic, cartoon, abstract).
    - Desired number of shots (e.g., "not more than 10 shots").
    - Other specific instructions (e.g., emphasize the characters' actions).

[Output]
{format_instructions}

[Guidelines]
- By default, output values (except keys) match the language used in the script — UNLESS an explicit per-field language directive is given below (it overrides this).
- Each shot must have a clear narrative purpose—such as establishing the setting, showing character relationships, or highlighting reactions.
- Use cinematic language deliberately: close-ups for emotion, wide shots for context, and varied angles to direct audience attention.
- When designing a new shot, first consider whether it can be filmed using an existing camera position. Introduce a new one only if the shot size, angle, and focus differ significantly. If the camera undergoes significant movement, it cannot be used thereafter.
- Keep character names in visual descriptions and speaker fields consistent with the character list. In visual descriptions, enclose names in angle brackets (e.g., <Alice>), but not in dialogue or speaker fields.
- When describing visual elements, it is necessary to indicate the position of the element within the frame. For example, Character A is on the left side of the frame, facing toward the right, with a table in front of him. The table is positioned slightly to the left of the center of the frame. Ensure that invisible elements are not included. For instance, do not describe someone behind a closed door if they cannot be seen.
- Avoid unsafe content (violence, discrimination, etc.) in visual descriptions. Use indirect methods like sound or suggestive imagery when needed, and substitute sensitive elements (e.g., ketchup for blood).
- Assign at most one dialogue line per character per shot. Each line of dialogue should correspond to a shot.
- Each shot requires an independent description without reference to each other.
- When the shot focuses on a character, describe which specific body part the focus is on.
- When describing a character, it is necessary to indicate the direction they are facing.
- Write director_desc as a detailed, user-facing director script. It must state the shot-relative time ranges and describe camera, gaze, breathing, posture, facial-muscle changes, swallowing, tears, lip movement, pauses, and action order whenever they carry the emotion. Keep the performance restrained and filmable rather than explaining an internal thought.
- Set duration_sec deliberately. Use 1-3 ordered, non-overlapping beats whose times are relative to the beginning of this shot and fit inside duration_sec. Do not overload a short shot: if more than three sequential actions are needed, lengthen the shot or split it into more shots.
- Give every prop exactly one state at the start of a shot: held by one character, resting on one support, or outside the frame. Never ask a character to pick up a prop that visual_desc already places in their hands; continue holding or carrying it instead.
- Choose exactly one camera behavior per shot. A locked/static/tripod shot must not also contain dolly, pan, tilt, zoom, tracking, orbit, crane, or handheld movement in visual_desc or beats.camera.
- Treat each beat action as a state transition with a filmable precondition. Enter requires the character to begin outside the visible room, pickup requires the prop to begin on a support, put-down requires the character to hold it, and opening/closing requires the opposite initial state.
- beats.action / beats.performance / beats.camera, visual_desc, visual_style, avoid, director_desc, and audio descriptions follow the target language of the script. For a Chinese target, use clear Simplified Chinese in every user-reviewable field.
- visual_style lists concrete photographic qualities such as lighting, depth of field, palette, lens feel, and atmosphere. avoid lists explicit unwanted performance, camera, edit, and motion behaviors.
- Keep literal dialogue out of visual_desc and beats. Put exact spoken words only in audio_desc; visual fields may describe lip movement, vocal effort, or pauses without quoting the line.
"""


human_prompt_template_design_storyboard = \
"""
<SCRIPT>
{script_str}
</SCRIPT>

<CHARACTERS>
{characters_str}
</CHARACTERS>

<USER_REQUIREMENT>
{user_requirement_str}
</USER_REQUIREMENT>
"""



system_prompt_template_decompose_visual_description = \
"""
[Role]
You are a professional visual text analyst, proficient in cinematic language and shot narration. Your expertise lies in deconstructing a comprehensive shot description accurately into three core components: the static first frame, the static last frame, and the dynamic motion that connects them.

[Task]
Your task is to dissect and rewrite a user-provided visual text description of a shot strictly and insightfully into three distinct parts:
- First Frame Description: Describe the static image at the very beginning of the shot. Focus on compositional elements, initial character postures, environmental layout, lighting, color, and other static visual aspects.
- Last Frame Description: Describe the static image at the very end of the shot. Similarly, focus on the static composition, but it must reflect the final state after changes caused by camera movement or internal element motion.
- Motion Description: Describe all movements that occur between the first frame and the last frame. This includes camera movement (e.g., static, push-in, pull-out, pan, track, follow, tilt, etc.) and movement of elements within the shot (e.g., character movement, object displacement, changes in lighting, etc.). This is the most dynamic part of the entire description. For the movement and changes of a character, you cannot directly use the character's name to refer to them. Instead, you need to refer to the character by their external features, especially noticeable ones like clothing characteristics.

[Input]
You will receive a single visual text description of a shot that typically implicitly or explicitly contains information about the starting state, the motion process, and the ending state.
Additionally, you will receive a sequence of potential characters, each containing an identifier and a feature.
- The description is enclosed within <VISUAL_DESC> and </VISUAL_DESC>.
- The character list is enclosed within <CHARACTERS> and </CHARACTERS>.


[Output]
{format_instructions}

[Guidelines]
- By default, output values (except keys) match the language used in the script — UNLESS an explicit per-field language directive is given below (it overrides this).
- Ensure the first and last frame descriptions are pure "snapshots," containing no ongoing actions (e.g., "He is about to stand up" is unacceptable; it should be "He is sitting on the chair, leaning slightly forward").
- Treat ff_desc as the exact time-zero state before the motion begins. Do not schedule an entrance, arrival, or walk-in action when that character is already visible in ff_vis_char_idxs: show the character just inside the threshold and start motion_desc with the next small action (stop, look, turn, breathe). This prevents the video model from generating a second copy. If the physical crossing is essential, make it a separate shot whose first frame is unambiguously before the crossing. For other actions, show exactly one character/object at the earliest starting position, never both the pre-action and post-action state. Do not use ongoing verbs such as "enters" or "walks in" inside ff_desc.
- Every character visible in ff_desc must correspond to one ff_vis_char_idxs entry, and each entry means exactly one on-screen instance. Never plan clones, double exposure, ghost trails, or a second copy of the same character.
- Give every prop exactly one time-zero state in ff_desc. If it is already held, motion_desc must continue holding/carrying it and must not repeat pickup. If motion_desc picks it up, ff_desc must place that single prop on a visible support and the hand must begin empty.
- Use exactly one camera mode. Do not combine static/fixed/locked/tripod language with dolly, pan, tilt, zoom, tracking, orbit, crane, or handheld movement. A focus pull is allowed with a locked camera because it changes focus rather than camera position.
- Validate action preconditions before writing motion_desc: entry starts offscreen/outside, exit starts visible/inside, pickup starts with the prop on a support, put-down starts with the prop held, open starts closed, and close starts open.
- In the motion description, you must clearly distinguish between camera movement and on-screen movement. Use professional cinematic terminology (e.g., dolly shot, pan, zoom, etc.) as precisely as possible to describe camera movement.
- In the motion description, you cannot directly use character names to refer to characters; instead, you should use the characters' visible characteristics to refer to them. For example, "Alice is walking" is unacceptable; it should be "Alice (short hair, wearing a green dress) is walking".
- The last frame description must be logically consistent with the first frame description and the motion description. All actions described in the motion section should be reflected in the static image of the last frame.
- If the input description is ambiguous about certain details, you may make reasonable inferences and additions based on the context to make all three sections complete and fluent. However, core elements must strictly adhere to the input text.
- Use accurate, concise, and professional descriptive language. Avoid overly literary rhetoric such as metaphors or emotional flourishes; focus on providing information that can be visualized.
- Similar to the input visual description, the first and last frame descriptions should include details such as shot type, angle, composition, etc.
- Below are the three types of variation within a shot (not between two shots):
(1) 'large' cases typically involve the exaggerated transition shots which means a significant change in the composition and focus, such as smoothly changing from a wide shot to a close-up. It is usually accompanied by significant camera movement (e.g., drone perspective shots across the city).
(2) 'medium' cases often involve the introduction of new characters and a character turns from the back to face the front (facing the camera).
(3) 'small' cases usually involve minor changes, such as expression changes, movement and pose changes of existing characters(e.g., walking, sitting down, standing up), moderate camera movements(e.g., pan, tilt, track).
- When describing a character, it is necessary to indicate the direction they are facing.
- The first shot must establish the overall scene environment, using the widest possible shot.
- Use as few camera positions as possible.
- Preserve every supplied timed beat, performance detail, style instruction, and avoid constraint in motion_desc. Do not summarize micro-performance directions away. Do not include literal spoken dialogue in motion_desc.
"""


human_prompt_template_decompose_visual_description = \
"""
<VISUAL_DESC>
{visual_desc}
</VISUAL_DESC>

<CHARACTERS>
{characters_str}
</CHARACTERS>
"""


class VisDescDecompositionResponse(BaseModel):
    ff_desc: str = Field(
        description="A detailed description of the first frame of the shot, capturing the initial visual elements and composition.",
        # examples=[
        #     "Medium shot of a supermarket aisle at eye level. Bob(a tall man wearing a blue shirt and jeans) is positioned on the right side of the frame, captured in profile and facing right, while Alice(a young woman with short hair, wearing a green dress) is on the left, shown pushing a shopping cart with her gaze lowered toward the ground. They are arranged in a front-to-back spatial relationship. Shelves line both sides of the frame, and cool-toned fluorescent lighting from above washes over the scene. The vibrant colors of product packaging contrast with the metallic gray of the shopping cart, all contained within a stable, horizontally balanced composition.",
        #     "Extreme long shot. Aerial view from hundreds of meters above the ground. The boundless golden desert resembles undulating frozen waves, occupying the vast majority of the frame. At the very center of the image, a tiny, solitary explorer appears only as a faint dark speck, dragging a long, lonely trail of footprints behind him, stretching all the way to the edge of the frame.",
        #     "Medium shot at eye level angle. Designer A(with a beard, wearing a white suit) leans forward passionately, speaking emphatically. Product Manager B(with a beard, wearing a white T-shirt) sits with crossed arms, looking skeptical. Between them, Development Engineer C(brown hair, wearing a blue T-shirt) appears anxious, glancing between the two. Project Manager D(curly hair, wearing a red T-shirt) prepares to mediate, focusing on a whiteboard. Bright overhead lighting highlights their expressions, with a blurred whiteboard and glass wall in the background.",
        #     "A low-angle close-up shot captures the figure from below, framing him from the chest up. His face appears resolute and commanding, his eyes piercing as he speaks passionately. Flecks of saliva are visible, emphasizing his intensity. The overcast sky breaks with occasional light, casting him as a heroic, almost monumental figure against the gloom.",
        #     "An extremely close-up of an old, motionless pocket watch. Soft light highlights scratches on its brass case and the enamel dial with Roman numerals. The second hand remains fixed at 'VIII', casting a sharp shadow. A wrinkled finger gently touches the glass surface, evoking a tangible sense of stillness and time.",
        #     "An over-the-shoulder shot at eye level, positioned behind Character A(red hair, wearing a white T-shirt). The foreground, including A's shoulder and head, is softly blurred, directing focus onto Character B(with a beard, wearing a white T-shirt)'s face. B's subtle reactions—shifting from surprise to confusion, then to a glimmer of understanding—are clearly visible. The café background is gently blurred with warm lighting.",
        # ]
    )
    ff_vis_char_idxs: List[int] = Field(
        description="A list of indices of characters that are visible in the first frame of the shot, corresponding to the character list provided in the input.",
        examples=[[0], [1], [0, 1], []]
    )
    lf_desc: str = Field(
        description="A detailed description of the last frame of the shot, capturing the concluding visual elements and composition.",
    )
    lf_vis_char_idxs: List[int] = Field(
        description="A list of indices of characters that are visible in the last frame of the shot, corresponding to the character list provided in the input.",
        examples=[[0], [1], [0, 1], []]
    )
    motion_desc: str = Field(
        description="The motion description of the shot. Describe the dynamic visual changes within the shot (camera movement and the movement of elements within the frame)",
        examples=[
            "Static camera. Alice (short hair, wearing a green dress) is walking towards the camera.",
            "Dolly in from meidum shot to close-up. Bob (with a beard, wearing a white T-shirt) smiles to the camera.",
        ]
    )
    variation_type: Literal["large", "medium", "small"] = Field(
        description="Indicates the degree of change between the first frame and the last frame.",
    )
    variation_reason: str = Field(
        description="The reason for the variation type of the shot.",
        examples=[
            "This is a smooth transition shot from the sky to the ground. The content of the shot has changed significantly, so the variation type is large.",
            "Compared to the first frame, a new character appears in the last frame, and there are no significant changes in the composition. So the variation type is medium.",
            "Compared to the first frame, there are only minor changes in the composition. So the variation type is small.",
            "This shot only shows Alice speaking and the changes in her facial expressions, thus the variation type is small.",
        ],
    )



# ── 单镜重写（分镜脚本阶段：只让 AI 重做某一个镜头的画面/台词）──────────────
system_prompt_template_rewrite_shot = \
"""
[Role]
You are a professional storyboard artist.

[Task]
You are given a single-scene script and the scene's CURRENT shot list. Rewrite ONLY the one shot marked ">>> REWRITE THIS <<<", producing a fresh, improved version of THAT shot's visual description and its audio/dialogue. Keep it consistent with the scene and the neighboring shots. Do NOT change the other shots, and do NOT add or remove shots.

[Input]
- Scene script within <SCRIPT></SCRIPT>.
- Character list within <CHARACTERS></CHARACTERS>.
- The current shots within <SHOTS></SHOTS> (the one to rewrite is marked).
- An optional user instruction within <INSTRUCTION></INSTRUCTION>; if present, follow it. If empty, produce a better alternative that keeps the shot's narrative purpose.

[Output]
{format_instructions}

[Guidelines]
- Output values match the language used in the script, unless a per-field directive below overrides it.
- Use cinematic language (shot type, angle, composition, movement). Indicate element positions in the frame and which direction each character faces.
- Produce a detailed director_desc and 1-3 ordered timed beats. Preserve subtle, filmable performance through gaze, breathing, posture, facial muscles, swallowing, tears, lip motion, and pauses where appropriate.
- beats/action/performance/camera, visual_desc, visual_style, avoid, director_desc, and audio descriptions follow the script's target language. For a Chinese target, use clear Simplified Chinese in every user-reviewable field.
- Keep literal dialogue only in audio_desc. Do not quote it in visual_desc or beats.
- In the visual description, enclose character names in angle brackets (e.g., <Alice>); do NOT bracket names in the dialogue/speaker text.
- At most one dialogue line per character in this shot. Keep the rewritten shot self-contained (no reference to other shots).
- Avoid unsafe content; use indirect/suggestive means if needed.
- Output ONLY the single rewritten shot, not the whole list.
"""

human_prompt_template_rewrite_shot = \
"""
<SCRIPT>
{script_str}
</SCRIPT>

<CHARACTERS>
{characters_str}
</CHARACTERS>

<SHOTS>
{shots_str}
</SHOTS>

<INSTRUCTION>
{instruction_str}
</INSTRUCTION>
"""


class ShotRewriteResponse(BaseModel):
    visual_desc: str = Field(
        description="The rewritten visual description of this single shot, in the target language of the script; cinematic (shot type/angle/composition/movement); character names in <angle brackets>."
    )
    audio_desc: str = Field(
        default="",
        description="The rewritten audio for this shot, in the script's language. For dialogue use a line like '[Speaker] <name> (emotion): the spoken line'; otherwise sound/narration tags. Empty if silent.",
    )
    duration_sec: float = Field(default=5.0, ge=1.0, le=15.0)
    director_desc: str = Field(
        default="",
        description="Detailed user-facing director script with shot-relative time ranges and performance progression.",
    )
    beats: List[PerformanceBeat] = Field(default_factory=list)
    visual_style: List[str] = Field(default_factory=list)
    avoid: List[str] = Field(default_factory=list)
    screen_text: Optional[str] = Field(
        default=None,
        description="Essential on-screen text the plot depends on (e.g. a phone notification). Keep it SHORT. null if none.",
    )
    screen_text_pos: Optional[Literal["top", "center", "bottom"]] = Field(
        default=None, description="Where the screen_text sits; null if no screen_text.",
    )


_CHINESE_MODE_MARKER = "【中文模式·语言规则"


def _is_chinese_dominant(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    text = re.sub(r"\[(?:Sound Effect|Speaker|Narrator|Inner Monologue)\]", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    han_count = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    latin_count = sum(1 for char in text if ("a" <= char.lower() <= "z"))
    if latin_count == 0:
        return True
    return han_count >= 2 and han_count >= latin_count


def chinese_review_field_issues(shot: object, *, decomposition: bool = False) -> dict[str, str]:
    """Return English-dominant or missing user-reviewable fields and their values."""
    get = lambda name, default="": (
        shot.get(name, default) if isinstance(shot, dict) else getattr(shot, name, default)
    )
    fields = []
    if decomposition:
        fields.extend((name, get(name)) for name in ("ff_desc", "lf_desc", "motion_desc"))
    else:
        fields.extend((name, get(name)) for name in ("visual_desc", "director_desc", "audio_desc"))
        fields.append(("visual_style", "；".join(str(item) for item in (get("visual_style", []) or []))))
        fields.append(("avoid", "；".join(str(item) for item in (get("avoid", []) or []))))
        for index, beat in enumerate(get("beats", []) or []):
            beat_get = lambda name: beat.get(name, "") if isinstance(beat, dict) else getattr(beat, name, "")
            for name in ("action", "performance", "camera"):
                fields.append((f"beats[{index}].{name}", str(beat_get(name) or "")))

    issues = {
        name: str(value or "")
        for name, value in fields
        if str(value or "").strip() and not _is_chinese_dominant(value)
    }
    if not decomposition:
        for name in ("visual_desc", "director_desc"):
            if not str(get(name, "") or "").strip():
                issues[name] = ""
    return issues


def validate_chinese_review_fields(shot: object, *, decomposition: bool = False) -> None:
    """Reject English-dominant user-reviewable fields so the LLM retry can repair them."""
    issues = chinese_review_field_issues(shot, decomposition=decomposition)
    if issues:
        raise ValueError("Chinese mode requires Simplified Chinese review fields: " + ", ".join(issues))


class StoryboardArtist:
    def __init__(
        self,
        chat_model: BaseChatModel,
        extra_system_instruction: str = "",
    ):
        self.chat_model = chat_model
        # Appended to system prompts so every user-reviewable storyboard field
        # follows the selected target language (Simplified Chinese in Chinese mode).
        self.extra_system_instruction = (extra_system_instruction or "").strip()
        self.require_chinese_output = _CHINESE_MODE_MARKER in self.extra_system_instruction

    def _system(self, base: str) -> str:
        return f"{base}\n\n{self.extra_system_instruction}" if self.extra_system_instruction else base


    @llm_retry
    async def design_storyboard(
        self,
        script: str,
        characters: List[CharacterInScene],
        user_requirement: Optional[str] = None,
        retry_timeout: int = 150,
    ) -> List[ShotBriefDescription]:

        class StoryboardResponse(BaseModel):
            storyboard: List[ShotBriefDescription] = Field(
                description="A complete storyboard of the scene, including the visual and audio description of each shot.",
            )

        script_str = script.strip()
        characters_str = "\n".join([f"Character {index}: {char}" for index, char in enumerate(characters)])
        user_requirement_str = user_requirement.strip() if user_requirement else ""

        parser = PydanticOutputParser(pydantic_object=StoryboardResponse)
        messages = [
            ('system', self._system(system_prompt_template_design_storyboard.format(format_instructions=parser.get_format_instructions()))),
            ('human', human_prompt_template_design_storyboard.format(script_str=script_str, characters_str=characters_str, user_requirement_str=user_requirement_str)),
        ]
        chain = self.chat_model | parser
        response: StoryboardResponse = await asyncio.wait_for(
            chain.ainvoke(messages),
            timeout=retry_timeout,
        )
        storyboard = response.storyboard
        if self.require_chinese_output:
            for shot in storyboard:
                validate_chinese_review_fields(shot)

        return storyboard




    @llm_retry
    async def rewrite_shot(
        self,
        script: str,
        shots: list,
        target_index: int,
        characters: List[CharacterInScene],
        user_requirement: str = "",
        instruction: str = "",
        retry_timeout: int = 150,
    ) -> ShotRewriteResponse:
        """Rewrite ONE shot's visual/audio description in place (分镜脚本阶段)，
        using the scene script + the current shot list as context. Returns just the
        rewritten shot's fields; the caller decides whether to persist."""
        parser = PydanticOutputParser(pydantic_object=ShotRewriteResponse)

        def g(s, k):
            return (s.get(k) if isinstance(s, dict) else getattr(s, k, "")) or ""

        lines = []
        for i, s in enumerate(shots):
            mark = " >>> REWRITE THIS <<<" if i == target_index else ""
            beats = g(s, 'beats')
            if isinstance(beats, list):
                beats = [b.model_dump() if hasattr(b, "model_dump") else b for b in beats]
            lines.append(
                f"Shot {i}{mark}:\n"
                f"  duration_sec: {g(s, 'duration_sec') or 5}\n"
                f"  director_desc: {g(s, 'director_desc')}\n"
                f"  visual: {g(s, 'visual_desc')}\n"
                f"  beats: {beats or []}\n"
                f"  visual_style: {g(s, 'visual_style') or []}\n"
                f"  avoid: {g(s, 'avoid') or []}\n"
                f"  audio: {g(s, 'audio_desc')}"
            )
        shots_str = "\n".join(lines) or "(none)"
        characters_str = "\n".join([f"Character {i}: {c}" for i, c in enumerate(characters)]) or "(none)"

        instr = (instruction or "").strip()
        if user_requirement:
            instr = (instr + "\n" + user_requirement).strip() if instr else user_requirement.strip()

        messages = [
            ('system', self._system(system_prompt_template_rewrite_shot.format(format_instructions=parser.get_format_instructions()))),
            ('human', human_prompt_template_rewrite_shot.format(
                script_str=script.strip(), characters_str=characters_str, shots_str=shots_str,
                instruction_str=instr or "(no specific instruction; produce an improved alternative)")),
        ]
        chain = self.chat_model | parser
        resp: ShotRewriteResponse = await asyncio.wait_for(chain.ainvoke(messages), timeout=retry_timeout)
        if self.require_chinese_output:
            validate_chinese_review_fields(resp)
        return resp

    @llm_retry
    async def decompose_visual_description(
        self,
        shot_brief_desc: ShotBriefDescription,
        characters: List[CharacterInScene],
        retry_timeout: int = 150,
    ) -> ShotDescription:
        parser = PydanticOutputParser(pydantic_object=VisDescDecompositionResponse)
        prompt_template = ChatPromptTemplate.from_messages(
            [
                ('system', self._system(system_prompt_template_decompose_visual_description)),
                ('human', human_prompt_template_decompose_visual_description),
            ]
        )
        chain = prompt_template | self.chat_model | parser

        visual_desc = compile_video_prompt(shot_brief_desc)

        characters_str = "\n".join([f"{char.identifier_in_scene}: (static) {char.static_features}; (dynamic) {char.dynamic_features}" for char in characters])

        decomposition: VisDescDecompositionResponse = await asyncio.wait_for(
            chain.ainvoke(
                input={
                    "format_instructions": parser.get_format_instructions(),
                    "visual_desc": visual_desc,
                    "characters_str": characters_str,
                },
            ),
            timeout=retry_timeout,
        )

        validate_char_idxs(decomposition.ff_vis_char_idxs, len(characters), "ff_vis_char_idxs")
        validate_char_idxs(decomposition.lf_vis_char_idxs, len(characters), "lf_vis_char_idxs")
        if self.require_chinese_output:
            validate_chinese_review_fields(decomposition, decomposition=True)

        return ShotDescription(
            idx=shot_brief_desc.idx,
            is_last=shot_brief_desc.is_last,
            cam_idx=shot_brief_desc.cam_idx,
            visual_desc=shot_brief_desc.visual_desc,
            variation_type=decomposition.variation_type,
            variation_reason=decomposition.variation_reason,
            ff_desc=decomposition.ff_desc,
            ff_vis_char_idxs=decomposition.ff_vis_char_idxs,
            lf_desc=decomposition.lf_desc,
            lf_vis_char_idxs=decomposition.lf_vis_char_idxs,
            motion_desc=decomposition.motion_desc,
            duration_sec=shot_brief_desc.duration_sec,
            director_desc=shot_brief_desc.director_desc,
            beats=shot_brief_desc.beats,
            visual_style=shot_brief_desc.visual_style,
            avoid=shot_brief_desc.avoid,
            audio_desc=shot_brief_desc.audio_desc,
            screen_text=shot_brief_desc.screen_text,
            screen_text_pos=shot_brief_desc.screen_text_pos,
        )


def validate_char_idxs(idxs, num_characters, field_name):
    """Reject LLM-emitted character indices outside [0, num_characters).

    Negative values would silently select the wrong character via Python
    indexing; out-of-range values would crash deep inside the render gather.
    Raising here lets the @retry on decompose_visual_description re-ask.
    """
    invalid = [idx for idx in idxs if idx < 0 or idx >= num_characters]
    if invalid:
        raise ValueError(
            f"{field_name} contains invalid character indices {invalid}; "
            f"valid range is 0..{num_characters - 1}"
        )
