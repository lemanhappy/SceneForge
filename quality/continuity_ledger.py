"""Project-local IP memory and explainable shot invalidation.

Reusable asset bibles describe facts that should survive across projects. The
continuity ledger records how those assets participate in one storyboard, so
preflight, review, and regeneration can share the same dependency evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from utils.atomic import atomic_write_text


VISUAL_CHANGE_DIMENSIONS = {
    "all",
    "visual",
    "character",
    "character_identity",
    "outfit",
    "prop",
    "scene",
    "composition",
    "camera",
    "action",
    "lighting",
}


_REPAIR_GUIDANCE = {
    "prop_already_held_before_pickup": (
        "道具在镜头开始时已经被持有，却再次执行拿起动作。",
        ["删除重复的拿起动作", "或把首帧改为道具尚未被持有"],
    ),
    "camera_locked_and_moving": (
        "同一镜头同时要求固定机位和主动运镜。",
        ["保留一种镜头运动方式", "重新生成关键帧后再生成视频"],
    ),
    "cross_shot_character_state_jump": (
        "相邻镜头的人物出入状态发生跳变。",
        ["补充人物进入或离开动作", "或让相邻镜头保持相同出镜人物"],
    ),
    "cross_shot_prop_state_jump": (
        "相邻镜头的道具持有人或摆放位置发生跳变。",
        ["补充拿起或放下动作", "或统一相邻镜头的道具状态"],
    ),
    "action_budget_too_dense": (
        "单个镜头中的状态变化过多。",
        ["拆分镜头", "或增加镜头时长并减少动作数量"],
    ),
}


def build_continuity_ledger(
    shots: Iterable[Any],
    *,
    contracts: dict | None = None,
    preflight_report: dict | None = None,
    characters: Iterable[Any] = (),
    character_bindings: dict[str, str] | None = None,
    character_assets: Any = None,
    reusable_assets: Iterable[Any] = (),
    inherited_ledger: dict | None = None,
    inheritance_source: dict | None = None,
) -> dict[str, Any]:
    ordered = sorted(list(shots or ()), key=lambda item: int(_value(item, "idx", 0) or 0))
    bindings = {str(key): str(value) for key, value in (character_bindings or {}).items()}
    characters_by_idx = {
        int(_value(item, "idx", index) or 0): item
        for index, item in enumerate(characters or ())
    }
    contract_items = ((contracts or {}).get("shots") or {})
    preflight_items = ((preflight_report or {}).get("shots") or {})
    reusable = list(reusable_assets or ())
    scene_assets = [item for item in reusable if _value(item, "asset_type", "") == "scene"]
    prop_assets = [item for item in reusable if _value(item, "asset_type", "") == "prop"]

    character_snapshots: dict[str, dict] = {}
    for asset_id in sorted(set(bindings.values())):
        asset = character_assets.get(asset_id) if character_assets is not None else None
        if asset is not None:
            character_snapshots[asset_id] = _character_snapshot(asset)
    scene_snapshots = {
        str(_value(item, "asset_id", "")): _reusable_snapshot(item)
        for item in scene_assets if _value(item, "asset_id", "")
    }
    prop_snapshots = {
        str(_value(item, "asset_id", "")): _reusable_snapshot(item)
        for item in prop_assets if _value(item, "asset_id", "")
    }
    asset_bibles = {
        "characters": character_snapshots,
        "scenes": scene_snapshots,
        "props": prop_snapshots,
    }
    asset_versions = {
        group: {asset_id: _fingerprint(snapshot) for asset_id, snapshot in snapshots.items()}
        for group, snapshots in asset_bibles.items()
    }
    inheritance = _inheritance_payload(inherited_ledger, inheritance_source)

    ledger_shots: dict[str, dict] = {}
    asset_usage: dict[str, list[int]] = {}
    for shot in ordered:
        shot_idx = int(_value(shot, "idx", 0) or 0)
        shot_text = _shot_text(shot)
        visible_indexes = sorted({
            int(index)
            for field in ("ff_vis_char_idxs", "lf_vis_char_idxs")
            for index in (_value(shot, field, []) or [])
            if index is not None
        })
        visible_character_assets = []
        visible_character_bindings: dict[str, str] = {}
        for character_idx in visible_indexes:
            character = characters_by_idx.get(character_idx)
            identifier = str(_value(character, "identifier_in_scene", "") or "")
            asset_id = bindings.get(identifier)
            if asset_id and asset_id not in visible_character_assets:
                visible_character_assets.append(asset_id)
            if asset_id:
                visible_character_bindings[str(character_idx)] = asset_id

        present_props = [
            str(_value(asset, "asset_id", ""))
            for asset in prop_assets
            if _asset_is_mentioned(asset, shot_text)
        ]
        present_scenes = [str(_value(asset, "asset_id", "")) for asset in scene_assets]
        asset_ids = [*visible_character_assets, *present_props, *present_scenes]
        for asset_id in asset_ids:
            asset_usage.setdefault(asset_id, []).append(shot_idx)

        contract = dict(contract_items.get(str(shot_idx)) or {})
        preflight = dict(preflight_items.get(str(shot_idx)) or {})
        reference_idx = contract.get("continuity_reference_shot_idx")
        try:
            dependencies = [int(reference_idx)] if reference_idx is not None else []
        except (TypeError, ValueError):
            dependencies = []
        ledger_shots[str(shot_idx)] = {
            "shot_idx": shot_idx,
            "camera_idx": _int_or_none(contract.get("camera_idx", _value(shot, "cam_idx", None))),
            "previous_shot_idx": _int_or_none(contract.get("previous_shot_idx")),
            "continuity_mode": str(contract.get("continuity_mode") or "root"),
            "depends_on_shot_idxs": dependencies,
            "character_asset_ids": visible_character_assets,
            "character_asset_bindings": visible_character_bindings,
            "prop_asset_ids": present_props,
            "scene_asset_ids": present_scenes,
            "invalidation_keys": [
                *[f"character:{asset_id}" for asset_id in visible_character_assets],
                *[f"prop:{asset_id}" for asset_id in present_props],
                *[f"scene:{asset_id}" for asset_id in present_scenes],
            ],
            "initial_state": preflight.get("initial_state") or contract.get("initial_state") or {},
            "final_state": preflight.get("final_state") or contract.get("final_state") or {},
            "transitions": preflight.get("transitions") or contract.get("action_transitions") or [],
            "preflight_status": str(
                preflight.get("status") or contract.get("prompt_preflight_status") or "unknown"
            ),
            "issue_codes": [
                str(item.get("code"))
                for item in (preflight.get("issues") or [])
                if isinstance(item, dict) and item.get("code")
            ],
        }
        ledger_shots[str(shot_idx)]["repair_suggestions"] = repair_suggestions(
            ledger_shots[str(shot_idx)]["issue_codes"]
        )

    if inheritance.get("enabled") and ledger_shots:
        first_key = min(ledger_shots, key=lambda key: int(ledger_shots[key].get("shot_idx", key)))
        first = ledger_shots[first_key]
        first["inherited_from"] = {
            key: inheritance.get(key)
            for key in ("source_session_id", "source_scene_index", "source_shot_idx")
            if inheritance.get(key) is not None
        }
        first["inherited_state"] = inheritance.get("final_state") or {}
        first["repair_suggestions"].extend(
            _inheritance_repair_suggestions(inheritance.get("final_state") or {}, first.get("initial_state") or {})
        )

    return {
        "version": 2,
        "asset_bibles": asset_bibles,
        "asset_versions": asset_versions,
        "asset_usage": {key: sorted(set(value)) for key, value in sorted(asset_usage.items())},
        "inheritance": inheritance,
        "shots": ledger_shots,
        "summary": {
            "shot_count": len(ledger_shots),
            "tracked_character_count": len(character_snapshots),
            "tracked_scene_count": len(scene_snapshots),
            "tracked_prop_count": len(prop_snapshots),
            "state_transition_count": sum(
                len(item.get("transitions") or []) for item in ledger_shots.values()
            ),
            "repair_suggestion_count": sum(
                len(item.get("repair_suggestions") or []) for item in ledger_shots.values()
            ),
            "inherits_previous_state": bool(inheritance.get("enabled")),
        },
    }


def regeneration_impact(
    ledger: dict,
    shot_idx: int,
    dimensions: Iterable[str] = (),
) -> dict[str, Any]:
    target = int(shot_idx)
    items = (ledger or {}).get("shots") or {}
    if str(target) not in items:
        raise ValueError(f"unknown shot index: {target}")
    normalized = sorted({str(item).strip().lower() for item in dimensions if str(item).strip()})
    propagate = not normalized or bool(set(normalized) & VISUAL_CHANGE_DIMENSIONS)
    reasons: dict[int, list[str]] = {target: ["direct_edit"]}
    if propagate:
        children: dict[int, list[int]] = {}
        for key, item in items.items():
            child = int(item.get("shot_idx", key))
            for parent in item.get("depends_on_shot_idxs") or []:
                try:
                    children.setdefault(int(parent), []).append(child)
                except (TypeError, ValueError):
                    continue
        stack = [target]
        while stack:
            parent = stack.pop()
            for child in children.get(parent, []):
                reason = f"continuity_reference:{parent}"
                if child not in reasons:
                    reasons[child] = [reason]
                    stack.append(child)
                elif reason not in reasons[child]:
                    reasons[child].append(reason)
    affected = [
        {"shot_idx": index, "reasons": reasons[index]}
        for index in sorted(reasons)
    ]
    return {
        "shot_idx": target,
        "dimensions": normalized,
        "scope": "continuity_chain" if len(affected) > 1 else "single_shot",
        "affected_count": len(affected),
        "affected_shots": affected,
    }


def asset_change_impact(ledger: dict, asset_id: str) -> dict[str, Any]:
    normalized = str(asset_id or "").strip()
    indexes = sorted({int(item) for item in ((ledger or {}).get("asset_usage") or {}).get(normalized, [])})
    return {
        "asset_id": normalized,
        "affected_count": len(indexes),
        "affected_shots": [
            {"shot_idx": index, "reasons": [f"asset_bible:{normalized}"]}
            for index in indexes
        ],
    }


def continuity_handoff(ledger: dict) -> dict[str, Any]:
    """Return the final known state that a later scene or episode can inherit."""
    items = (ledger or {}).get("shots") or {}
    if not items:
        return {
            "source_shot_idx": None,
            "final_state": {},
            "character_asset_ids": [],
            "prop_asset_ids": [],
            "scene_asset_ids": [],
        }
    last = max(items.values(), key=lambda item: int(item.get("shot_idx", 0) or 0))
    return {
        "source_shot_idx": int(last.get("shot_idx", 0) or 0),
        "final_state": dict(last.get("final_state") or {}),
        "character_asset_ids": list(last.get("character_asset_ids") or []),
        "prop_asset_ids": list(last.get("prop_asset_ids") or []),
        "scene_asset_ids": list(last.get("scene_asset_ids") or []),
    }


def repair_suggestions(issue_codes: Iterable[str]) -> list[dict[str, Any]]:
    suggestions = []
    for code in dict.fromkeys(str(item) for item in (issue_codes or []) if str(item)):
        guidance = _REPAIR_GUIDANCE.get(code)
        if guidance is None:
            suggestions.append({
                "code": code,
                "severity": "warning",
                "message": "该镜头存在连续性问题，需要人工复核。",
                "actions": ["检查首尾状态与相邻镜头是否一致"],
            })
            continue
        message, actions = guidance
        suggestions.append({
            "code": code,
            "severity": "warning",
            "message": message,
            "actions": list(actions),
        })
    return suggestions


def evaluate_asset_invalidations(
    ledger: dict,
    *,
    character_assets: Any = None,
    reusable_assets: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Compare current asset definitions with the render-time ledger snapshot.

    Direct users of a changed asset are marked first, then only descendants in
    the recorded continuity graph are propagated. No project files are mutated.
    """
    baseline = (ledger or {}).get("asset_bibles") or {}
    stored_versions = (ledger or {}).get("asset_versions") or {}
    current: dict[str, dict[str, dict]] = {"characters": {}, "scenes": {}, "props": {}}
    available = {
        "characters": character_assets is not None,
        "scenes": reusable_assets is not None,
        "props": reusable_assets is not None,
    }
    if character_assets is not None:
        for asset_id in (baseline.get("characters") or {}):
            asset = character_assets.get(asset_id) if hasattr(character_assets, "get") else None
            if asset is None and not hasattr(character_assets, "get"):
                asset = next(
                    (item for item in character_assets if str(_value(item, "asset_id", "")) == asset_id),
                    None,
                )
            if asset is not None:
                current["characters"][asset_id] = _character_snapshot(asset)
    if reusable_assets is not None:
        for asset in reusable_assets:
            group = "scenes" if _value(asset, "asset_type", "") == "scene" else "props"
            asset_id = str(_value(asset, "asset_id", ""))
            if asset_id:
                current[group][asset_id] = _reusable_snapshot(asset)

    changed_assets: list[dict[str, Any]] = []
    for group in ("characters", "scenes", "props"):
        if not available[group]:
            continue
        for asset_id, previous in (baseline.get(group) or {}).items():
            latest = current[group].get(asset_id)
            stored_version = str((stored_versions.get(group) or {}).get(asset_id) or "")
            previous_version = stored_version or _fingerprint(previous)
            comparable_latest = latest if stored_version else _project_like(latest, previous)
            latest_version = _fingerprint(comparable_latest) if comparable_latest is not None else ""
            if latest_version == previous_version:
                continue
            changed_assets.append({
                "asset_id": asset_id,
                "asset_type": {"characters": "character", "scenes": "scene", "props": "prop"}[group],
                "change_kind": "removed" if latest is None else "updated",
                "changed_fields": ["asset_removed"] if latest is None else _changed_paths(previous, comparable_latest),
                "previous_version": previous_version,
                "current_version": latest_version or None,
            })

    per_shot: dict[str, dict[str, list]] = {}
    usage = (ledger or {}).get("asset_usage") or {}
    for changed in changed_assets:
        asset_id = changed["asset_id"]
        direct = sorted({int(item) for item in usage.get(asset_id, [])})
        for direct_idx in direct:
            impact = regeneration_impact(ledger, direct_idx, [changed["asset_type"]])
            for affected in impact.get("affected_shots") or []:
                shot_idx = int(affected["shot_idx"])
                item = per_shot.setdefault(str(shot_idx), {"invalidations": [], "repair_suggestions": []})
                direct_change = shot_idx == direct_idx
                invalidation = {
                    **changed,
                    "direct": direct_change,
                    "source_shot_idx": direct_idx,
                    "reason": "asset_snapshot_changed" if direct_change else "continuity_dependency",
                }
                key = (asset_id, direct_idx, direct_change)
                existing = {
                    (entry.get("asset_id"), entry.get("source_shot_idx"), entry.get("direct"))
                    for entry in item["invalidations"]
                }
                if key not in existing:
                    item["invalidations"].append(invalidation)
                suggestion_code = f"refresh_asset:{asset_id}:{direct_idx}"
                if not any(entry.get("code") == suggestion_code for entry in item["repair_suggestions"]):
                    item["repair_suggestions"].append({
                        "code": suggestion_code,
                        "severity": "warning",
                        "message": (
                            f"资产“{asset_id}”已更新，需要重新生成当前镜头。"
                            if direct_change else
                            f"镜头继承自受资产“{asset_id}”影响的镜头 {direct_idx + 1}。"
                        ),
                        "actions": [
                            "从最早受影响镜头开始按连续性链重新生成",
                            "生成后复核人物、道具与场景状态",
                        ],
                    })

    stale_indexes = sorted(int(key) for key in per_shot)
    direct_indexes = sorted({
        int(key)
        for key, item in per_shot.items()
        if any(entry.get("direct") for entry in item.get("invalidations") or [])
    })
    return {
        "status": "stale" if changed_assets else "current",
        "changed_assets": changed_assets,
        "shots": per_shot,
        "summary": {
            "changed_asset_count": len(changed_assets),
            "direct_stale_shot_count": len(direct_indexes),
            "stale_shot_count": len(stale_indexes),
            "stale_shot_idxs": stale_indexes,
        },
    }


