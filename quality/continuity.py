"""Deterministic continuity contracts for generated shots.

The contract is intentionally provider-neutral.  It records which camera plate a
shot must preserve and which narrative changes are expected, so reference
selection, visual QA, and repair can share the same source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .prompt_preflight import preflight_shot
from utils.atomic import atomic_write_text


DEFAULT_SCENE_LOCKS = (
    "camera_side_and_axis",
    "spatial_projection",
    "architecture_and_openings",
    "fixed_furniture_layout",
    "major_light_sources",
    "described_camera_movement",
)

DEFAULT_WORLD_LOCKS = (
    "scene_identity",
    "spatial_topology",
    "architecture_and_materials",
    "time_weather_and_lighting",
    "screen_direction",
)


def build_continuity_contracts(camera_tree: Iterable[Any], shot_descriptions: Iterable[Any]) -> dict:
    shots = sorted(list(shot_descriptions), key=lambda item: int(getattr(item, "idx")))
    by_idx = {int(getattr(item, "idx")): item for item in shots}
    camera_for_shot: dict[int, int] = {}
    anchor_for_shot: dict[int, int] = {}
    camera_metadata: dict[int, dict] = {}

    for camera in camera_tree or ():
        active = [int(idx) for idx in (getattr(camera, "active_shot_idxs", None) or [])]
        if not active:
            continue
        camera_idx = int(getattr(camera, "idx", getattr(by_idx.get(active[0]), "cam_idx", 0)))
        anchor_idx = active[0]
        camera_metadata[camera_idx] = {
            "parent_camera_idx": getattr(camera, "parent_cam_idx", None),
            "parent_shot_idx": getattr(camera, "parent_shot_idx", None),
            "relation": str(getattr(camera, "reason", "") or "").strip(),
        }
        for shot_idx in active:
            camera_for_shot[shot_idx] = camera_idx
            anchor_for_shot[shot_idx] = anchor_idx

    # Older/imported projects can lack a camera tree.  The shot descriptions still
    # carry cam_idx, which is enough to derive a stable first-shot camera plate.
    fallback_anchors: dict[int, int] = {}
    for shot in shots:
        shot_idx = int(getattr(shot, "idx"))
        camera_idx = int(getattr(shot, "cam_idx", 0))
        fallback_anchors.setdefault(camera_idx, shot_idx)
        camera_for_shot.setdefault(shot_idx, camera_idx)
        anchor_for_shot.setdefault(shot_idx, fallback_anchors[camera_idx])

    contracts = {}
    previous_idx = None
    for shot in shots:
        shot_idx = int(getattr(shot, "idx"))
        prompt_check = preflight_shot(shot)
        prompt_check_data = prompt_check.to_dict()
        camera_idx = camera_for_shot[shot_idx]
        anchor_idx = anchor_for_shot[shot_idx]
        camera_info = camera_metadata.get(camera_idx) or {}
        previous_camera = camera_for_shot.get(previous_idx) if previous_idx is not None else None
        if shot_idx != anchor_idx:
            continuity_mode = "same_camera"
            continuity_reference_idx = anchor_idx
        else:
            parent_shot_idx = camera_info.get("parent_shot_idx")
            if parent_shot_idx is not None:
                continuity_mode = "cross_camera"
                continuity_reference_idx = int(parent_shot_idx)
            elif previous_idx is not None:
                continuity_mode = "cross_camera"
                continuity_reference_idx = previous_idx
            else:
                continuity_mode = "root"
                continuity_reference_idx = None
        expected_changes = " ".join(
            str(value or "").strip()
            for value in (
                getattr(shot, "visual_desc", ""),
                getattr(shot, "motion_desc", ""),
                getattr(shot, "variation_reason", ""),
            )
            if str(value or "").strip()
        )[:1600]
        contracts[str(shot_idx)] = {
            "shot_idx": shot_idx,
            "previous_shot_idx": previous_idx,
            "camera_idx": camera_idx,
            "camera_anchor_shot_idx": anchor_idx,
            "camera_anchor_path": f"shots/{anchor_idx}/first_frame.png",
            "parent_camera_idx": camera_info.get("parent_camera_idx"),
            "parent_shot_idx": camera_info.get("parent_shot_idx"),
            "camera_relation": camera_info.get("relation") or "",
            "continuity_mode": continuity_mode,
            "continuity_reference_shot_idx": continuity_reference_idx,
            "continuity_reference_path": (
                f"shots/{continuity_reference_idx}/first_frame.png"
                if continuity_reference_idx is not None else None
            ),
            "same_camera_as_previous": previous_camera == camera_idx if previous_camera is not None else False,
            "lock_scene_geometry": continuity_mode == "same_camera",
            "locked": list(
                DEFAULT_SCENE_LOCKS if continuity_mode == "same_camera" else DEFAULT_WORLD_LOCKS
            ),
            "expected_changes": expected_changes,
            "prompt_preflight_status": prompt_check.status.value,
            "prompt_issue_codes": [issue.code for issue in prompt_check.issues],
            "initial_state": prompt_check_data["initial_state"],
            "final_state": prompt_check_data["final_state"],
            "action_transitions": prompt_check_data["transitions"],
        }
        previous_idx = shot_idx

    return {"version": 1, "shots": contracts}


def save_continuity_contracts(path: str | Path, contracts: dict) -> str:
    target = Path(path)
    atomic_write_text(str(target), json.dumps(contracts, ensure_ascii=False, indent=2))
    return str(target)


def load_continuity_contracts(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": 1, "shots": {}}
    if not isinstance(data, dict) or not isinstance(data.get("shots"), dict):
        return {"version": 1, "shots": {}}
    return data


def scene_anchor_for_shot(contracts: dict, shot_idx: int) -> int | None:
    item = ((contracts or {}).get("shots") or {}).get(str(int(shot_idx))) or {}
    value = item.get("camera_anchor_shot_idx")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def continuity_reference_for_shot(contracts: dict, shot_idx: int) -> tuple[int | None, str]:
    item = ((contracts or {}).get("shots") or {}).get(str(int(shot_idx))) or {}
    value = item.get("continuity_reference_shot_idx")
    try:
        reference_idx = int(value) if value is not None else None
    except (TypeError, ValueError):
        reference_idx = None
    return reference_idx, str(item.get("continuity_mode") or "root")
