"""User preference + template memory.

Gives the product a bit of "remembers you": the last-used style / requirement are
recalled to pre-fill the topic form, and named templates (style + requirement)
can be saved and re-applied — handy for batch content where only the topic
changes (e.g. a "修仙短剧" template reused across episodes).

Persisted as JSON, written atomically under a per-file lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from utils.atomic import atomic_write_text, file_lock


class PreferenceService:
    def __init__(self, path: str = "assets/preferences.json"):
        self.path = Path(path)

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"last_used": {}, "templates": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"last_used": {}, "templates": {}}
        data.setdefault("last_used", {})
        data.setdefault("templates", {})
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def get(self) -> dict:
        data = self._load()
        templates = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in data["templates"].items()]
        return {"last_used": data["last_used"], "templates": sorted(templates, key=lambda t: t["name"])}

    def remember(self, style: str, user_requirement: str) -> dict:
        with file_lock(self.path):
            data = self._load()
            data["last_used"] = {"style": style or "", "user_requirement": user_requirement or ""}
            self._write(data)
        return self.get()

    def save_template(self, name: str, style: str, user_requirement: str, note: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("template name is required")
        with file_lock(self.path):
            data = self._load()
            data["templates"][name] = {"style": style or "", "user_requirement": user_requirement or "", "note": note or ""}
            self._write(data)
        return self.get()

    def delete_template(self, name: str) -> dict:
        with file_lock(self.path):
            data = self._load()
            data["templates"].pop((name or "").strip(), None)
            self._write(data)
        return self.get()
