from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from infrastructure.sqlite import SQLiteDatabase


class SeriesService:
    """Owns short-drama metadata while existing projects remain individual episodes."""

    _EDITABLE_FIELDS = {
        "title", "premise", "planned_episode_count", "episode_duration_sec",
        "style", "target_language", "aspect_ratio", "quality_tier", "domain",
        "character_asset_ids", "prop_asset_ids", "scene_asset_ids", "lora_ids",
        "bible", "outline", "status",
    }

    def __init__(self, database: SQLiteDatabase, session_index: Any):
        self.database = database
        self.session_index = session_index
        self.database.migrate()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _text(value: Any, limit: int = 4000) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @staticmethod
    def _json_value(value: Any, fallback: Any) -> Any:
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return fallback
        return value

    def _normalize(self, values: dict, *, existing: dict | None = None) -> dict:
        source = dict(existing or {})
        source.update({key: value for key, value in (values or {}).items() if key in self._EDITABLE_FIELDS})
        title = self._text(source.get("title"), 120)
        if not title:
            raise ValueError("短剧名称不能为空")
        try:
            planned = int(source.get("planned_episode_count") or 1)
            duration = int(source.get("episode_duration_sec") or 60)
        except (TypeError, ValueError) as exc:
            raise ValueError("计划集数和单集时长必须是整数") from exc
        if not 1 <= planned <= 200:
            raise ValueError("计划集数必须在 1 到 200 之间")
        if not 5 <= duration <= 600:
            raise ValueError("单集时长必须在 5 到 600 秒之间")
        quality = self._text(source.get("quality_tier"), 20) or "balanced"
        if quality not in {"economy", "balanced", "quality"}:
            raise ValueError("质量档位必须是 economy、balanced 或 quality")
        status = self._text(source.get("status"), 20) or "active"
        if status not in {"active", "completed", "archived"}:
            raise ValueError("短剧状态无效")
        return {
            "title": title,
            "premise": self._text(source.get("premise"), 8000),
            "planned_episode_count": planned,
            "episode_duration_sec": duration,
            "style": self._text(source.get("style"), 4000),
            "target_language": self._text(source.get("target_language"), 40) or "zh-CN",
            "aspect_ratio": self._text(source.get("aspect_ratio"), 40) or "portrait",
            "quality_tier": quality,
            "domain": self._text(source.get("domain"), 100),
            "character_asset_ids": self._ids(source.get("character_asset_ids")),
            "prop_asset_ids": self._ids(source.get("prop_asset_ids")),
            "scene_asset_ids": self._ids(source.get("scene_asset_ids")),
            "lora_ids": self._ids(source.get("lora_ids")),
            "bible": self._json_value(source.get("bible") or {}, {}),
            "outline": self._json_value(source.get("outline") or [], []),
            "status": status,
        }

    def _new_id(self, title: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:32] or "series"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return f"series-{stamp}-{slug}"

    def create(self, values: dict) -> dict:
        record = self._normalize(values or {})
        now = self._now()
        record.update({"series_id": self._new_id(record["title"]), "created_at": now, "updated_at": now})
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO series(series_id, title, status, planned_episode_count, record_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["series_id"], record["title"], record["status"], record["planned_episode_count"],
                 json.dumps(record, ensure_ascii=False), now, now),
            )
        return self.get(record["series_id"])

    def _load_row(self, row) -> dict:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"短剧 {row['series_id']} 的数据已损坏") from exc
        record["series_id"] = row["series_id"]
        return record

    def list(self) -> list[dict]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM series ORDER BY updated_at DESC, series_id").fetchall()
        return [self._with_progress(self._load_row(row)) for row in rows]

    def get(self, series_id: str) -> dict:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM series WHERE series_id = ?", (str(series_id),)).fetchone()
        if row is None:
            raise KeyError(f"短剧不存在：{series_id}")
        record = self._with_progress(self._load_row(row), include_episodes=True)
        return record

    def update(self, series_id: str, values: dict) -> dict:
        current = self.get(series_id)
        current.pop("episodes", None)
        current.pop("episode_count", None)
        current.pop("completed_episode_count", None)
        current.pop("next_episode_number", None)
        normalized = self._normalize(values or {}, existing=current)
        current.update(normalized)
        current["updated_at"] = self._now()
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute(
                "UPDATE series SET title = ?, status = ?, planned_episode_count = ?, record_json = ?, updated_at = ? WHERE series_id = ?",
                (current["title"], current["status"], current["planned_episode_count"],
                 json.dumps(current, ensure_ascii=False), current["updated_at"], str(series_id)),
            )
            if not result.rowcount:
                raise KeyError(f"短剧不存在：{series_id}")
        return self.get(series_id)

    def delete(self, series_id: str) -> bool:
        detail = self.get(series_id)
        if detail.get("episodes"):
            raise ValueError("短剧已有剧集，不能直接删除；请先删除剧集")
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute("DELETE FROM series WHERE series_id = ?", (str(series_id),))
        return bool(result.rowcount)

    def _episodes(self, series_id: str) -> list[dict]:
        items = [item for item in self.session_index.list_sessions() if item.get("series_id") == series_id]
        items.sort(key=lambda item: (int(item.get("episode_number") or 0), str(item.get("created_at") or "")))
        return [
            {
                "session_id": item.get("session_id"),
                "episode_number": int(item.get("episode_number") or 0),
                "episode_title": item.get("episode_title") or item.get("idea") or "",
                "idea": item.get("idea") or "",
                "stage": item.get("stage") or "created",
                "summary": item.get("summary") or "",
                "previous_episode_id": item.get("previous_episode_id") or "",
                "created_at": item.get("created_at") or "",
                "updated_at": item.get("updated_at") or "",
            }
            for item in items
        ]

    def _with_progress(self, record: dict, *, include_episodes: bool = False) -> dict:
        episodes = self._episodes(record["series_id"])
        completed = sum(1 for item in episodes if item["stage"] in {"completed", "published"})
        used = {item["episode_number"] for item in episodes}
        next_number = next((number for number in range(1, record["planned_episode_count"] + 1) if number not in used),
                           record["planned_episode_count"] + 1)
        result = dict(record)
        result.update({
            "episode_count": len(episodes),
            "completed_episode_count": completed,
            "next_episode_number": next_number,
        })
        if include_episodes:
            result["episodes"] = episodes
        return result

    def prepare_episode(self, series_id: str, episode_number: Any = None) -> dict:
        series = self.get(series_id)
        number = int(episode_number or series["next_episode_number"])
        if number < 1 or number > series["planned_episode_count"]:
            raise ValueError("集数超出短剧的计划范围")
        existing = {item["episode_number"]: item for item in series["episodes"]}
        if number in existing:
            raise ValueError(f"第 {number} 集已经存在")
        previous = existing.get(number - 1)
        if number > 1 and previous is None:
            raise ValueError(f"请先创建第 {number - 1} 集")
        outline = next((item for item in series.get("outline") or []
                        if isinstance(item, dict) and int(item.get("episode_number") or 0) == number), {})
        return {
            "series": series,
            "episode_number": number,
            "episode_title": self._text(outline.get("title"), 120),
            "episode_outline": self._text(outline.get("synopsis") or outline.get("outline"), 8000),
            "previous_episode_id": str((previous or {}).get("session_id") or ""),
        }
