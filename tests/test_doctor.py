from pathlib import Path
from unittest.mock import patch

from infrastructure.sqlite import SQLiteDatabase
from scripts.doctor import run_checks


def test_doctor_reports_healthy_workspace_without_exposing_paths(tmp_path):
    state = tmp_path / ".sceneforge"
    database = SQLiteDatabase(state / "sceneforge.db")
    with database.transaction() as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")

    with patch("scripts.doctor.REPO_ROOT", Path(__file__).resolve().parent.parent), \
         patch("scripts.doctor.ffmpeg_executable", return_value="ffmpeg"):
        checks = run_checks(tmp_path)

    by_name = {item["name"]: item for item in checks}
    assert by_name["Database"]["status"] == "ok"
    assert by_name["State directory"]["status"] == "ok"
    assert by_name["Model configuration"]["status"] in {"ok", "warning"}
    assert not any(str(tmp_path) in item["detail"] for item in checks)


def test_doctor_marks_missing_required_components_as_errors(tmp_path):
    with patch("scripts.doctor.REPO_ROOT", tmp_path / "source"), \
         patch("scripts.doctor.ffmpeg_executable", return_value=None):
        checks = run_checks(tmp_path / "workspace")

    by_name = {item["name"]: item for item in checks}
    assert by_name["FFmpeg"]["status"] == "error"
    assert by_name["Web UI"]["status"] == "error"