def save_continuity_ledger(path: str | Path, ledger: dict) -> str:
    target = Path(path)
    atomic_write_text(str(target), json.dumps(ledger, ensure_ascii=False, indent=2))
    return str(target)


def load_continuity_ledger(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"version": 1, "asset_bibles": {}, "asset_usage": {}, "shots": {}, "summary": {}}
    if not isinstance(value, dict) or not isinstance(value.get("shots"), dict):
        return {"version": 1, "asset_bibles": {}, "asset_usage": {}, "shots": {}, "summary": {}}
    return value


def _character_snapshot(asset: Any) -> dict:
    return {
        "asset_id": str(_value(asset, "asset_id", "")),
        "display_name": str(_value(asset, "display_name", "")),
        "description": str(_value(asset, "description", "")),
        "visual_prompt": str(_value(asset, "visual_prompt", "") or ""),
        "identity_profile": _model_dict(_value(asset, "identity_profile", {})),
        "bible": _model_dict(_value(asset, "bible", {})),
        "outfit_versions": [_model_dict(item) for item in (_value(asset, "outfit_versions", []) or [])],
        "reference_sets": [_model_dict(item) for item in (_value(asset, "reference_sets", []) or [])],
        "render_bindings": [_model_dict(item) for item in (_value(asset, "render_bindings", []) or [])],
        "reference_files": _character_reference_files(asset),
    }


