from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal, Tuple


class PerformanceBeat(BaseModel):
    """A relative-time performance beat inside one generated video clip.

    The user-facing director prose lives in ``director_desc``. Beat instructions
    are deliberately structured so they can be preserved when the shot is
    decomposed into keyframes and compiled for a video provider.
    """

    start_sec: float = Field(
        default=0.0,
        ge=0.0,
        description="Start time relative to the beginning of this shot, in seconds.",
    )
    end_sec: float = Field(
        default=5.0,
        gt=0.0,
        description="End time relative to the beginning of this shot, in seconds.",
    )
    action: str = Field(
        default="",
        description="Concrete, visible on-screen action for this beat, written in the target language of the script.",
    )
    performance: str = Field(
        default="",
        description="Fine performance direction in the target language: gaze, breath, facial muscles, swallowing, tears, posture, and restrained emotion.",
    )
    camera: str = Field(
        default="",
        description="Camera behavior during this beat. Empty means maintain the current framing.",
    )

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.end_sec <= self.start_sec:
            raise ValueError("beat end_sec must be greater than start_sec")
        return self


def _validate_beat_timeline(beats: List[PerformanceBeat], duration_sec: float) -> None:
    previous_end = 0.0
    for beat in beats:
        if beat.start_sec < previous_end:
            raise ValueError("performance beats must be ordered and non-overlapping")
        if beat.end_sec > duration_sec:
            raise ValueError("performance beat exceeds duration_sec")
        previous_end = beat.end_sec


