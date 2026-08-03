import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.production_api import ProductionAPI
from services.timeline_editor import TimelineEditService


def _project(tmp_path: Path) -> Path:
    working = tmp_path / "project"
    idea = working / "idea2video"
    for scene_index, shots in (
        (0, [{"idx": 0, "duration_sec": 4}, {"idx": 1, "duration_sec": 6}]),
        (1, [{"idx": 0, "duration_sec": 5}]),
    ):
        scene = idea / f"scene_{scene_index}"
        scene.mkdir(parents=True, exist_ok=True)
        (scene / "storyboard.json").write_text(
            json.dumps(shots), encoding="utf-8"
        )
    (idea / "final_video.mp4").write_bytes(b"original-final")
    return working


def test_default_plan_scales_storyboard_ranges_to_real_duration(tmp_path: Path):
    working = _project(tmp_path)
    service = TimelineEditService(working, duration_provider=lambda _path: 12.0)

    plan = service.get_plan()

    assert [item["clip_id"] for item in plan["clips"]] == ["0_0", "0_1", "1_0"]
    assert [item["source_duration"] for item in plan["clips"]] == [3.2, 4.8, 4.0]
    assert plan["source_duration"] == 12.0
    assert plan["output_duration"] == 12.0
    assert plan["source_status"] == "will_initialize"


def test_default_plan_falls_back_for_invalid_storyboard_duration(tmp_path: Path):
    working = _project(tmp_path)
    storyboard = working / "idea2video" / "scene_0" / "storyboard.json"
    storyboard.write_text(
        json.dumps([
            {"idx": 0, "duration_sec": "invalid"},
            {"idx": 1, "duration_sec": "NaN"},
        ]),
        encoding="utf-8",
    )
    service = TimelineEditService(
        working, duration_provider=lambda _path: float("nan")
    )

    plan = service.get_plan()

    assert [item["source_duration"] for item in plan["clips"]] == [5.0, 5.0, 5.0]
    assert service.has_original_source() is False
    with pytest.raises(ValueError, match="original edit source"):
        service.reset()


def test_plan_validates_reorder_trim_and_transition_math(tmp_path: Path):
    working = _project(tmp_path)
    service = TimelineEditService(working, duration_provider=lambda _path: 15.0)
    plan = service.get_plan()
    plan["clips"].reverse()
    plan["clips"][0]["trim_start"] = 1.0
    plan["clips"][0]["trim_end"] = 4.0
    plan["transition"] = {"type": "crossfade", "duration": 0.5}

    saved = service.save_plan(plan)

    assert [item["clip_id"] for item in saved["clips"]] == ["1_0", "0_1", "0_0"]
    assert saved["clips"][0]["output_duration"] == 3.0
    assert saved["output_duration"] == 12.0
    assert service.get_plan()["transition"]["type"] == "crossfade"

    duplicate = {**plan, "clips": [plan["clips"][0], plan["clips"][0], plan["clips"][2]]}
    with pytest.raises(ValueError, match="exactly once"):
        service.validate(duplicate)
    plan["clips"][0]["trim_end"] = 9.0
    with pytest.raises(ValueError, match="exceed"):
        service.validate(plan)

    fresh = service.get_plan()
    fresh["clips"][0]["trim_start"] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        service.validate(fresh)
    fresh = service.get_plan()
    fresh["transition"] = {"type": "fade", "duration": "Infinity"}
    with pytest.raises(ValueError, match="finite"):
        service.validate(fresh)


def test_render_archives_current_output_and_reset_restores_source(tmp_path: Path):
    working = _project(tmp_path)
    calls = []

    def renderer(source, ranges, output, *, transition):
        calls.append({"source": source, "ranges": ranges, "transition": transition})
        Path(output).write_bytes(b"edited-final")
        return output

    service = TimelineEditService(
        working,
        duration_provider=lambda _path: 15.0,
        renderer=renderer,
    )
    plan = service.get_plan()
    plan["clips"] = [plan["clips"][2], plan["clips"][0], plan["clips"][1]]
    plan["clips"][1]["trim_start"] = 0.5

    rendered = service.render(plan)

    final = working / "idea2video" / "final_video.mp4"
    source = working / "idea2video" / "_editing" / "source_video.mp4"
    assert final.read_bytes() == b"edited-final"
    assert source.read_bytes() == b"original-final"
    assert Path(rendered["archive_path"]).joinpath("final_video.mp4").read_bytes() == b"original-final"
    assert [item["clip_id"] for item in calls[0]["ranges"]] == ["1_0", "0_0", "0_1"]
    assert service.has_original_source() is True

    reset = service.reset()
    assert reset["ok"] is True
    assert final.read_bytes() == b"original-final"
    assert not (working / "idea2video" / "edit_plan.json").exists()
    assert Path(rendered["archive_path"]).name == "v1_render"
    assert Path(reset["archive_path"]).name == "v2_reset"


def test_external_final_change_marks_saved_plan_stale(tmp_path: Path):
    working = _project(tmp_path)

    def renderer(_source, _ranges, output, *, transition):
        Path(output).write_bytes(b"edited")
        return output

    service = TimelineEditService(
        working, duration_provider=lambda _path: 15.0, renderer=renderer
    )
    service.render(service.get_plan())
    (working / "idea2video" / "final_video.mp4").write_bytes(b"regenerated-final")

    refreshed = service.get_plan()

    assert refreshed["source_status"] == "refresh_required"
    assert refreshed["stale_saved_plan"] is True
    assert service.has_original_source() is False
    with pytest.raises(ValueError, match="original edit source"):
        service.reset()


def test_production_api_exposes_timeline_plan_routes():
    runner = SimpleNamespace(is_running=lambda _sid: False)
    service = SimpleNamespace(
        runner=runner,
        edit_plan=lambda sid: {"session_id": sid, "clips": [{"clip_id": "0_0"}]},
        save_edit_plan=lambda sid, plan: {**plan, "session_id": sid},
        render_edit_plan=lambda sid, plan: {"accepted": True, "session_id": sid, "plan": plan},
        reset_edit_plan=lambda sid: {"accepted": True, "session_id": sid},
    )
    index = SimpleNamespace(
        get=lambda sid: {"session_id": sid},
        working_dir=lambda _sid: Path("."),
    )
    api = ProductionAPI(index, service, SimpleNamespace())

    status, loaded = asyncio.run(api.handle("GET", "/api/production/demo/edit-plan"))
    assert status == 200
    assert loaded["plan"]["clips"][0]["clip_id"] == "0_0"
    status, saved = asyncio.run(api.handle(
        "PUT", "/api/production/demo/edit-plan", {"plan": {"clips": [{"clip_id": "0_0"}]}}
    ))
    assert status == 200
    assert saved["plan"]["session_id"] == "demo"
    status, rendered = asyncio.run(api.handle(
        "POST", "/api/production/demo/edit-plan/render", {"plan": {"clips": []}}
    ))
    assert status == 200
    assert rendered["accepted"] is True
    status, reset = asyncio.run(api.handle(
        "POST", "/api/production/demo/edit-plan/reset", {}
    ))
    assert status == 200
    assert reset["session_id"] == "demo"