def _reusable_snapshot(asset: Any) -> dict:
    return {
        "asset_id": str(_value(asset, "asset_id", "")),
        "asset_type": str(_value(asset, "asset_type", "")),
        "display_name": str(_value(asset, "display_name", "")),
        "description": str(_value(asset, "description", "")),
        "visual_prompt": str(_value(asset, "visual_prompt", "")),
        "negative_prompt": str(_value(asset, "negative_prompt", "")),
        "consistency_notes": str(_value(asset, "consistency_notes", "")),
        "scene_bible": _model_dict(_value(asset, "scene_bible", None)),
        "prop_bible": _model_dict(_value(asset, "prop_bible", None)),
        "reference_files": _reference_file_records(_value(asset, "assets", {}) or {}),
    }


def _inheritance_payload(ledger: dict | None, source: dict | None) -> dict[str, Any]:
    if not ledger or not (ledger.get("shots") or {}):
        return {"enabled": False}
    handoff = continuity_handoff(ledger)
    return {"enabled": True, **dict(source or {}), **handoff}


def _inheritance_repair_suggestions(previous: dict, current: dict) -> list[dict[str, Any]]:
    previous_props = {
        str(item.get("prop_id")): item
        for item in (previous.get("props") or [])
        if isinstance(item, dict) and item.get("prop_id")
    }
    current_props = {
        str(item.get("prop_id")): item
        for item in (current.get("props") or [])
        if isinstance(item, dict) and item.get("prop_id")
    }
    suggestions = []
    for prop_id, before in previous_props.items():
        after = current_props.get(prop_id)
        held_before = before.get("holder_character_idx") is not None
        if after is None and not held_before:
            continue
        if after is None or (
            before.get("holder_character_idx"), before.get("support")
        ) != (
            after.get("holder_character_idx"), after.get("support")
        ):
            suggestions.append({
                "code": f"inherited_prop_state:{prop_id}",
                "severity": "warning",
                "message": f"继承状态中的道具“{before.get('label') or prop_id}”与本集首镜状态不一致。",
                "actions": ["让首镜沿用上一集道具位置或持有人", "或在剧本中补充明确的状态变化"],
            })
    return suggestions


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _changed_paths(previous: Any, current: Any, prefix: str = "") -> list[str]:
    if isinstance(previous, dict) and isinstance(current, dict):
        paths = []
        for key in sorted(set(previous) | set(current)):
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_changed_paths(previous.get(key), current.get(key), path))
            if len(paths) >= 24:
                return paths[:24]
        return paths
    if isinstance(previous, list) and isinstance(current, list):
        if _fingerprint(previous) == _fingerprint(current):
            return []
        return [prefix or "value"]
    return [] if previous == current else [prefix or "value"]