class ShotBriefDescription(BaseModel):
    idx: int = Field(
        description="The index of the shot in the sequence, starting from 0.",
        examples=[0, 1, 2],
    )
    is_last: bool = Field(
        description="Whether this is the last shot. If True, the story of the script has ended and no more shots will be planned after this one.",
        examples=[False, True],
    )

    # visual
    cam_idx: int = Field(
        description="The index of the camera in the scene.",
        examples=[0, 1, 2],
    )
    visual_desc: str = Field(
        description='''An execution description in the script's target language of the visible shot: composition, lighting, character placement, facing direction, and initial-to-final visual change. Character identifiers must match the character list and be enclosed in angle brackets (e.g., <Alice>, <Bob>). Do not include the literal spoken dialogue; dialogue belongs in audio_desc.''',
        examples=[
            "An over-the-shoulder shot at eye level, positioned behind <Alice>. The foreground, including <Alice>'s shoulder and head, is softly blurred, directing focus onto <Bob>'s face. <Bob>'s subtle reactions—shifting from surprise to delight—are clearly visible. The supermarket background is gently blurred with cool fluorescent lighting.",
        ]
    )

    duration_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=15.0,
        description="Planned duration of this shot in seconds. This is a soft timing target until the selected video provider supports it exactly.",
    )
    director_desc: str = Field(
        default="",
        description="Detailed user-facing director script in the same language as the source script, including timing, camera, performance, and emotional progression.",
    )
    beats: List[PerformanceBeat] = Field(
        default_factory=list,
        description="Ordered, non-overlapping performance beats using times relative to this shot. Usually 1-3 beats.",
    )
    visual_style: List[str] = Field(
        default_factory=list,
        description="Short visual-style instructions in the script's target language that must reach the image/video generation prompt.",
    )
    avoid: List[str] = Field(
        default_factory=list,
        description="Short negative constraints in the script's target language, such as exaggerated crying, fast cutting, or large gestures.",
    )


    # audio
    audio_desc: str = Field(
        description=(
            "A detailed description of the audio in the shot. The structural tags "
            "([Sound Effect] / [Speaker] / [Narrator] / [Inner Monologue]) stay in English "
            "for machine recognition, while the speaker name, emotion, sound description and spoken line "
            "(dialogue / narration / monologue, after the colon) MUST be written in the "
            "SAME language as the lines in the script — reuse the script's exact wording "
            "verbatim; never translate or rewrite it into another language."
        ),
        examples=[
            "[Sound Effect] Ambient sound (supermarket background noise, shopping cart wheels rolling)",
            "[Speaker] <speaker name> (<emotion>): <the spoken line, verbatim in the script's own language>",
            None,
        ],
    )

    # On-screen ("diegetic") text the STORY needs the viewer to read.
    screen_text: Optional[str] = Field(
        default=None,
        description=(
            "Essential on-screen text the plot depends on the viewer reading — a phone "
            "notification, a chat/email line, a sign, a PPT bullet, a bank balance. Put it "
            "HERE, and do NOT bake it into visual_desc: image models render text as "
            "garbled/mirrored fake glyphs, so this string is instead composited cleanly in "
            "post with a real font. Use the SAME language as the script. Keep it SHORT — a "
            "few words / one short line. Leave null when the shot needs no readable text "
            "(most shots) — convey information through action and dialogue when you can."
        ),
        examples=["全员邮件：组织优化，部分岗位裁撤", "到账 ¥12,000", "余额 ¥2,376.52", None],
    )
    screen_text_pos: Optional[Literal["top", "center", "bottom"]] = Field(
        default=None,
        description=(
            "Where to place screen_text: 'top' | 'center' | 'bottom' (default 'center'). "
            "Avoid 'bottom' — that band is reserved for dialogue subtitles."
        ),
        examples=["center", "top", None],
    )

    @model_validator(mode="after")
    def validate_beat_timeline(self):
        _validate_beat_timeline(self.beats, self.duration_sec)
        return self

    # sound_effect: Optional[str] = Field(
    #     default=None,
    #     description="The sound effects used in the shot.",
    #     examples=[
    #         "Ambient sound (supermarket background noise, shopping cart wheels rolling)",
    #         None,
    #     ],
    # )
    # speaker: Optional[str] = Field(
    #     default=None,
    #     description="The speaker in the shot, if applicable. If there is no speaker, this field should be set to None.",
    #     examples=[
    #         "Alice",
    #         None,
    #     ],
    # )
    # is_speaker_lip_visible: Optional[bool] = Field(
    #     default=None,
    #     description="Indicates whether the speaker's lips are visible in the shot. If there is no speaker, this field should be set to None.",
    #     examples=[
    #         True,
    #         False,
    #         None,
    #     ],
    # )
    # line: Optional[str] = Field(
    #     default=None,
    #     description="The dialogue or monologue in the shot, if applicable. If there is a speaker, there must be a line. If there is no speaker, this field should be set to None.",
    #     examples=[
    #         "Hello, how are you?",
    #         None,
    #     ],
    # )
    # emotion: Optional[str] = Field(
    #     default=None,
    #     description="The emotion of the speaker when delivering the line, if applicable. If there is a speaker, there must be an emotion. If there is no speaker, this field should be set to None.",
    #     examples=[
    #         "Happy",
    #         None,
    #     ],
    # )

    def __str__(self):
        s = f"Shot {self.idx}:\n"
        s += f"Camera Index: {self.cam_idx}\n"
        s += f"Visual: {self.visual_desc}\n"
        if self.audio_desc:
            s += f"Audio: {self.audio_desc}"
        return s


