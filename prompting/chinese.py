"""Chinese-runtime language constraint (design §14).

Kept independent of any prompt template so it can be injected verbatim even when
prompts are later externalised to Markdown and edited by users — the constraint
must survive user edits, hence it is a standalone block rather than embedded
prose.
"""

DEFAULT_CHINESE_RUNTIME_INSTRUCTION = (
    "【中文模式·语言规则（优先级最高，覆盖上文任何“输出与剧本语言一致 / match the "
    "language used in the script”之类的要求）】\n"
    "- audio_desc 里每一句台词/旁白/独白的正文必须是简体中文，并尽量直接沿用剧本中的"
    "中文原句；严禁翻译或改写成英文。\n"
    "- director_desc 是给用户审核的导演稿，必须用简体中文，写清按秒节拍、表演与运镜。\n"
    "- visual_desc / ff_desc / lf_desc / motion_desc、beats 内的 action / performance / camera、"
    "visual_style 与 avoid 都是用户可审核和编辑的提示词，必须使用简体中文；镜头术语要准确、"
    "具体、可执行，不要为了模型而改写成英文。\n"
    "- audio_desc 中的台词、旁白、情绪和音效描述必须使用简体中文；[Speaker] / "
    "[Sound Effect] / [Narrator] / [Inner Monologue] 方括号标签仅作为机器识别标记可保留英文。\n"
    "- 绝不能出现整句英文台词（例如 “At 35, I thought I would get promoted...” 这类必须"
    "写成简体中文）。"
)

DEFAULT_ENGLISH_RUNTIME_INSTRUCTION = (
    "[English mode - highest priority]\n"
    "- All spoken dialogue, narration and inner monologue in audio_desc must be in natural English.\n"
    "- director_desc and every user-reviewable storyboard field must be written in English.\n"
    "- visual_desc / ff_desc / lf_desc / motion_desc, performance beats, visual_style and avoid "
    "must use precise, executable English film terminology.\n"
    "- Preserve the meaning of source dialogue, but do not leave complete Chinese sentences in "
    "spoken or reviewable fields."
)


# Injected into every image/video generation prompt. Image models render most text
# (especially Chinese) as garbled fake glyphs, so we steer them. Two policies:
#
#   "essential" (default): suppress gratuitous/background text & watermarks (the main
#     source of garble), but ALLOW text that is essential to the scene — a phone
#     screen, a PPT slide, a key sign — kept short/large/legible and in Simplified
#     Chinese. Garble is reduced, not guaranteed gone (the model still struggles).
#   "none": no text in the picture at all — cleanest; use when scenes need no text.
#
# Real, crisp on-screen text is best composited in post with a proper font.
# Bilingual on purpose; append-only so it survives prompt edits.
IMAGE_TEXT_ESSENTIAL = (
    "On-screen text policy: avoid gratuitous or background text — no random signage, "
    "captions, watermarks, logos, UI chrome or decorative lettering (these tend to "
    "render as garbled glyphs). ONLY include text when it is essential to the scene "
    "(e.g. text on a phone screen, a PPT slide, a key sign the story depends on); in "
    "that case keep it SHORT, LARGE and clearly legible, and render it in Simplified "
    "Chinese — never English and never gibberish. "
    "画面中避免无谓的背景文字、招牌、水印、UI；仅当文字是剧情必需时（如手机屏幕、PPT、"
    "关键招牌）才出现，且要简短、字大、清晰，用简体中文，不要英文或乱码。"
)
# English-target variant of the essential policy (e.g. for English shorts): same
# "suppress gratuitous text" rule, but any essential on-screen text is English.
IMAGE_TEXT_ESSENTIAL_EN = (
    "On-screen text policy: avoid gratuitous or background text — no random signage, "
    "captions, watermarks, logos, UI chrome or decorative lettering (these tend to "
    "render as garbled glyphs). ONLY include text when it is essential to the scene "
    "(e.g. text on a phone screen, a slide, a key sign the story depends on); keep it "
    "SHORT, LARGE and clearly legible, in correct English — never gibberish."
)
IMAGE_TEXT_NONE = (
    "On-screen text policy: render the scene WITHOUT any written text at all — no "
    "letters, words, captions, subtitles, signage text, labels, logos, watermarks, "
    "UI or numbers. Do NOT attempt any Chinese characters; they would come out as "
    "garbled fake glyphs. Keep any sign/book/screen blank, textless or out of focus. "
    "画面里不要出现任何文字（中英文、招牌、字幕、水印、UI、数字一律不要）；切勿尝试画"
    "中文，否则会变成乱码假字。需要的文字一律由后期叠加层添加。"
)
# Back-compat default (also what chinese_mode turns on): the nuanced policy.
DEFAULT_IMAGE_TEXT_CONSTRAINT = IMAGE_TEXT_ESSENTIAL

