from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "sceneforge_benchmark"


def validate_dataset(root: Path) -> dict:
    errors = []
    index_path = root / "benchmark_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "errors": [f"benchmark_index.json: {exc}"], "summary": {}}
    stories = index.get("stories")
    if not isinstance(stories, list):
        return {"ok": False, "errors": ["benchmark_index.json: stories must be a list"], "summary": {}}
    if index.get("total_stories") != len(stories):
        errors.append("benchmark_index.json: total_stories does not match stories")

    ids = [item.get("id") for item in stories if isinstance(item, dict)]
    files = [str(item.get("file") or "") for item in stories if isinstance(item, dict)]
    if ids != list(range(1, len(stories) + 1)):
        errors.append("benchmark_index.json: ids must be contiguous starting at 1")
    if len(files) != len(set(files)):
        errors.append("benchmark_index.json: duplicate story files")

    type_counts: Counter[str] = Counter()
    total_scenes = 0
    total_shots = 0
    for item in stories:
        if not isinstance(item, dict):
            errors.append("benchmark_index.json: each story must be an object")
            continue
        filename = str(item.get("file") or "")
        path = root / filename
        if path.parent != root or not path.is_file():
            errors.append(f"{filename or '<missing>'}: indexed file not found")
            continue
        try:
            story = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{filename}: invalid JSON: {exc}")
            continue
        consistency_type = str(story.get("consistency_type") or "")
        expected_type = str(item.get("type") or "")
        if consistency_type != expected_type:
            errors.append(f"{filename}: consistency_type does not match index")
        metadata = story.get("metadata") or {}
        if metadata.get("theme_key") != item.get("theme"):
            errors.append(f"{filename}: metadata theme_key does not match index")
        if metadata.get("consistency_type") != consistency_type:
            errors.append(f"{filename}: metadata consistency_type mismatch")
        if len(str(story.get("story_overview") or "").strip()) < 40:
            errors.append(f"{filename}: story_overview is missing or too short")

        scenes = story.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            errors.append(f"{filename}: scenes must be a non-empty list")
            continue
        scene_numbers = [scene.get("scene_num") for scene in scenes if isinstance(scene, dict)]
        if scene_numbers != list(range(1, len(scenes) + 1)):
            errors.append(f"{filename}: scene_num values must be contiguous")
        shots = [shot for scene in scenes for shot in (scene.get("shots") or [])]
        shot_ids = [shot.get("shot_id") for shot in shots if isinstance(shot, dict)]
        if shot_ids != list(range(1, len(shots) + 1)):
            errors.append(f"{filename}: shot_id values must be contiguous")
        if metadata.get("requested_scenes") != len(scenes):
            errors.append(f"{filename}: requested_scenes does not match content")
        if metadata.get("requested_shots") != len(shots):
            errors.append(f"{filename}: requested_shots does not match content")
        for shot in shots:
            shot_id = shot.get("shot_id", "?") if isinstance(shot, dict) else "?"
            first_frame = str(shot.get("first_frame") or "") if isinstance(shot, dict) else ""
            video_prompt = str(shot.get("video_prompt") or "") if isinstance(shot, dict) else ""
            if len(first_frame.strip()) < 80:
                errors.append(f"{filename} shot {shot_id}: first_frame is missing or too short")
            if len(video_prompt.strip()) < 60:
                errors.append(f"{filename} shot {shot_id}: video_prompt is missing or too short")
            prompt_lower = video_prompt.lower()
            if not any(term in prompt_lower for term in (
                "shot", "camera", "close-up", "overhead", "medium-wide", "over-the-shoulder",
                "low angle", "high angle", "wide", "mm lens", "locked-off", "push-in", "dolly",
            )):
                errors.append(f"{filename} shot {shot_id}: video_prompt lacks camera direction")
        type_counts[consistency_type] += 1
        total_scenes += len(scenes)
        total_shots += len(shots)

    indexed = set(files)
    unindexed = sorted(path.name for path in root.glob("*.json") if path.name != "benchmark_index.json" and path.name not in indexed)
    if unindexed:
        errors.append("unindexed benchmark files: " + ", ".join(unindexed))
    return {
        "ok": not errors,
        "errors": errors,
        "summary": {
            "stories": len(stories),
            "scenes": total_scenes,
            "shots": total_shots,
            "types": dict(sorted(type_counts.items())),
        },
    }


def summarize_results(path: Path) -> dict:
    records = []
    errors = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: {exc}")
            continue
        if not all(record.get(key) for key in ("case", "provider", "model", "status")):
            errors.append(f"line {line_number}: case, provider, model and status are required")
            continue
        for metric in ("identity", "temporal", "composition", "prompt_adherence"):
            value = record.get(metric)
            if value is not None and not 0 <= float(value) <= 1:
                errors.append(f"line {line_number}: {metric} must be between 0 and 1")
        records.append(record)
    groups = {}
    for record in records:
        key = f"{record['provider']}/{record['model']}"
        groups.setdefault(key, []).append(record)
    summaries = {}
    for key, items in groups.items():
        succeeded = [item for item in items if item.get("status") == "succeeded"]

        def mean(name: str):
            values = [float(item[name]) for item in succeeded if item.get(name) is not None]
            return round(statistics.fmean(values), 4) if values else None

        summaries[key] = {
            "cases": len(items),
            "success_rate": round(len(succeeded) / len(items), 4),
            "identity": mean("identity"),
            "temporal": mean("temporal"),
            "composition": mean("composition"),
            "prompt_adherence": mean("prompt_adherence"),
            "average_seconds": mean("seconds"),
            "average_retries": mean("retries"),
            "total_cost": round(sum(float(item.get("cost") or 0) for item in items), 4),
        }
    return {"ok": not errors, "errors": errors, "models": summaries}


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate SceneForge benchmark cases and summarize model results")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--results", type=Path, help="Optional JSONL model-run results")
    parser.add_argument("--output", type=Path, help="Write the JSON report to this file")
    args = parser.parse_args()
    report = {"dataset": validate_dataset(args.root.resolve())}
    if args.results:
        report["results"] = summarize_results(args.results.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    failed = not report["dataset"]["ok"] or ("results" in report and not report["results"]["ok"])
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