class ShotDescription(BaseModel):
    idx: int = Field(
        description="The index of the shot in the sequence, starting from 0."
    )
    is_last: bool = Field(
        description="Whether this is the last shot in the sequence. If True, no more shots will be planned after this one."
    )

    # visual
    cam_idx: int = Field(
        description="The index of the camera in the scene.",
        examples=[0, 1, 2],
    )
    visual_desc: str = Field(
        description='''An execution description in the script's target language of the visible shot: composition, lighting, character placement, facing direction, and initial-to-final visual change. Character identifiers must match the character list and be enclosed in angle brackets (e.g., <Alice>, <Bob>). Do not include the literal spoken dialogue; dialogue belongs in audio_desc.''',
        examples=[
            "An over-the-shoulder shot at eye level, positioned behind <Alice>. The foreground, including <Alice>'s shoulder and head, is softly blurred, directing focus onto <Bob>'s face. <Bob>'s subtle reactions—shifting from surprise to delight—are clearly visible. The supermarket background is gently blurred with cool fluorescent lighting.",
        ]
    )
    variation_type: Literal["large", "medium", "small"] = Field(
        description="Indicates the degree of change in the shot's content.",
        examples=["large", "medium", "small"],
    )
    variation_reason: str = Field(
        description="The reason for the variation type of the shot.",
        examples=[
            "This is a transition shot where the content of the first frame and the last frame differs dramatically. So the variation type is large.",
            "Compared to the first frame, a new character appears in the last frame, and there are no significant changes in the composition. So the variation type is medium.",
            "Compared to the first frame, there are only minor changes in the composition. So the variation type is small.",
            "This shot only shows Alice speaking and the changes in her facial expressions, thus the variation type is small.",
        ],
    )

    ff_desc: str = Field(
        description="The first frame of the shot.",
        examples=[
            "Medium shot of a supermarket aisle at eye level. Bob(a tall man wearing a blue shirt and jeans) is positioned on the right side of the frame, captured in profile and facing right, while Alice(a young woman with short hair, wearing a green dress) is on the left, shown pushing a shopping cart with her gaze lowered toward the ground. They are arranged in a front-to-back spatial relationship. Shelves line both sides of the frame, and cool-toned fluorescent lighting from above washes over the scene. The vibrant colors of product packaging contrast with the metallic gray of the shopping cart, all contained within a stable, horizontally balanced composition.",
            "Extreme long shot. Aerial view from hundreds of meters above the ground. The boundless golden desert resembles undulating frozen waves, occupying the vast majority of the frame. At the very center of the image, a tiny, solitary explorer appears only as a faint dark speck, dragging a long, lonely trail of footprints behind him, stretching all the way to the edge of the frame.",
            "Medium shot at eye level angle. Designer A(with a beard, wearing a white suit) leans forward passionately, speaking emphatically. Product Manager B(with a beard, wearing a white T-shirt) sits with crossed arms, looking skeptical. Between them, Development Engineer C(brown hair, wearing a blue T-shirt) appears anxious, glancing between the two. Project Manager D(curly hair, wearing a red T-shirt) prepares to mediate, focusing on a whiteboard. Bright overhead lighting highlights their expressions, with a blurred whiteboard and glass wall in the background.",
            "A low-angle close-up shot captures the figure from below, framing him from the chest up. His face appears resolute and commanding, his eyes piercing as he speaks passionately. Flecks of saliva are visible, emphasizing his intensity. The overcast sky breaks with occasional light, casting him as a heroic, almost monumental figure against the gloom.",
            "An extremely close-up of an old, motionless pocket watch. Soft light highlights scratches on its brass case and the enamel dial with Roman numerals. The second hand remains fixed at 'VIII', casting a sharp shadow. A wrinkled finger gently touches the glass surface, evoking a tangible sense of stillness and time.",
            "An over-the-shoulder shot at eye level, positioned behind Character A(red hair, wearing a white T-shirt). The foreground, including A's shoulder and head, is softly blurred, directing focus onto Character B(with a beard, wearing a white T-shirt)'s face. B's subtle reactions—shifting from surprise to confusion, then to a glimmer of understanding—are clearly visible. The café background is gently blurred with warm lighting.",
        ]
    )
    ff_vis_char_idxs: List[int] = Field(
        default=[],
        description="The indices of the characters in the first frame.",
        examples=[
            [0, 1],
            [0],
            [],
        ],
    )
    lf_desc: str = Field(
        description="The last frame of the shot.",
    )
    lf_vis_char_idxs: List[int] = Field(
        default=[],
        description="The indices of the characters in the last frame.",
    )
    motion_desc: str = Field(
        description="Visual motion in the script's target language: camera movement, ordered on-screen action, and visible performance changes. Do not include literal dialogue or narration text.",
    )

    duration_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=15.0,
        description="Planned duration of this shot in seconds.",
    )
    director_desc: str = Field(
        default="",
        description="Detailed user-facing director script in the same language as the source script.",
    )
    beats: List[PerformanceBeat] = Field(
        default_factory=list,
        description="Ordered performance beats with shot-relative timing.",
    )
    visual_style: List[str] = Field(
        default_factory=list,
        description="Visual-style instructions in the script's target language, preserved for rendering.",
    )
    avoid: List[str] = Field(
        default_factory=list,
        description="Negative constraints in the script's target language, preserved for rendering.",
    )

    # audio
    audio_desc: str = Field(
        description=(
            "A detailed description of the audio in the shot. The structural tags "
            "([Sound Effect] / [Speaker] / [Narrator] / [Inner Monologue]) stay in English "
            "for machine recognition, while the speaker name, emotion, sound description and spoken line "
            "(dialogue / narration / monologue, after the colon) MUST be written in the "
            "SAME language as the lines in the script — reuse the script's exact wording "
            "verbatim; never translate or rewrite it into another language."
        ),
        examples=[
            "[Sound Effect] Ambient sound (supermarket background noise, shopping cart wheels rolling)",
            "[Speaker] <speaker name> (<emotion>): <the spoken line, verbatim in the script's own language>",
            None,
        ],
    )

    # On-screen ("diegetic") text the STORY needs the viewer to read.
    screen_text: Optional[str] = Field(
        default=None,
        description=(
            "Essential on-screen text the plot depends on the viewer reading — a phone "
            "notification, a chat/email line, a sign, a PPT bullet, a bank balance. Put it "
            "HERE, and do NOT bake it into visual_desc: image models render text as "
            "garbled/mirrored fake glyphs, so this string is instead composited cleanly in "
            "post with a real font. Use the SAME language as the script. Keep it SHORT — a "
            "few words / one short line. Leave null when the shot needs no readable text "
            "(most shots) — convey information through action and dialogue when you can."
        ),
        examples=["全员邮件：组织优化，部分岗位裁撤", "到账 ¥12,000", "余额 ¥2,376.52", None],
    )
    screen_text_pos: Optional[Literal["top", "center", "bottom"]] = Field(
        default=None,
        description=(
            "Where to place screen_text: 'top' | 'center' | 'bottom' (default 'center'). "
            "Avoid 'bottom' — that band is reserved for dialogue subtitles."
        ),
        examples=["center", "top", None],
    )

    @model_validator(mode="after")
    def validate_beat_timeline(self):
        _validate_beat_timeline(self.beats, self.duration_sec)
        return self

    # sound_effect: Optional[str] = Field(
    #     default=None,
    #     description="The sound effects used in the shot. For example, a door creaking or footsteps approaching.",
    # )
    # speaker: Optional[str] = Field(
    #     default=None,
    #     description="The speaker in the shot, if applicable. If there is no speaker, this field should be set to None.",
    # )
    # is_speaker_lip_visible: Optional[bool] = Field(
    #     default=None,
    #     description="Indicates whether the speaker's lips are visible in the shot. If there is no speaker, this field should be set to None.",
    # )
    # line: Optional[str] = Field(
    #     default=None,
    #     description="The dialogue or monologue in the shot, if applicable. If there is a speaker, there must be a line. If there is no speaker, this field should be set to None.",
    # )
    # emotion: Optional[str] = Field(
    #     default=None,
    #     description="The emotion of the speaker when delivering the line, if applicable. If there is a speaker, there must be an emotion. If there is no speaker, this field should be set to None.",
    # )
