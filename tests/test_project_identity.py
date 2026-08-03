from __future__ import annotations

from pathlib import Path

from project_identity import apply_legacy_environment, state_directory


def test_legacy_environment_is_mapped_without_overriding_current_value():
    environ = {
        "VIMAX_LLM_API_KEY": "legacy-key",
        "SCENEFORGE_VIDEO_MODEL": "current-model",
        "VIMAX_VIDEO_MODEL": "legacy-model",
    }

    migrated = apply_legacy_environment(environ)

    assert migrated == 1
    assert environ["SCENEFORGE_LLM_API_KEY"] == "legacy-key"
    assert environ["SCENEFORGE_VIDEO_MODEL"] == "current-model"


def test_state_directory_migrates_legacy_name(tmp_path: Path):
    legacy = tmp_path / ".vimax"
    legacy.mkdir()
    (legacy / "sessions.json").write_text('{"sessions": {}}', encoding="utf-8")

    current = state_directory(tmp_path)

    assert current == tmp_path / ".sceneforge"
    assert current.joinpath("sessions.json").is_file()
    assert not legacy.exists()
