"""Domain-specific reasoning packs (领域化推理).

The base screenwriter / storyboard / hook prompts are genre-neutral. Real
short-video verticals need very different narrative reasoning: a 短剧 lives or
dies on 爽点 density and cliffhangers, a 影视解说 needs third-person narration
pacing with 过渡钩子, a 知识科普 needs a curiosity-question structure. Rather
than fork the prompts, each pack contributes a small instruction snippet that is
appended (alongside the Chinese-runtime instruction) to the relevant agent's
system prompt. Packs are additive and fully optional — ``domain="" / "none"``
yields empty snippets, so behaviour is identical to upstream SceneForge.

Wiring: pipelines resolve a pack by key and call ``instruction_for(role, base)``
to compose ``base + snippet`` for the screenwriter and storyboard artist, and
``hook`` for the HookWriter. Keys come from ``creative.domain`` in the pipeline
YAML or the production form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True, slots=True)
class DomainPack:
    key: str
    label: str
    # Genre-specific reasoning appended to each agent's system prompt. Empty
    # string = no extra guidance for that role.
    screenwriter: str = ""
    storyboard: str = ""
    hook: str = ""
    # Visual-style snippet appended to every image/video generation prompt
    # (e.g. "cyberpunk, neon-lit, rain, cinematic"). Empty = no styling.
    video: str = ""
    # Provenance: builtin packs vs user-uploaded skills (so the UI can list /
    # delete only the latter). The source filename for uploaded skills.
    source: str = "builtin"
    author: str = ""

    def instruction_for(self, role: str, base: str = "") -> str:
        """Compose ``base`` (e.g. the Chinese instruction) with this pack's
        snippet for ``role`` ('screenwriter' | 'storyboard' | 'hook' | 'video')."""
        snippet = getattr(self, role, "") or ""
        base = (base or "").strip()
        snippet = snippet.strip()
        if base and snippet:
            return f"{base}\n\n{snippet}"
        return base or snippet


# ── Built-in packs ──────────────────────────────────────────────────────────
# Snippets are written in Chinese because the runtime spoken content is Chinese;
# they steer reasoning, not output language (the Chinese-runtime instruction
# already governs language).

_SHORT_DRAMA = DomainPack(
    key="short_drama",
    label="短剧 / 爽文",
    screenwriter=(
        "[领域：短剧/爽文]\n"
        "- 爽点前置：开场10秒内必须抛出核心冲突或一个钩子，严禁冗长铺垫。\n"
        "- 每个场景至少包含一个情绪爆点（打脸、逆袭、误会反转、身份揭露、扮猪吃老虎），"
        "并在场景结尾留下钩子（cliffhanger）勾住观众看下一幕。\n"
        "- 台词短促、口语化、有冲击力，多用反差与悬念，少用旁白解释。\n"
        "- 节奏快、信息密度高；情节推进以'制造期待→延迟满足→爆发'为骨架。"
    ),
    storyboard=(
        "[领域：短剧/爽文]\n"
        "- 多用近景与特写强化情绪与反应（震惊、得意、隐忍、爆发）。\n"
        "- 关键反转处务必给反应镜头（reaction shot），先给施压方、再切被打脸方的表情。\n"
        "- 镜头切换节奏快、信息密度高；爆点时刻可用推镜（push-in）强调。"
    ),
    hook=(
        "钩子要先抛出最强的冲突或反转结果（先给结果、制造'这是怎么回事'的悬念），"
        "再让观众想看过程。"
    ),
)

_EXPLAINER = DomainPack(
    key="explainer",
    label="影视/事件解说",
    screenwriter=(
        "[领域：影视/事件解说]\n"
        "- 采用第三人称解说口吻，以旁白叙述为主，不要写成角色对白剧本。\n"
        "- 开篇用一句话钩子概括最劲爆的看点（如'接下来发生的事，没有人能想到'）。\n"
        "- 按'悬念→展开→揭晓'推进，每段之间留过渡钩子（'但是''然而''更离谱的是'）承上启下。\n"
        "- 解说词凝练、信息密度高，每个信息点对应一个画面节点。"
    ),
    storyboard=(
        "[领域：影视/事件解说]\n"
        "- 画面服务于解说：镜头跟随解说节点切换，每个信息点一个镜头。\n"
        "- 多用全景交代背景、特写强调关键证据/细节；镜头停留时间匹配解说语速。\n"
        "- 避免冗余空镜，每个镜头都要承载一条解说信息。"
    ),
    hook=(
        "钩子用悬念式提问或反常识结论开场（如'你绝对想不到结局是这样'）。"
    ),
)

_KNOWLEDGE = DomainPack(
    key="knowledge",
    label="知识科普",
    screenwriter=(
        "[领域：知识科普]\n"
        "- 开篇抛出一个让人好奇的问题或反直觉结论。\n"
        "- 采用'问题→原理→生活化举例→结论'结构，语言通俗、善用类比。\n"
        "- 每个知识点都配一个贴近生活的例子；结尾给一句可转发的金句。"
    ),
    storyboard=(
        "[领域：知识科普]\n"
        "- 多用图示化/示意性画面与对比镜头；关键数据或名词给特写并配文字强调。\n"
        "- 画面简洁、服务于讲解，避免与讲解无关的复杂场景。"
    ),
    hook=(
        "钩子抛出一个反直觉问题（如'为什么你以为对的，其实是错的？'）。"
    ),
)

_PACKS: Dict[str, DomainPack] = {
    p.key: p for p in (_SHORT_DRAMA, _EXPLAINER, _KNOWLEDGE)
}

# Accept a few friendly aliases (Chinese names, common synonyms) so config /
# form values are forgiving.
_ALIASES: Dict[str, str] = {
    "短剧": "short_drama",
    "爽文": "short_drama",
    "shortdrama": "short_drama",
    "drama": "short_drama",
    "解说": "explainer",
    "影视解说": "explainer",
    "narration": "explainer",
    "科普": "knowledge",
    "知识": "knowledge",
    "knowledge": "knowledge",
    "popsci": "knowledge",
}

_GENERAL = DomainPack(key="general", label="通用（不限定领域）")


# ── User-uploaded skills (.md) ────────────────────────────────────────────────
# A "skill" is a user-authored DomainPack loaded from a Markdown file: YAML
# frontmatter (name/key/author) + per-stage sections. It plugs into the exact
# same injection points as the built-in packs, so the pipelines need no special
# casing — a selected skill is just a DomainPack resolved by key. The registry
# is process-global and refreshed by the server (startup + on upload/delete).

import re as _re

_USER_SKILLS: Dict[str, DomainPack] = {}

# Section heading (## 标题) -> DomainPack role. Accepts Chinese and English so
# uploaded files can use either. Unknown headings are ignored.
_SECTION_ROLES: Dict[str, str] = {
    "剧本": "screenwriter", "故事": "screenwriter", "编剧": "screenwriter",
    "script": "screenwriter", "story": "screenwriter", "screenwriter": "screenwriter",
    "分镜": "storyboard", "镜头": "storyboard", "storyboard": "storyboard", "shots": "storyboard",
    "视频": "video", "画面": "video", "视觉": "video", "风格": "video",
    "video": "video", "visual": "video", "style": "video",
    "钩子": "hook", "开场": "hook", "hook": "hook",
}

_KEY_RE = _re.compile(r"[^a-z0-9_-]+")


def _safe_key(raw: str) -> str:
    """Sanitize a skill key to ``[a-z0-9_-]`` (no path separators / spaces)."""
    k = _KEY_RE.sub("_", (raw or "").strip().lower()).strip("_")
    return k[:64]


def parse_skill_markdown(text: str, default_key: str = "") -> Optional[DomainPack]:
    """Parse a skill ``.md`` (YAML frontmatter + ``## section`` bodies) into a
    DomainPack. Returns None if it yields no usable per-stage content, so callers
    can reject empty/garbage uploads. Pure text in -> pure data out (no code)."""
    import yaml
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    meta: dict = {}
    body = text
    # Optional YAML frontmatter delimited by leading '---' ... '---'.
    if text.lstrip().startswith("---"):
        rest = text.lstrip()[3:]
        end = rest.find("\n---")
        if end != -1:
            try:
                meta = yaml.safe_load(rest[:end]) or {}
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            body = rest[end + 4:]
    sections: Dict[str, list] = {}
    current: Optional[str] = None
    for line in body.split("\n"):
        m = _re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if m:
            role = _SECTION_ROLES.get(m.group(1).strip().lower())
            current = role
            if role:
                sections.setdefault(role, [])
            continue
        if current:
            sections[current].append(line)
    parts = {role: "\n".join(lines).strip() for role, lines in sections.items()}
    if not any(parts.values()):
        return None
    key = _safe_key(str(meta.get("key") or "") or default_key)
    if not key:
        return None
    label = str(meta.get("name") or meta.get("label") or key).strip()[:80]
    return DomainPack(
        key=key, label=label,
        screenwriter=parts.get("screenwriter", ""),
        storyboard=parts.get("storyboard", ""),
        hook=parts.get("hook", ""),
        video=parts.get("video", ""),
        source=str(meta.get("__source") or default_key or key),
        author=str(meta.get("author") or "").strip()[:60],
    )


def load_skills_from_dir(path) -> int:
    """(Re)load all ``*.md`` skills from ``path`` into the process registry,
    replacing any previously loaded set. Bad files are skipped. Returns the
    number of skills loaded. Missing dir -> registry cleared, returns 0."""
    from pathlib import Path as _Path
    _USER_SKILLS.clear()
    d = _Path(path)
    if not d.exists() or not d.is_dir():
        return 0
    for f in sorted(d.glob("*.md")):
        try:
            pack = parse_skill_markdown(f.read_text(encoding="utf-8"), default_key=f.stem)
        except Exception:
            pack = None
        if pack is not None:
            # Stamp the source filename so the server can map key -> file for delete.
            _USER_SKILLS[pack.key] = DomainPack(
                key=pack.key, label=pack.label, screenwriter=pack.screenwriter,
                storyboard=pack.storyboard, hook=pack.hook, video=pack.video,
                source=f.name, author=pack.author)
    return len(_USER_SKILLS)


def list_user_skills() -> List[DomainPack]:
    """Loaded user skills (for the management UI / skill picker)."""
    return list(_USER_SKILLS.values())


def resolve_domain(name: Optional[str]) -> DomainPack:
    """Resolve a domain/skill key/alias to a pack. Built-in domains and aliases
    win; then user-uploaded skills by key. Unknown/empty -> the no-op general
    pack (all snippets empty), so callers never need a None check."""
    key = (name or "").strip().lower()
    if not key or key in ("none", "general", "default", "通用"):
        return _GENERAL
    if key in _PACKS:
        return _PACKS[key]
    if key in _USER_SKILLS:
        return _USER_SKILLS[key]
    if key in _ALIASES:
        return _PACKS.get(_ALIASES[key], _GENERAL)
    # A user skill key may contain characters we lowercase but not otherwise
    # mangle; try the sanitized form too.
    sk = _safe_key(key)
    return _USER_SKILLS.get(sk, _GENERAL)


def list_domains() -> List[Dict[str, str]]:
    """[{key, label}] for UI/config — general first, then the built-ins."""
    return [{"key": _GENERAL.key, "label": _GENERAL.label}] + [
        {"key": p.key, "label": p.label} for p in _PACKS.values()
    ]


def builtin_packs() -> List[DomainPack]:
    """The built-in domain packs (短剧/解说/科普) as full DomainPacks, so the
    Skill 市场 page can show them read-only (they're compiled in, not files)."""
    return list(_PACKS.values())
