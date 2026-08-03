"""Regression tests for semantic prompt preflight and continuity state."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile

from pipelines.script2video_pipeline import Script2VideoPipeline
from quality.prompt_preflight import (
    PreflightStatus,
    preflight_shot,
    preflight_storyboard,
)
from server.artifacts_reader import build_manifest


def _shot(**overrides):
    values = {
        "idx": 0,
        "cam_idx": 0,
        "duration_sec": 5,
        "ff_desc": "An empty waiting room.",
        "ff_vis_char_idxs": [],
        "lf_desc": "The same waiting room.",
        "lf_vis_char_idxs": [],
        "visual_desc": "A restrained cinematic shot.",
        "motion_desc": "Static camera.",
        "beats": [],
        "visual_style": [],
        "avoid": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _issue_codes(result):
    return {issue.code for issue in result.issues}


def test_chinese_visible_actor_entry_is_detected_and_rewritten():
    result = preflight_shot(_shot(
        ff_desc="一名男子已经站在候车厅门内。",
        ff_vis_char_idxs=[0],
        lf_vis_char_idxs=[0],
        motion_desc="固定机位。男子走进候车厅，然后停下看向窗口。",
    ))

    assert result.status == PreflightStatus.REWRITTEN
    assert "actor_already_visible_before_entry" in _issue_codes(result)
    assert "走进" not in result.normalized_motion_desc
    assert "从首帧中的准确位置继续" in result.normalized_motion_desc


def test_chinese_held_prop_pickup_is_detected_and_rewritten():
    result = preflight_shot(_shot(
        ff_desc="男子双手拿着蓝色饭盒，站在长椅旁。",
        ff_vis_char_idxs=[0],
        lf_vis_char_idxs=[0],
        motion_desc="男子拿起蓝色饭盒，然后缓慢转身。",
    ))

    assert "prop_already_held_before_pickup" in _issue_codes(result)
    assert "拿起" not in result.normalized_motion_desc
    assert "继续拿着" in result.normalized_motion_desc
    assert result.initial_state.props
    assert result.initial_state.props[0].holder_character_idx == 0


def test_locked_and_moving_camera_prefers_active_direction():
    result = preflight_shot(_shot(
        motion_desc="Static camera throughout. A slow dolly push-in follows the woman.",
    ))

    assert "camera_locked_and_moving" in _issue_codes(result)
    assert result.initial_state.camera.mode == "moving"
    assert "Static camera" not in result.normalized_motion_desc
    assert "dolly push-in" in result.normalized_motion_desc


def test_pickup_transition_updates_final_prop_holder():
    result = preflight_shot(_shot(
        ff_desc="A blue lunchbox rests on the bench beside a man.",
        ff_vis_char_idxs=[0],
        lf_vis_char_idxs=[0],
        motion_desc="The man picks up the blue lunchbox from the bench and holds it.",
    ))

    pickup = next(item for item in result.transitions if item.kind == "pickup")
    prop = next(item for item in result.final_state.props if item.prop_id == pickup.prop_id)
    assert pickup.preconditions_satisfied
    assert prop.holder_character_idx == 0
    assert prop.prop_id in result.final_state.characters[0].holding


def test_valid_entry_from_offscreen_is_not_rewritten():
    result = preflight_shot(_shot(
        ff_vis_char_idxs=[],
        lf_vis_char_idxs=[0],
        motion_desc="A man enters through the left door and stops.",
    ))

    assert result.status == PreflightStatus.PASSED
    assert "enters through" in result.normalized_motion_desc


def test_storyboard_flags_unexplained_same_camera_character_jump():
    report = preflight_storyboard([
        _shot(idx=0, cam_idx=2, ff_vis_char_idxs=[0], lf_vis_char_idxs=[0]),
        _shot(idx=1, cam_idx=2, ff_vis_char_idxs=[1], lf_vis_char_idxs=[1]),
    ])

    shot_report = report["shots"]["1"]
    codes = {issue["code"] for issue in shot_report["issues"]}
    assert "cross_shot_character_state_jump" in codes
    assert shot_report["status"] == "review"
    assert report["summary"]["review"] == 1


def test_pipeline_persists_preflight_for_manifest_and_ui():
    with tempfile.TemporaryDirectory() as root:
        scene_dir = Path(root) / "idea2video" / "scene_0"
        pipeline = object.__new__(Script2VideoPipeline)
        pipeline.working_dir = str(scene_dir)

        report = pipeline._write_prompt_preflight([_shot(idx=0)])

        stored = json.loads((scene_dir / "prompt_preflight.json").read_text(encoding="utf-8"))
        assert stored["summary"] == report["summary"]
        assert (scene_dir / "shots" / "0" / "prompt_preflight.json").exists()
        manifest = build_manifest(Path(root))
        scene = manifest["scenes"][0]
        assert scene["prompt_preflight_summary"]["shot_count"] == 1
        assert scene["shots"][0]["prompt_preflight"]["status"] == "passed"
