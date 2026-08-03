"""User-uploaded "skill" backend: manage Markdown skill files that steer the
generation pipeline toward a style.

A *skill* is a user-authored ``.md`` (YAML frontmatter + per-stage sections:
剧本 / 分镜 / 视频 / 钩子) that plugs into the same injection points as the
built-in domain packs (``agents/domain_packs.py``). Skills live as plain text
files under ``<workspace>/skills_user/`` — no code, so they're safe to import
and bundle into the exe. The actual "市场" is external; the app only consumes
``.md`` files the user uploads here and exposes a configurable jump-out URL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agents.domain_packs import (
    builtin_packs,
    list_user_skills,
    load_skills_from_dir,
    parse_skill_markdown,
    resolve_domain,
    _safe_key,
)

# Reject oversized uploads — a skill is a few short prompt snippets, not a corpus.
_MAX_BYTES = 64 * 1024

# Built-in jump-out links to public skill marketplaces. NOTE: these host skills in
# the SKILL.md standard (for coding agents like Claude Code/Codex/Cursor) — a
# DIFFERENT schema from SceneForge's 剧本/分镜/视频/钩子 format, so files there usually
# can't be imported directly; they're ecosystem/inspiration links. Directly-usable
# starters are the bundled examples. Users add their own market via skills.market_url.
_BUILTIN_MARKETS = [
    {"name": "Skills.sh", "url": "https://skills.sh", "note": "通用 Agent 技能市场（SKILL.md 标准）"},
    {"name": "Awesome Claude Skills", "url": "https://github.com/travisvn/awesome-claude-skills", "note": "社区精选清单"},
]


class SkillsService:
    def __init__(self, workspace_root: str | Path, config_paths: List[str] | None = None):
        self.skills_dir = Path(workspace_root) / "skills_user"
        # Bundled starter skills shipped with the app (read-only templates). Users
        # import one to get going without hunting for an external market — the
        # custom .md format means no third-party market has compatible files.
        self.examples_dir = Path(workspace_root) / "skills_examples"
        self.config_paths = [Path(p) for p in (config_paths or ["configs/idea2video.yaml"])]
        # Populate the process-global registry at startup so a freshly-launched
        # server already knows the on-disk skills (pipelines resolve by key).
        self.reload()

    def reload(self) -> int:
        return load_skills_from_dir(self.skills_dir)

    def _market_url(self) -> str:
        for p in self.config_paths:
            if p.exists():
                try:
                    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    cfg = {}
                url = ((cfg.get("skills") or {}).get("market_url") or "").strip()
                if url:
                    return url
        return ""

    @staticmethod
    def _pack_to_dict(p) -> dict:
        return {"key": p.key, "label": p.label, "author": p.author, "source": p.source,
                "script": p.screenwriter, "storyboard": p.storyboard,
                "video": p.video, "hook": p.hook}

    @staticmethod
    def _pack_to_md(label: str, key: str, author: str, parts: dict) -> str:
        """Render a skill back to .md (frontmatter + sections) — used when forking
        a built-in pack into an editable user skill."""
        lines = ["---", f"name: {label}", f"key: {key}"]
        if author:
            lines.append(f"author: {author}")
        lines += ["---", ""]
        for header, val in (("剧本", parts.get("script")), ("分镜", parts.get("storyboard")),
                            ("视频", parts.get("video")), ("钩子", parts.get("hook"))):
            if val:
                lines += [f"## {header}", val.strip(), ""]
        return "\n".join(lines)

    def _builtins(self) -> List[dict]:
        """Built-in domain packs shown read-only on the page (compiled in)."""
        return [self._pack_to_dict(p) for p in builtin_packs()]

    def _examples(self) -> List[dict]:
        """Bundled starter skills as plain dicts (parsed, not registered). The
        ``installed`` flag tells the UI which examples are already imported."""
        out: List[dict] = []
        if not self.examples_dir.exists() or not self.examples_dir.is_dir():
            return out
        installed = {p.key for p in list_user_skills()}
        for f in sorted(self.examples_dir.glob("*.md")):
            try:
                pack = parse_skill_markdown(f.read_text(encoding="utf-8"), default_key=f.stem)
            except Exception:
                pack = None
            if pack is not None:
                d = self._pack_to_dict(pack)
                d["installed"] = pack.key in installed
                out.append(d)
        return out

    def _markets(self) -> List[dict]:
        """Jump-out market links: the user's own configured market first (if any),
        then the built-in public ones."""
        out: List[dict] = []
        url = self._market_url()
        if url:
            out.append({"name": "我的市场", "url": url, "note": "你在设置→扩展里配置的地址"})
        out.extend(_BUILTIN_MARKETS)
        return out

    def list(self) -> dict:
        self.reload()
        skills = sorted(list_user_skills(), key=lambda p: p.label)
        return {"skills": [self._pack_to_dict(p) for p in skills],
                "examples": self._examples(),
                "builtins": self._builtins(),
                "market_url": self._market_url(),
                "markets": self._markets()}

    def fork_builtin(self, key: str) -> dict:
        """Copy a built-in pack into ``skills_user/`` as an editable user skill.
        Uses a distinct ``{key}_custom`` key so it doesn't get shadowed by the
        built-in (resolve_domain prefers built-ins)."""
        key = _safe_key(key)
        pack = resolve_domain(key)
        if not key or pack.key == "general" or pack.key != key:
            return {"ok": False, "error": "内置风格不存在"}
        new_key = f"{key}_custom"
        md = self._pack_to_md(f"{pack.label}（自定义）", new_key, pack.author,
                              {"script": pack.screenwriter, "storyboard": pack.storyboard,
                               "video": pack.video, "hook": pack.hook})
        return self.upload(f"{new_key}.md", md)

    def import_example(self, key: str) -> dict:
        """Copy a bundled example into ``skills_user/`` so it becomes a usable,
        selectable skill (and survives even if later deleted, since the example
        file stays). Returns the same shape as ``upload``."""
        key = _safe_key(key)
        if not key:
            return {"ok": False, "error": "无效 key"}
        src = self.examples_dir / f"{key}.md"
        if not src.exists():
            return {"ok": False, "error": "示例不存在"}
        return self.upload(f"{key}.md", src.read_text(encoding="utf-8"))

    def upload(self, filename: str, content: str) -> dict:
        """Validate a ``.md`` skill and save it under ``skills_user/``. Pure-text
        only: parsed into a DomainPack, rejected if it has no usable stage content
        or no valid key. Returns ``{ok, skill}`` or ``{ok: False, error}``."""
        if not isinstance(content, str) or not content.strip():
            return {"ok": False, "error": "内容为空"}
        if len(content.encode("utf-8")) > _MAX_BYTES:
            return {"ok": False, "error": f"文件过大（上限 {_MAX_BYTES // 1024}KB）"}
        # Derive a key: prefer the frontmatter key (via parse), else the filename.
        stem = _safe_key(Path(str(filename or "")).stem)
        pack = parse_skill_markdown(content, default_key=stem or "skill")
        if pack is None:
            return {"ok": False, "error": "未解析到有效内容（需含 ## 剧本/分镜/视频/钩子 中的至少一节，且有合法 key/文件名）"}
        key = pack.key
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        # Filename is the sanitized key — keeps one file per key (re-upload replaces).
        target = self.skills_dir / f"{key}.md"
        # Containment guard: the resolved path must stay inside skills_dir.
        try:
            target.resolve().relative_to(self.skills_dir.resolve())
        except Exception:
            return {"ok": False, "error": "非法文件名"}
        target.write_text(content, encoding="utf-8")
        self.reload()
        return {"ok": True, "skill": self._pack_to_dict(
            next((p for p in list_user_skills() if p.key == key), pack))}

    def delete(self, key: str) -> dict:
        key = _safe_key(key)
        if not key:
            return {"ok": False, "error": "无效 key"}
        target = self.skills_dir / f"{key}.md"
        try:
            target.resolve().relative_to(self.skills_dir.resolve())
        except Exception:
            return {"ok": False, "error": "非法 key"}
        if not target.exists():
            return {"ok": False, "error": "skill 不存在"}
        target.unlink()
        self.reload()
        return {"ok": True, "deleted": key}
