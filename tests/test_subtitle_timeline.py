import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.production_api import ProductionAPI
from services.subtitle_timeline import SubtitleTimelineService
from subtitles import parse_srt_text


def _project(tmp_path: Path):
    working = tmp_path / "project"
    idea = working / "idea2video"
    durations = {}
    for scene_index in (0, 1):
        scene = idea / f"scene_{scene_index}"
        scene.mkdir(parents=True, exist_ok=True)
        video = scene / "final_video_with_subtitles.mp4"
        video.write_bytes(f"scene-{scene_index}".encode())
        durations[str(video)] = 5.0
    (idea / "scene_0" / "audio").mkdir()
    (idea / "scene_0" / "audio" / "voiced_track.json").write_text(
        json.dumps({"lines": [{
            "text": "第一句", "speaker": "甲", "shot_idx": 0,
            "start": 1.0, "end": 2.0,
        }]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (idea / "scene_1" / "subtitles").mkdir()
    (idea / "scene_1" / "subtitles" / "final.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\n第二句\n",
        encoding="utf-8",
    )
    final = idea / "final_video.mp4"
    final.write_bytes(b"final")
    durations[str(final)] = 9.0
    return working, durations


def test_parse_srt_text_supports_multiline_and_dot_timestamps():
    track = parse_srt_text(
        "1\n00:00:01.250 --> 00:00:02,500\n第一行\n第二行\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n结束\n"
    )

    assert len(track.lines) == 2
    assert track.lines[0].text == "第一行\n第二行"
    assert track.lines[0].start == 1.25
    assert track.lines[1].end == 4.0


def test_aggregates_scene_tracks_with_transition_overlap(tmp_path: Path):
    working, durations = _project(tmp_path)
    service = SubtitleTimelineService(
        working, duration_provider=lambda path: durations[path]
    )

    plan = service.get_plan()

    assert plan["duration"] == 9.0
    assert plan["line_count"] == 2
    assert plan["lines"][0]["speaker"] == "甲"
    assert (plan["lines"][0]["start"], plan["lines"][0]["end"]) == (1.0, 2.0)
    assert (plan["lines"][1]["start"], plan["lines"][1]["end"]) == (4.5, 5.5)
    assert not (working / "idea2video" / "subtitles" / "final.srt").exists()

    downloaded = service.download_path()

    assert downloaded == working / "idea2video" / "subtitles" / "final.srt"
    assert (working / "idea2video" / "subtitles" / "final.srt").is_file()


def test_save_archives_sidecar_and_reset_restores_generated_track(tmp_path: Path):
    working, durations = _project(tmp_path)
    service = SubtitleTimelineService(
        working, duration_provider=lambda path: durations[path]
    )
    plan = service.get_plan()
    service.download_path()
    plan["lines"][0].update({"text": "修改后", "start": 0.8, "end": 2.2})

    saved = service.save_plan(plan)

    assert saved["lines"][0]["text"] == "修改后"
    srt = (working / "idea2video" / "subtitles" / "final.srt").read_text(
        encoding="utf-8"
    )
    assert "修改后" in srt
    archive = working / "idea2video" / "_archive" / "subtitle_timelines"
    assert (archive / "v1_save" / "final.srt").is_file()

    reset = service.reset()

    assert reset["lines"][0]["text"] == "第一句"
    assert not (working / "idea2video" / "subtitles" / "timeline.json").exists()
    assert (archive / "v2_reset" / "timeline.json").is_file()


def test_validation_rejects_invalid_lines_and_external_change_marks_stale(tmp_path: Path):
    working, durations = _project(tmp_path)
    service = SubtitleTimelineService(
        working, duration_provider=lambda path: durations[path]
    )
    plan = service.get_plan()
    invalid = json.loads(json.dumps(plan))
    invalid["lines"][0]["start"] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        service.validate(invalid)
    invalid = json.loads(json.dumps(plan))
    invalid["lines"][0]["text"] = ""
    with pytest.raises(ValueError, match="empty"):
        service.validate(invalid)

    service.save_plan(plan)
    (working / "idea2video" / "final_video.mp4").write_bytes(b"new-final")
    refreshed = service.get_plan()
    assert refreshed["stale_saved_timeline"] is True


def test_applied_edit_plan_reorders_subtitles(tmp_path: Path):
    working = tmp_path / "project"
    idea = working / "idea2video"
    scene = idea / "scene_0"
    (scene / "audio").mkdir(parents=True)
    scene_video = scene / "final_video_with_subtitles.mp4"
    scene_video.write_bytes(b"scene")
    (scene / "audio" / "voiced_track.json").write_text(json.dumps({"lines": [
        {"text": "前段", "start": 0, "end": 1, "shot_idx": 0},
        {"text": "后段", "start": 5, "end": 6, "shot_idx": 1},
    ]}, ensure_ascii=False), encoding="utf-8")
    final = idea / "final_video.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"edited-final")
    editing = idea / "_editing"
    editing.mkdir()
    fingerprint = f"{final.stat().st_size}:{final.stat().st_mtime_ns}"
    (editing / "source.json").write_text(json.dumps({
        "last_output_fingerprint": fingerprint,
    }), encoding="utf-8")
    (editing / "applied_edit_plan.json").write_text(json.dumps({
        "source_duration": 10,
        "transition": {"type": "crossfade", "duration": 0.5},
        "clips": [
            {"clip_id": "0_1", "source_start": 5, "trim_start": 0, "trim_end": 5},
            {"clip_id": "0_0", "source_start": 0, "trim_start": 0, "trim_end": 5},
        ],
    }), encoding="utf-8")
    durations = {str(scene_video): 10.0, str(final): 9.5}

    plan = SubtitleTimelineService(
        working, duration_provider=lambda path: durations[path]
    ).get_plan()

    assert [line["text"] for line in plan["lines"]] == ["后段", "前段"]
    assert [(line["start"], line["end"]) for line in plan["lines"]] == [
        (0.0, 1.0), (4.5, 5.5),
    ]
    assert len({line["line_id"] for line in plan["lines"]}) == 2


def test_production_api_exposes_subtitle_routes(tmp_path: Path):
    subtitle_file = tmp_path / "final.srt"
    subtitle_file.write_text("subtitle", encoding="utf-8")
    runner = SimpleNamespace(is_running=lambda _sid: False)
    service = SimpleNamespace(
        runner=runner,
        subtitle_timeline=lambda sid: {"session_id": sid, "lines": []},
        save_subtitle_timeline=lambda sid, timeline: {**timeline, "session_id": sid},
        reset_subtitle_timeline=lambda sid: {"session_id": sid, "reset": True},
        subtitle_file=lambda _sid: subtitle_file,
    )
    index = SimpleNamespace(
        get=lambda sid: {"session_id": sid},
        working_dir=lambda _sid: Path("."),
    )
    api = ProductionAPI(index, service, SimpleNamespace())

    status, loaded = asyncio.run(api.handle("GET", "/api/production/demo/subtitles"))
    assert status == 200
    assert loaded["timeline"]["session_id"] == "demo"
    status, saved = asyncio.run(api.handle(
        "PUT", "/api/production/demo/subtitles", {"timeline": {"lines": []}}
    ))
    assert status == 200
    assert saved["timeline"]["session_id"] == "demo"
    status, reset = asyncio.run(api.handle(
        "POST", "/api/production/demo/subtitles/reset", {}
    ))
    assert reset["timeline"]["reset"] is True
    status, download = asyncio.run(api.handle(
        "GET", "/api/production/demo/subtitles/file"
    ))
    assert status == 200
    assert download["_file"] == str(subtitle_file)