def _project_like(current: Any, shape: Any) -> Any:
    """Project current data onto an old ledger shape for v1 compatibility."""
    if current is None:
        return None
    if isinstance(shape, dict) and isinstance(current, dict):
        return {key: _project_like(current.get(key), value) for key, value in shape.items()}
    return current


def _character_reference_files(asset: Any) -> list[dict[str, str]]:
    records = _reference_file_records(_value(asset, "assets", {}) or {}, prefix="legacy")
    for reference_set in _value(asset, "reference_sets", []) or []:
        set_id = str(_value(reference_set, "reference_set_id", "default") or "default")
        records.extend(_reference_file_records(_value(reference_set, "images", {}) or {}, prefix=set_id))
        records.extend(_reference_file_records(_value(reference_set, "expressions", {}) or {}, prefix=f"{set_id}:expression"))
    unique = {(item["key"], item["path"]): item for item in records}
    return [unique[key] for key in sorted(unique)]


def _reference_file_records(values: dict, prefix: str = "reference") -> list[dict[str, str]]:
    records = []
    for key, raw_path in sorted((values or {}).items()):
        path = str(raw_path or "")
        if not path:
            continue
        record = {"key": f"{prefix}:{key}", "path": path, "sha256": ""}
        try:
            target = Path(path)
            if target.is_file():
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                record["sha256"] = digest.hexdigest()
        except OSError:
            pass
        records.append(record)
    return records


def _asset_is_mentioned(asset: Any, text: str) -> bool:
    haystack = str(text or "").casefold()
    candidates = [
        _value(asset, "asset_id", ""),
        _value(asset, "display_name", ""),
        *(_value(asset, "aliases", []) or []),
    ]
    return any(
        str(candidate).strip().casefold() in haystack
        for candidate in candidates
        if len(str(candidate).strip()) >= 2
    )


def _shot_text(shot: Any) -> str:
    values = [
        _value(shot, name, "")
        for name in (
            "ff_desc", "lf_desc", "visual_desc", "motion_desc", "director_desc",
            "audio_desc", "screen_text", "variation_reason",
        )
    ]
    for beat in _value(shot, "beats", []) or []:
        values.extend(_value(beat, name, "") for name in ("camera", "action", "performance"))
    return " ".join(str(value or "") for value in values)


def _model_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return {}


def _value(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
