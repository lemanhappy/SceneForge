from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class ProjectRecord:
    """Stable project metadata independent from its rendering artifacts.

    ``project_id`` is exposed as ``session_id`` by the current API until the UI
    migration is complete. Unknown legacy fields are retained in ``extra`` so a
    JSON import never discards user data.
    """

    project_id: str
    working_dir: str
    mode: str = "idea"
    idea: str = ""
    user_requirement: str = ""
    style: str = ""
    stage: str = "created"
    summary: str = ""
    target_language: str | None = None
    aspect_ratio: str | None = None
    created_at: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    _KNOWN_FIELDS = frozenset(
        {
            "session_id",
            "project_id",
            "working_dir",
            "mode",
            "idea",
            "user_requirement",
            "style",
            "stage",
            "summary",
            "target_language",
            "aspect_ratio",
            "created_at",
            "updated_at",
        }
    )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], project_id: str | None = None) -> "ProjectRecord":
        identifier = str(project_id or value.get("project_id") or value.get("session_id") or "").strip()
        if not identifier:
            raise ValueError("project_id cannot be empty")
        working_dir = str(value.get("working_dir") or "").strip()
        if not working_dir:
            raise ValueError(f"project {identifier!r} is missing working_dir")
        return cls(
            project_id=identifier,
            working_dir=working_dir,
            mode=str(value.get("mode") or "idea"),
            idea=str(value.get("idea") or ""),
            user_requirement=str(value.get("user_requirement") or ""),
            style=str(value.get("style") or ""),
            stage=str(value.get("stage") or "created"),
            summary=str(value.get("summary") or ""),
            target_language=_optional_text(value.get("target_language")),
            aspect_ratio=_optional_text(value.get("aspect_ratio")),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            extra={key: item for key, item in value.items() if key not in cls._KNOWN_FIELDS},
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        value = dict(self.extra)
        fields = asdict(self)
        fields.pop("extra", None)
        fields["session_id"] = fields.pop("project_id")
        value.update(fields)
        return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
