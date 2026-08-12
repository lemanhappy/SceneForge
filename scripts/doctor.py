from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from infrastructure.sqlite import SQLiteDatabase
from project_identity import state_directory
from utils.media import ffmpeg_executable


def _check_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".sceneforge-doctor-", dir=path)
        os.close(handle)
        Path(name).unlink(missing_ok=True)
        return True, "writable"
    except OSError as exc:
        return False, str(exc)


def run_checks(workspace: str | Path = ".", *, check_web: bool = True) -> list[dict]:
    root = Path(workspace).resolve()
    state_root = state_directory(root)
    checks = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({
            "name": name,
            "status": "ok" if ok else ("error" if required else "warning"),
            "detail": detail,
        })

    version_ok = sys.version_info[:2] >= (3, 12)
    add("Python", version_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    ffmpeg = ffmpeg_executable()
    add("FFmpeg", bool(ffmpeg), "available" if ffmpeg else "not found")
    if check_web:
        add(
            "Web UI",
            (REPO_ROOT / "webui-dist" / "index.html").is_file(),
            "built" if (REPO_ROOT / "webui-dist" / "index.html").is_file() else "run npm ci && npm run build in frontend",
        )
    for label, path in (("State directory", state_root), ("Media directory", root / ".working_dir")):
        ok, detail = _check_writable(path)
        add(label, ok, detail)

    database_path = state_root / "sceneforge.db"
    if database_path.is_file() and database_path.stat().st_size:
        try:
            ok, messages = SQLiteDatabase(database_path).integrity_check(quick=True)
            add("Database", ok, "ok" if ok else "; ".join(messages))
        except Exception as exc:
            add("Database", False, str(exc))
    else:
        add("Database", True, "will be created on first start")

    local_config = root / "configs" / "agent.local.yaml"
    env_configured = any(
        os.environ.get(name)
        for name in ("SCENEFORGE_LLM_API_KEY", "SCENEFORGE_IMAGE_API_KEY", "SCENEFORGE_VIDEO_API_KEY")
    )
    add(
        "Model configuration",
        local_config.is_file() or env_configured,
        "configured" if local_config.is_file() or env_configured else "configure models in Settings before generation",
        required=False,
    )
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether SceneForge can start safely")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-web", action="store_true", help="Skip the frontend build check")
    args = parser.parse_args()
    checks = run_checks(args.workspace, check_web=not args.skip_web)
    if args.json:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
    else:
        symbols = {"ok": "[OK]", "warning": "[WARN]", "error": "[ERROR]"}
        for check in checks:
            print(f"{symbols[check['status']]} {check['name']}: {check['detail']}")
    raise SystemExit(1 if any(check["status"] == "error" for check in checks) else 0)


if __name__ == "__main__":
    main()