# Friendly names for spoken-content target languages.
_LANG_NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어"}


def target_language(config: dict | None = None) -> str:
    """Normalized spoken-content target language: ``zh`` | ``en`` | other code, or
    ``""`` (no forced language — follow the script). Prefers ``language.target_language``;
    falls back to the legacy ``chinese_mode`` flag (true -> ``zh``)."""
    language = (config or {}).get("language") or {}
    # An explicitly empty target means "follow the script". Only fall back to
    # the legacy chinese_mode switch when the modern key is genuinely absent.
    if "target_language" in language:
        tl = str(language.get("target_language") or "").strip().lower()
        if not tl:
            return ""
    else:
        tl = ""
    if tl:
        if tl in ("zh", "zh-cn", "zh-hans", "cn", "chinese", "中文", "简体中文"):
            return "zh"
        if tl in ("en", "en-us", "english", "英文", "英语"):
            return "en"
        return tl
    return "zh" if language.get("chinese_mode") else ""


def is_chinese_mode(config: dict) -> bool:
    return target_language(config) == "zh"


def image_text_constraint(config: dict | None = None) -> str:
    """On-screen-text constraint to append to image/video prompts (empty = off).

    Order: explicit raw ``image_text_constraint`` > ``image_text_policy``
    (essential|none|off) > on(essential) when a target language is set, else off.
    For ``essential`` the allowed-text language follows the target language (default
    Simplified Chinese; English when target is ``en``)."""
    language = (config or {}).get("language") or {}
    if "image_text_constraint" in language:  # explicit raw override (string; "" = off)
        return str(language.get("image_text_constraint") or "").strip()
    lang = target_language(config)
    policy = str(language.get("image_text_policy") or "").strip().lower()
    if not policy:
        policy = "essential" if lang else "off"
    if policy == "off":
        return ""
    if policy == "none":
        return IMAGE_TEXT_NONE
    return IMAGE_TEXT_ESSENTIAL_EN if lang == "en" else IMAGE_TEXT_ESSENTIAL


def chinese_runtime_instruction(config: dict | None = None) -> str:
    """The Chinese runtime constraint text (honors a custom override at
    ``language.chinese_runtime_instruction``). This is the *text*; gating by mode is
    done by the caller / :func:`runtime_language_instruction`."""
    language = (config or {}).get("language") or {}
    override = language.get("chinese_runtime_instruction")
    return str(override).strip() if override else DEFAULT_CHINESE_RUNTIME_INSTRUCTION


def runtime_language_instruction(config: dict | None = None) -> str:
    """Per-target language instruction to inject into screenwriter/storyboard prompts.

    - target ``zh``: all user-reviewable storyboard and prompt fields use Chinese.
    - target ``en``: force English even when the user's source idea is Chinese.
    - unset / other: follow the source script unless a future language pack is added."""
    language = target_language(config)
    if language == "zh":
        return chinese_runtime_instruction(config)
    if language == "en":
        return DEFAULT_ENGLISH_RUNTIME_INSTRUCTION
    return ""
