"""Read a session's structured artifacts so the web can show them for review.

Targets the idea-mode layout the WorkflowEngine writes:
  <working_dir>/idea2video/{story.txt, characters.json, script.json}
  <working_dir>/idea2video/scene_<i>/storyboard.json
  <working_dir>/idea2video/scene_<i>/shots/<idx>/{first_frame.png,last_frame.png,video.mp4}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _scene_index(scene_dir: Path) -> int:
    try:
        return int(scene_dir.name.split("_")[1])
    except (IndexError, ValueError):
        return 0


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_script(working_dir: Path) -> dict:
    idea = Path(working_dir) / "idea2video"
    story = ""
    story_path = idea / "story.txt"
    if story_path.exists():
        story = story_path.read_text(encoding="utf-8")
    scenes = _load_json(idea / "script.json", [])
    if not isinstance(scenes, list):
        scenes = [scenes]
    scenes = [s if isinstance(s, str) else json.dumps(s, ensure_ascii=False) for s in scenes]
    characters = _load_json(idea / "characters.json", [])
    return {"story": story, "scenes": scenes, "characters": characters}


def read_storyboard(working_dir: Path) -> dict:
    idea = Path(working_dir) / "idea2video"
    scenes: List[dict] = []
    if idea.exists():
        for scene_dir in sorted(idea.glob("scene_*"), key=_scene_index):
            sb = scene_dir / "storyboard.json"
            if sb.exists():
                shots = _load_json(sb, [])
                scenes.append({"scene_index": _scene_index(scene_dir), "shots": shots})
    return {"scenes": scenes}


def build_manifest(working_dir: Path) -> dict:
    """Relative media paths the UI can request via /file?path=... ."""
    root = Path(working_dir)
    idea = root / "idea2video"

    def rel(p: Path) -> str:
        return p.resolve().relative_to(root.resolve()).as_posix()

    scenes: List[dict] = []
    if idea.exists():
        for scene_dir in sorted(idea.glob("scene_*"), key=_scene_index):
            # optional per-shot quality verdicts (written by the consistency critic)
            quality = {}
            qpath = scene_dir / "quality.json"
            if qpath.exists():
                try:
                    quality = json.loads(qpath.read_text(encoding="utf-8")) or {}
                except (json.JSONDecodeError, OSError):
                    quality = {}
            preflight = _load_json(scene_dir / "prompt_preflight.json", {})
            if not isinstance(preflight, dict):
                preflight = {}
            preflight_shots = preflight.get("shots") or {}
            if not isinstance(preflight_shots, dict):
                preflight_shots = {}
            continuity = _load_json(scene_dir / "continuity_ledger.json", {})
            if not isinstance(continuity, dict):
                continuity = {}
            continuity_shots = continuity.get("shots") or {}
            if not isinstance(continuity_shots, dict):
                continuity_shots = {}
            shots: List[dict] = []
            shots_dir = scene_dir / "shots"
            if shots_dir.exists():
                for shot_dir in sorted([d for d in shots_dir.iterdir() if d.is_dir()],
                                       key=lambda d: int(d.name) if d.name.isdigit() else 0):
                    media = {}
                    for name in ("first_frame.png", "last_frame.png", "video.mp4"):
                        f = shot_dir / name
                        if f.exists():
                            media[name] = rel(f)
                    idx = int(shot_dir.name) if shot_dir.name.isdigit() else shot_dir.name
                    entry = {"idx": idx, "media": media}
                    render_plan = _load_json(shot_dir / "render_plan.json", None)
                    if isinstance(render_plan, dict):
                        entry["render_plan"] = render_plan
                    shot_preflight = _load_json(shot_dir / "prompt_preflight.json", None)
                    if not isinstance(shot_preflight, dict):
                        shot_preflight = preflight_shots.get(str(idx))
                    if isinstance(shot_preflight, dict):
                        entry["prompt_preflight"] = shot_preflight
                    shot_continuity = continuity_shots.get(str(idx))
                    if isinstance(shot_continuity, dict):
                        entry["continuity"] = shot_continuity
                    q = quality.get(str(idx))
                    if isinstance(q, dict):
                        entry["quality"] = q
                    shots.append(entry)
            scene_final = scene_dir / "final_video.mp4"
            scenes.append({
                "scene_index": _scene_index(scene_dir),
                "shots": shots,
                "final_video": rel(scene_final) if scene_final.exists() else None,
                "prompt_preflight_summary": preflight.get("summary") or {},
                "continuity_summary": continuity.get("summary") or {},
                "asset_bibles": continuity.get("asset_bibles") or {},
            })
    final_video = idea / "final_video.mp4"
    return {"scenes": scenes, "final_video": rel(final_video) if final_video.exists() else None}


def resolve_file(working_dir: Path, rel_path: str) -> Optional[Path]:
    """Resolve a relative media path inside the session dir (traversal-guarded)."""
    if not rel_path:
        return None
    root = Path(working_dir).resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        return None
    return target if target.is_file() else None
