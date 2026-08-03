"""Semantic preflight for storyboard-to-video prompts.

Text prompts are not a reliable place to resolve contradictory world states. This
module derives a small provider-neutral continuity snapshot, validates action
preconditions, and normalizes only conflicts whose intended resolution is clear.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from utils.atomic import atomic_write_text


class PreflightSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PreflightStatus(str, Enum):
    PASSED = "passed"
    REWRITTEN = "rewritten"
    REVIEW = "review"
    BLOCKED = "blocked"


@dataclass(slots=True)
class CharacterContinuityState:
    character_idx: int
    visible: bool = True
    location: str = "visible_frame"
    holding: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PropContinuityState:
    prop_id: str
    label: str
    count: int = 1
    holder_character_idx: int | None = None
    support: str | None = None


@dataclass(slots=True)
class CameraContinuityState:
    camera_idx: int | None = None
    mode: str = "unspecified"
    directives: list[str] = field(default_factory=list)
    has_conflict: bool = False


@dataclass(slots=True)
class ContinuityState:
    characters: list[CharacterContinuityState] = field(default_factory=list)
    props: list[PropContinuityState] = field(default_factory=list)
    scene_id: str = ""
    camera: CameraContinuityState = field(default_factory=CameraContinuityState)


@dataclass(slots=True)
class ActionTransition:
    kind: str
    raw: str
    character_idx: int | None = None
    prop_id: str | None = None
    preconditions_satisfied: bool = True
    effects: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PromptIssue:
    code: str
    severity: PreflightSeverity
    message: str
    fields: list[str] = field(default_factory=list)
    auto_fixed: bool = False


@dataclass(slots=True)
class ShotPreflightResult:
    shot_idx: int
    status: PreflightStatus
    initial_state: ContinuityState
    final_state: ContinuityState
    transitions: list[ActionTransition]
    issues: list[PromptIssue]
    normalized_motion_desc: str
    normalized_beats: list[dict[str, Any]]

    @property
    def rewritten(self) -> bool:
        return any(issue.auto_fixed for issue in self.issues)

    def prompt_input(self, shot: Any) -> dict[str, Any]:
        return {
            "duration_sec": _value(shot, "duration_sec", 5),
            "motion_desc": self.normalized_motion_desc,
            "visual_desc": _value(shot, "visual_desc", ""),
            "beats": self.normalized_beats,
            "visual_style": _value(shot, "visual_style", []),
            "avoid": _value(shot, "avoid", []),
        }

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


_ENTRY_ACTION = re.compile(
    r"\b(?:enter|enters|entered|entering|arrive|arrives|arrived|arriving|"
    r"walks?\s+in|walking\s+in|steps?\s+in(?:to)?|stepping\s+in(?:to)?)\b|"
    r"进入|走进|步入|进门|入场|闯入|推门而入|到达",
    re.IGNORECASE,
)
_EXIT_ACTION = re.compile(
    r"\b(?:exit|exits|exited|exiting|leave|leaves|left|leaving|walks?\s+out|"
    r"steps?\s+out)\b|离开|走出|退出|出门",
    re.IGNORECASE,
)
_PICKUP_ACTION = re.compile(
    r"\b(?:pick(?:s|ed|ing)?\s+up|lift(?:s|ed|ing)?)\s+"
    r"(?P<object>(?:the\s+|a\s+|an\s+)?[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*){0,3}?)"
    r"(?=\s+from\b|\s*,|[.;]|$)",
    re.IGNORECASE,
)
_PICKUP_CN = re.compile(r"拿起|拾起|捡起|提起")
_PUTDOWN_ACTION = re.compile(
    r"\b(?:put(?:s|ting)?\s+down|set(?:s|ting)?\s+down|place(?:s|d|ing)?)\b|"
    r"放下|放到|放在|搁下",
    re.IGNORECASE,
)
_OPEN_ACTION = re.compile(r"\bopen(?:s|ed|ing)?\b|打开|推开", re.IGNORECASE)
_CLOSE_ACTION = re.compile(r"\bclose(?:s|d|ing)?\b|关闭|关上", re.IGNORECASE)
_HELD_STATE = re.compile(
    r"\b(?:held|holds|holding|carries|carrying|cradles|cradling|"
    r"in (?:his|her|their) hands?)\b|拿着|手持|抱着|提着",
    re.IGNORECASE,
)
_HELD_OBJECT_EN = re.compile(
    r"\b(?:holds|holding|carries|carrying|cradles|cradling)\s+"
    r"(?:the\s+|a\s+|an\s+)?(?P<object>[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*){0,3}?)"
    r"(?=\s+(?:with|in|near|at|while|and)\b|[.,;]|$)",
    re.IGNORECASE,
)
_HELD_OBJECT_CN = re.compile(
    r"(?:拿着|手持|抱着|提着)\s*(?P<object>[\u4e00-\u9fffA-Za-z0-9_-]{1,10})"
)

_LOCKED_CAMERA = re.compile(
    r"\b(?:static camera|fixed camera|fixed shot|locked camera|locked shot|"
    r"locked framing|tripod shot|camera remains still)\b|"
    r"固定机位|固定镜头|镜头固定|相机固定|三脚架机位",
    re.IGNORECASE,
)
_ACTIVE_CAMERA = re.compile(
    r"\b(?:dolly|push[ -]?in|pull[ -]?back|pan(?:s|ned|ning)?|"
    r"tilt(?:s|ed|ing)?|zoom(?:s|ed|ing)?|orbit(?:s|ed|ing)?|"
    r"tracking shot|track(?:s|ed|ing)?|crane|handheld|whip pan)\b|"
    r"推进|拉远|摇镜|摇摄|平移|跟拍|环绕|升降镜头|手持镜头",
    re.IGNORECASE,
)


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _beat_dict(beat: Any) -> dict[str, Any]:
    if isinstance(beat, dict):
        return dict(beat)
    if hasattr(beat, "model_dump"):
        return beat.model_dump()
    return {
        "start_sec": _value(beat, "start_sec", 0),
        "end_sec": _value(beat, "end_sec", 5),
        "camera": _value(beat, "camera", ""),
        "action": _value(beat, "action", ""),
        "performance": _value(beat, "performance", ""),
    }


def camera_state_for_shot(shot: Any) -> CameraContinuityState:
    texts = [str(_value(shot, "motion_desc", "") or "")]
    texts.extend(str(_value(beat, "camera", "") or "") for beat in (_value(shot, "beats", []) or []))
    combined = " ".join(texts)
    locked = bool(_LOCKED_CAMERA.search(combined))
    active = bool(_ACTIVE_CAMERA.search(combined))
    if active:
        mode = "moving"
    elif locked:
        mode = "locked"
    else:
        mode = "unspecified"
    directives = []
    if locked:
        directives.append("locked")
    if active:
        directives.append("moving")
    camera_idx = _value(shot, "cam_idx", None)
    return CameraContinuityState(
        camera_idx=int(camera_idx) if camera_idx is not None else None,
        mode=mode,
        directives=directives,
        has_conflict=locked and active,
    )


def _normalize_prop_label(value: str) -> str:
    value = re.sub(r"^(?:the|a|an)\s+", "", str(value or "").strip(), flags=re.I)
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", value.lower()).strip("_")
    return value[:80]


def _held_props(first_frame: str, visible: list[int]) -> list[PropContinuityState]:
    found: list[str] = []
    for match in _HELD_OBJECT_EN.finditer(first_frame):
        found.append(match.group("object"))
    for match in _HELD_OBJECT_CN.finditer(first_frame):
        found.append(match.group("object"))
    holder = visible[0] if len(visible) == 1 else None
    props = []
    seen = set()
    for label in found:
        prop_id = _normalize_prop_label(label)
        if not prop_id or prop_id in seen:
            continue
        seen.add(prop_id)
        props.append(PropContinuityState(
            prop_id=prop_id,
            label=str(label).strip(),
            holder_character_idx=holder,
        ))
    return props


def _pickup_objects(text: str) -> list[str]:
    return [match.group("object").strip() for match in _PICKUP_ACTION.finditer(text)]


def _entry_conflict(shot: Any, action_text: str) -> bool:
    return bool(_value(shot, "ff_vis_char_idxs", []) or []) and bool(_ENTRY_ACTION.search(action_text))


def _pickup_conflict(first_frame: str, action_text: str) -> bool:
    if not _HELD_STATE.search(first_frame):
        return False
    if _PICKUP_CN.search(action_text):
        return True
    first_lower = first_frame.lower()
    for object_phrase in _pickup_objects(action_text):
        tokens = [token for token in _normalize_prop_label(object_phrase).split("_") if len(token) > 2]
        if tokens and any(token in first_lower for token in tokens):
            return True
    return False


def _rewrite_entry(text: str) -> str:
    replacement = (
        "从首帧中的准确位置继续向室内迈出一小步"
        if re.search(r"[\u4e00-\u9fff]", text or "")
        else "continues inward from the exact time-zero reference position"
    )
    return _ENTRY_ACTION.sub(replacement, str(text or ""))


def _rewrite_pickup(text: str) -> str:
    def replace(match: re.Match) -> str:
        return f"continues holding {match.group('object')}"

    rewritten = _PICKUP_ACTION.sub(replace, str(text or ""))
    return _PICKUP_CN.sub("继续拿着", rewritten)


def _remove_locked_camera_claims(text: str) -> str:
    parts = re.split(r"(?<=[.!?。！？])\s*", str(text or "").strip())
    kept = []
    for part in parts:
        if not part:
            continue
        if _LOCKED_CAMERA.search(part) and not _ACTIVE_CAMERA.search(part):
            continue
        cleaned = _LOCKED_CAMERA.sub("", part)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;，；")
        if cleaned:
            kept.append(cleaned)
    return " ".join(kept)


def _transitions(action_texts: Iterable[str], visible: list[int], first_frame: str) -> list[ActionTransition]:
    result: list[ActionTransition] = []
    sole_character = visible[0] if len(visible) == 1 else None
    for raw in action_texts:
        raw = str(raw or "").strip()
        if not raw:
            continue
        if _ENTRY_ACTION.search(raw):
            valid = not bool(visible)
            result.append(ActionTransition(
                kind="enter",
                raw=raw,
                character_idx=sole_character,
                preconditions_satisfied=valid,
                effects={"visible": True},
            ))
        if _EXIT_ACTION.search(raw):
            result.append(ActionTransition(
                kind="exit",
                raw=raw,
                character_idx=sole_character,
                preconditions_satisfied=bool(visible),
                effects={"visible": False},
            ))
        pickup_objects = _pickup_objects(raw)
        if pickup_objects or _PICKUP_CN.search(raw):
            label = pickup_objects[0] if pickup_objects else "未命名道具"
            prop_id = _normalize_prop_label(label)
            valid = not _pickup_conflict(first_frame, raw)
            result.append(ActionTransition(
                kind="pickup",
                raw=raw,
                character_idx=sole_character,
                prop_id=prop_id,
                preconditions_satisfied=valid,
                effects={"holder_character_idx": sole_character},
            ))
        if _PUTDOWN_ACTION.search(raw):
            result.append(ActionTransition(
                kind="put_down",
                raw=raw,
                character_idx=sole_character,
                preconditions_satisfied=bool(_HELD_STATE.search(first_frame)),
                effects={"holder_character_idx": None},
            ))
        if _OPEN_ACTION.search(raw):
            result.append(ActionTransition(kind="open", raw=raw, effects={"open": True}))
        if _CLOSE_ACTION.search(raw):
            result.append(ActionTransition(kind="close", raw=raw, effects={"open": False}))
    return result


def _status_for(issues: list[PromptIssue]) -> PreflightStatus:
    if any(issue.severity == PreflightSeverity.ERROR and not issue.auto_fixed for issue in issues):
        return PreflightStatus.BLOCKED
    if any(issue.auto_fixed for issue in issues):
        return PreflightStatus.REWRITTEN
    if any(issue.severity == PreflightSeverity.WARNING for issue in issues):
        return PreflightStatus.REVIEW
    return PreflightStatus.PASSED


def preflight_shot(shot: Any) -> ShotPreflightResult:
    shot_idx = int(_value(shot, "idx", 0) or 0)
    first_frame = str(_value(shot, "ff_desc", "") or "")
    motion = str(_value(shot, "motion_desc", "") or "")
    beats = [_beat_dict(beat) for beat in (_value(shot, "beats", []) or [])]
    beat_actions = [str(beat.get("action", "") or "") for beat in beats]
    action_text = " ".join([motion, *beat_actions])
    visible_first = sorted(set(int(idx) for idx in (_value(shot, "ff_vis_char_idxs", []) or [])))
    visible_last = sorted(set(int(idx) for idx in (_value(shot, "lf_vis_char_idxs", []) or [])))
    camera = camera_state_for_shot(shot)
    issues: list[PromptIssue] = []

    entry_conflict = _entry_conflict(shot, action_text)
    pickup_conflict = _pickup_conflict(first_frame, action_text)
    if entry_conflict:
        issues.append(PromptIssue(
            code="actor_already_visible_before_entry",
            severity=PreflightSeverity.ERROR,
            message="A character visible at time zero was instructed to enter again.",
            fields=["ff_vis_char_idxs", "motion_desc", "beats.action"],
            auto_fixed=True,
        ))
        motion = _rewrite_entry(motion)
        for beat in beats:
            beat["action"] = _rewrite_entry(str(beat.get("action", "") or ""))

    if pickup_conflict:
        issues.append(PromptIssue(
            code="prop_already_held_before_pickup",
            severity=PreflightSeverity.ERROR,
            message="A prop already held at time zero was instructed to be picked up again.",
            fields=["ff_desc", "motion_desc", "beats.action"],
            auto_fixed=True,
        ))
        motion = _rewrite_pickup(motion)
        for beat in beats:
            beat["action"] = _rewrite_pickup(str(beat.get("action", "") or ""))

    if camera.has_conflict:
        issues.append(PromptIssue(
            code="camera_locked_and_moving",
            severity=PreflightSeverity.ERROR,
            message="The shot contains both locked-camera and active-camera directions; active movement takes precedence.",
            fields=["motion_desc", "beats.camera"],
            auto_fixed=True,
        ))
        motion = _remove_locked_camera_claims(motion)
        for beat in beats:
            beat["camera"] = _remove_locked_camera_claims(str(beat.get("camera", "") or ""))

    transitions = _transitions(
        [str(_value(shot, "motion_desc", "") or ""), *beat_actions],
        visible_first,
        first_frame,
    )
    held_props = _held_props(first_frame, visible_first)
    initial_characters = [CharacterContinuityState(character_idx=idx) for idx in visible_first]
    for prop in held_props:
        if prop.holder_character_idx is not None:
            for character in initial_characters:
                if character.character_idx == prop.holder_character_idx:
                    character.holding.append(prop.prop_id)

    initial = ContinuityState(
        characters=initial_characters,
        props=held_props,
        camera=camera,
    )
    final = ContinuityState(
        characters=[CharacterContinuityState(character_idx=idx) for idx in visible_last],
        props=[PropContinuityState(**asdict(prop)) for prop in held_props],
        camera=CameraContinuityState(**asdict(camera)),
    )
    props_by_id = {prop.prop_id: prop for prop in final.props}
    for transition in transitions:
        if not transition.preconditions_satisfied:
            continue
        if transition.kind == "pickup" and transition.prop_id:
            prop = props_by_id.get(transition.prop_id)
            if prop is None:
                prop = PropContinuityState(
                    prop_id=transition.prop_id,
                    label=transition.prop_id.replace("_", " "),
                )
                props_by_id[prop.prop_id] = prop
                final.props.append(prop)
            prop.holder_character_idx = transition.character_idx
            prop.support = None
        elif transition.kind == "put_down":
            for prop in final.props:
                if transition.character_idx is None or prop.holder_character_idx == transition.character_idx:
                    prop.holder_character_idx = None
                    prop.support = "unspecified_surface"
    for character in final.characters:
        character.holding = [
            prop.prop_id
            for prop in final.props
            if prop.holder_character_idx == character.character_idx
        ]

    action_count = len({transition.kind for transition in transitions})
    duration = float(_value(shot, "duration_sec", 5) or 5)
    if action_count >= 4 and duration / action_count < 1.5:
        issues.append(PromptIssue(
            code="action_budget_too_dense",
            severity=PreflightSeverity.WARNING,
            message=f"{action_count} state-changing actions are packed into {duration:g} seconds.",
            fields=["duration_sec", "motion_desc", "beats"],
        ))

    return ShotPreflightResult(
        shot_idx=shot_idx,
        status=_status_for(issues),
        initial_state=initial,
        final_state=final,
        transitions=transitions,
        issues=issues,
        normalized_motion_desc=motion,
        normalized_beats=beats,
    )


def preflight_storyboard(shots: Iterable[Any]) -> dict[str, Any]:
    ordered = sorted(list(shots), key=lambda shot: int(_value(shot, "idx", 0) or 0))
    results = [preflight_shot(shot) for shot in ordered]
    for previous, current in zip(results, results[1:]):
        previous_camera = previous.final_state.camera.camera_idx
        current_camera = current.initial_state.camera.camera_idx
        same_camera = previous_camera is not None and previous_camera == current_camera
        previous_visible = {item.character_idx for item in previous.final_state.characters}
        current_visible = {item.character_idx for item in current.initial_state.characters}
        transition_kinds = {item.kind for item in [*previous.transitions, *current.transitions]}
        if same_camera and previous_visible != current_visible and not ({"enter", "exit"} & transition_kinds):
            current.issues.append(PromptIssue(
                code="cross_shot_character_state_jump",
                severity=PreflightSeverity.WARNING,
                message=(
                    "Adjacent shots on the same camera change visible characters without an explicit "
                    "entry or exit action."
                ),
                fields=["previous.lf_vis_char_idxs", "current.ff_vis_char_idxs"],
            ))
            current.status = _status_for(current.issues)

        previous_props = {item.prop_id: item for item in previous.final_state.props}
        current_props = {item.prop_id: item for item in current.initial_state.props}
        for prop_id in sorted(previous_props.keys() & current_props.keys()):
            before = previous_props[prop_id]
            after = current_props[prop_id]
            if (
                same_camera
                and (before.holder_character_idx, before.support)
                != (after.holder_character_idx, after.support)
            ):
                current.issues.append(PromptIssue(
                    code="cross_shot_prop_state_jump",
                    severity=PreflightSeverity.WARNING,
                    message=(
                        f"Prop '{after.label}' changes holder or support between adjacent "
                        "same-camera shots without a pickup or put-down action."
                    ),
                    fields=["previous.final_state.props", "current.initial_state.props"],
                ))
                current.status = _status_for(current.issues)

    counts = {status.value: 0 for status in PreflightStatus}
    for result in results:
        counts[result.status.value] += 1
    return {
        "version": 1,
        "summary": {
            "shot_count": len(results),
            "issue_count": sum(len(result.issues) for result in results),
            **counts,
        },
        "shots": {str(result.shot_idx): result.to_dict() for result in results},
    }


def save_prompt_preflight_report(path: str | Path, report: dict[str, Any]) -> str:
    target = Path(path)
    atomic_write_text(str(target), json.dumps(report, ensure_ascii=False, indent=2))
    return str(target)
