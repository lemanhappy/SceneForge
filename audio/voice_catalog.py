"""Static catalog of selectable TTS voices per provider, for the voice-picker
UI. OpenAI exposes a fixed set of voices via /audio/speech; MiniMax exposes
named voice_ids via its T2A endpoint (much stronger Chinese).
"""

# Each voice carries a ``lang`` tag so the UI can auto-pick a voice matching the
# video's target language: "multi" = multilingual (reads any language, e.g. OpenAI),
# "zh"/"en" = language-specific.
VOICE_CATALOG = {
    "openai": {
        "label": "OpenAI (tts-1)",
        "models": ["tts-1", "tts-1-hd"],
        "voices": [
            {"id": "alloy", "label": "Alloy（多语种）", "lang": "multi"},
            {"id": "echo", "label": "Echo（多语种）", "lang": "multi"},
            {"id": "fable", "label": "Fable（多语种）", "lang": "multi"},
            {"id": "onyx", "label": "Onyx（低沉男声·多语种）", "lang": "multi"},
            {"id": "nova", "label": "Nova（多语种）", "lang": "multi"},
            {"id": "shimmer", "label": "Shimmer（多语种）", "lang": "multi"},
        ],
    },
    "minimax": {
        "label": "MiniMax（中文情感，推荐）",
        "models": ["speech-2.6-hd", "speech-02-hd"],
        "voices": [
            {"id": "male-qn-jingying", "label": "精英青年（沉稳男）", "lang": "zh"},
            {"id": "male-qn-badao", "label": "霸道青年（气势男）", "lang": "zh"},
            {"id": "male-qn-qingse", "label": "青涩青年（年轻男）", "lang": "zh"},
            {"id": "male-qn-daxuesheng", "label": "青年大学生（男）", "lang": "zh"},
            {"id": "audiobook_male_1", "label": "有声书男声1（解说）", "lang": "zh"},
            {"id": "audiobook_male_2", "label": "有声书男声2（解说）", "lang": "zh"},
            {"id": "presenter_male", "label": "男主播", "lang": "zh"},
            {"id": "female-shaonv", "label": "少女", "lang": "zh"},
            {"id": "female-yujie", "label": "御姐", "lang": "zh"},
            {"id": "female-chengshu", "label": "成熟女性", "lang": "zh"},
            {"id": "female-tianmei", "label": "甜美女性", "lang": "zh"},
            {"id": "audiobook_female_1", "label": "有声书女声1（解说）", "lang": "zh"},
            {"id": "presenter_female", "label": "女主播", "lang": "zh"},
        ],
    },
}


def default_voice_for(provider: str, language: str) -> str:
    """A sensible default voice for ``language`` under ``provider``: prefer an exact
    language match, then a multilingual voice, then the first voice. ``""`` if the
    provider is unknown / has no voices."""
    prov = VOICE_CATALOG.get((provider or "").lower())
    if not prov or not prov.get("voices"):
        return ""
    lang = (language or "").lower()
    short = "zh" if lang.startswith("zh") else ("en" if lang.startswith("en") else lang)
    voices = prov["voices"]
    for v in voices:
        if v.get("lang") == short:
            return v["id"]
    for v in voices:
        if v.get("lang") == "multi":
            return v["id"]
    return voices[0]["id"]

# A short, expressive line used for the "试听" preview.
PREVIEW_TEXT = "我命由我不由天，仙人又如何，照样跪下。"


def catalog() -> dict:
    return VOICE_CATALOG


def is_valid(provider: str, voice: str) -> bool:
    prov = VOICE_CATALOG.get((provider or "").lower())
    if not prov:
        return False
    return any(v["id"] == voice for v in prov["voices"])
