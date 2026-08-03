from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from project_identity import state_directory
from utils.atomic import atomic_write_text, file_lock


SCHEMA_VERSION = 1
MIN_ROUTING_SAMPLES = 5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_generation(scene_dir: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one immutable provider-generation fact to a scene-local ledger."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generation_id": str(record.get("generation_id") or uuid4().hex),
        "recorded_at": utc_now(),
        **record,
    }
    path = Path(scene_dir) / "production_generations.jsonl"
    _append_jsonl(path, payload)
    return payload


def append_decision(
    working_dir: str | Path,
    event_type: str,
    *,
    scene_index: int | None = None,
    shot_index: int | None = None,
    generation_id: str | None = None,
    reason: str = "",
    dimensions: Iterable[str] = (),
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid4().hex,
        "timestamp": utc_now(),
        "event_type": str(event_type),
        "scene_index": None if scene_index is None else int(scene_index),
        "shot_index": None if shot_index is None else int(shot_index),
        "generation_id": str(generation_id or "") or None,
        "reason": str(reason or ""),
        "dimensions": sorted({str(item) for item in dimensions if str(item).strip()}),
        "metadata": dict(metadata or {}),
    }
    path = Path(working_dir) / ".sceneforge" / "production_events.jsonl"
    _append_jsonl(path, payload)
    return payload


def list_artifact_annotations(
    working_dir: str | Path, artifact_id: str
) -> list[dict[str, Any]]:
    """Return immutable review annotations attached to one artifact version."""
    target = str(artifact_id or "").strip()
    if not target:
        return []
    events = _read_jsonl(
        Path(working_dir) / ".sceneforge" / "production_events.jsonl"
    )
    annotations = []
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        if event.get("event_type") != "artifact_annotation":
            continue
        if str(metadata.get("artifact_id") or "") != target:
            continue
        annotations.append({
            "annotation_id": str(event.get("event_id") or ""),
            "artifact_id": target,
            "scene_index": event.get("scene_index"),
            "shot_index": event.get("shot_index"),
            "text": str(metadata.get("text") or ""),
            "timecode_seconds": metadata.get("timecode_seconds"),
            "author": str(metadata.get("author") or ""),
            "created_at": str(event.get("timestamp") or ""),
        })
    return annotations


def current_generation_id(
    working_dir: str | Path, scene_index: int, shot_index: int
) -> str | None:
    scene_dir = _scene_directory(Path(working_dir), int(scene_index))
    records = [
        item for item in _read_jsonl(scene_dir / "production_generations.jsonl")
        if _integer(item.get("shot_index"), -1) == int(shot_index)
        and item.get("status") == "completed"
    ]
    if records:
        return str(records[-1].get("generation_id") or "") or None
    plan = _read_json(scene_dir / "shots" / str(shot_index) / "render_plan.json", {})
    production = plan.get("production") if isinstance(plan, dict) else None
    if isinstance(production, dict):
        return str(production.get("generation_id") or "") or None
    fallback = _render_plan_generation(scene_dir, int(scene_index), int(shot_index))
    if fallback:
        return str(fallback.get("generation_id") or "") or None
    return None


def record_stage_acceptance(working_dir: str | Path) -> int:
    """Record an explicit shot-video gate approval for each current clip."""
    root = Path(working_dir)
    existing = _read_jsonl(root / ".sceneforge" / "production_events.jsonl")
    accepted_ids = {
        str(item.get("generation_id"))
        for item in existing
        if item.get("event_type") == "accepted" and item.get("generation_id")
    }
    count = 0
    for scene_index, scene_dir in _scene_directories(root):
        for shot_index in _shot_indexes(scene_dir):
            video = scene_dir / "shots" / str(shot_index) / "video.mp4"
            if not video.is_file():
                continue
            generation_id = current_generation_id(root, scene_index, shot_index)
            if generation_id and generation_id in accepted_ids:
                continue
            append_decision(
                root,
                "accepted",
                scene_index=scene_index,
                shot_index=shot_index,
                generation_id=generation_id,
                reason="shot_video_gate_approved",
                metadata={"artifact": str(video.relative_to(root).as_posix())},
            )
            count += 1
    return count


def aggregate_production_metrics(
    working_dir: str | Path, *, session_id: str | None = None
) -> dict[str, Any]:
    root = Path(working_dir)
    decisions = _read_jsonl(root / ".sceneforge" / "production_events.jsonl")
    decisions_by_shot: dict[tuple[int, int], list[dict[str, Any]]] = {}
    outcomes: dict[str, str] = {}
    for event in decisions:
        key = (_integer(event.get("scene_index"), -1), _integer(event.get("shot_index"), -1))
        decisions_by_shot.setdefault(key, []).append(event)
        generation_id = str(event.get("generation_id") or "")
        if generation_id and event.get("event_type") in {"accepted", "regenerated"}:
            outcomes[generation_id] = "accepted" if event["event_type"] == "accepted" else "rejected"

    shots: list[dict[str, Any]] = []
    all_generations: list[dict[str, Any]] = []
    for scene_index, scene_dir in _scene_directories(root):
        quality = _read_json(scene_dir / "quality.json", {})
        generations = _read_jsonl(scene_dir / "production_generations.jsonl")
        by_shot: dict[int, list[dict[str, Any]]] = {}
        for record in generations:
            shot_index = _integer(record.get("shot_index"), -1)
            if shot_index < 0:
                continue
            record = {**record, "scene_index": scene_index}
            by_shot.setdefault(shot_index, []).append(record)
            all_generations.append(record)

        for shot_index in _shot_indexes(scene_dir, extras=by_shot):
            records = by_shot.get(shot_index, [])
            if not records:
                fallback = _render_plan_generation(scene_dir, scene_index, shot_index)
                if fallback:
                    records = [fallback]
                    all_generations.append(fallback)
            shot_decisions = decisions_by_shot.get((scene_index, shot_index), [])
            rejected = [item for item in shot_decisions if item.get("event_type") == "regenerated"]
            accepted = [item for item in shot_decisions if item.get("event_type") == "accepted"]
            selected = [item for item in shot_decisions if item.get("event_type") == "artifact_selected"]
            current = records[-1] if records else {}
            current_generation_id_value = str(current.get("generation_id") or "")
            current_accepted = any(
                str(item.get("generation_id") or "") == current_generation_id_value
                for item in accepted
            ) if current_generation_id_value else bool(accepted)
            verdict = quality.get(str(shot_index)) if isinstance(quality, dict) else None
            quality_ok = verdict.get("ok") if isinstance(verdict, dict) else None
            shot_cost = _sum_costs(records)
            shots.append({
                "scene_index": scene_index,
                "shot_index": shot_index,
                "status": "accepted" if current_accepted else "generated" if records else "pending",
                "accepted": current_accepted,
                "first_pass": bool(current_accepted and not rejected),
                "quality_ok": quality_ok if isinstance(quality_ok, bool) else None,
                "quality_score": _number(verdict.get("score")) if isinstance(verdict, dict) else None,
                "failed_dimensions": list((verdict or {}).get("failed") or []) if isinstance(verdict, dict) else [],
                "generation_count": len(records),
                "request_attempts": sum(_integer(item.get("request_attempts"), 0) for item in records),
                "retry_count": sum(_integer(item.get("retry_count"), 0) for item in records),
                "rework_count": len(rejected),
                "rework_reasons": [str(item.get("reason") or "") for item in rejected],
                "current_generation": current or None,
                "selected_artifact": selected[-1].get("metadata") if selected else None,
                "cost": shot_cost,
            })

    summary = _summary(shots, all_generations, decisions)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "generated_at": utc_now(),
        "summary": summary,
        "shots": shots,
        "models": _model_summary(all_generations, outcomes),
        "notes": [
            "实际费用仅统计供应商明确返回的账单字段；其余费用保持为估算。",
            f"模型历史表现至少累计 {MIN_ROUTING_SAMPLES} 个已决策样本后才参与自动路由。",
        ],
    }


def rebuild_provider_performance(workspace_root: str | Path, session_index: Any) -> dict[str, Any]:
    combined: dict[str, dict[str, Any]] = {}
    for session in session_index.list_sessions():
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        try:
            metrics = aggregate_production_metrics(
                session_index.working_dir(session_id), session_id=session_id
            )
        except (OSError, ValueError):
            continue
        for item in metrics.get("models") or []:
            key = str(item.get("key") or "")
            if not key:
                continue
            target = combined.setdefault(key, {
                "key": key,
                "profile_id": item.get("profile_id"),
                "provider_id": item.get("provider_id"),
                "model_id": item.get("model_id"),
                "sample_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "generation_seconds_total": 0.0,
                "estimated_cost_total": 0.0,
            })
            for field in ("sample_count", "accepted_count", "rejected_count"):
                target[field] += _integer(item.get(field), 0)
            target["generation_seconds_total"] += _number(item.get("generation_seconds_total")) or 0.0
            target["estimated_cost_total"] += _number(item.get("estimated_cost_total")) or 0.0

    profiles = []
    for item in combined.values():
        samples = item["sample_count"]
        accepted = item["accepted_count"]
        profiles.append({
            **item,
            "acceptance_rate": round(accepted / samples, 4) if samples else None,
            "mean_generation_seconds": round(item["generation_seconds_total"] / samples, 3) if samples else None,
            "estimated_cost_per_accepted_shot": round(item["estimated_cost_total"] / accepted, 4) if accepted else None,
            "routing_eligible": samples >= MIN_ROUTING_SAMPLES,
        })
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "minimum_routing_samples": MIN_ROUTING_SAMPLES,
        "profiles": sorted(profiles, key=lambda item: item["key"]),
    }
    path = state_directory(workspace_root) / "provider_performance.json"
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def load_provider_performance(workspace_root: str | Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(state_directory(workspace_root) / "provider_performance.json", {})
    profiles = payload.get("profiles") if isinstance(payload, dict) else []
    return {
        str(item.get("key")): item
        for item in (profiles or [])
        if isinstance(item, dict) and item.get("key")
    }


def _summary(
    shots: list[dict[str, Any]],
    generations: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    generated = [item for item in shots if item["generation_count"]]
    accepted = [item for item in shots if item["accepted"]]
    decided = [item for item in shots if item["accepted"] or item["rework_count"]]
    evaluated = [item for item in shots if item["quality_ok"] is not None]
    passed = [item for item in evaluated if item["quality_ok"]]
    first_pass = [item for item in decided if item["first_pass"]]
    completed_generations = [item for item in generations if item.get("status") == "completed"]
    costs = _sum_costs(generations)
    queue_values = [_number(item.get("queue_seconds")) for item in completed_generations]
    generation_values = [_number(item.get("generation_seconds")) for item in completed_generations]
    actual_covered = sum(1 for item in completed_generations if _billing(item).get("actual_cost") is not None)
    accepted_times = [
        _parse_time(item.get("timestamp"))
        for item in decisions if item.get("event_type") == "accepted"
    ]
    started_times = [_parse_time(item.get("started_at")) for item in generations]
    accepted_times = [item for item in accepted_times if item is not None]
    started_times = [item for item in started_times if item is not None]
    time_to_final = None
    if accepted_times and started_times:
        time_to_final = max(0.0, (max(accepted_times) - min(started_times)).total_seconds())
    local_reworks = [
        item for item in decisions if item.get("event_type") == "local_rework_completed"
    ]
    rework_savings = _local_rework_savings(local_reworks)
    return {
        "total_shots": len(shots),
        "generated_shots": len(generated),
        "accepted_shots": len(accepted),
        "decided_shots": len(decided),
        "pending_decision_shots": max(0, len(generated) - len(accepted)),
        "first_pass_shots": len(first_pass),
        "first_pass_rate": round(len(first_pass) / len(decided), 4) if decided else None,
        "quality_evaluated_shots": len(evaluated),
        "quality_passed_shots": len(passed),
        "quality_pass_rate": round(len(passed) / len(evaluated), 4) if evaluated else None,
        "total_reworks": sum(item["rework_count"] for item in shots),
        "mean_reworks_per_generated_shot": round(
            sum(item["rework_count"] for item in shots) / len(generated), 3
        ) if generated else 0.0,
        "request_attempts": sum(_integer(item.get("request_attempts"), 0) for item in generations),
        "retry_count": sum(_integer(item.get("retry_count"), 0) for item in generations),
        "mean_queue_seconds": _mean(queue_values),
        "mean_generation_seconds": _mean(generation_values),
        "time_to_final_seconds": round(time_to_final, 3) if time_to_final is not None else None,
        "local_rework_savings": rework_savings,
        "cost": {
            **costs,
            "actual_coverage_count": actual_covered,
            "generation_record_count": len(completed_generations),
            "actual_coverage_rate": round(actual_covered / len(completed_generations), 4) if completed_generations else 0.0,
            "estimated_cost_per_accepted_shot": round(costs["estimated_upper_bound"] / len(accepted), 4) if accepted else None,
            "actual_cost_per_accepted_shot": round(costs["actual_total"] / len(accepted), 4) if accepted and actual_covered == len(completed_generations) else None,
        },
    }


def _local_rework_savings(events: list[dict[str, Any]]) -> dict[str, Any]:
    avoided_shots = 0
    saved_seconds = 0.0
    saved_seconds_count = 0
    saved_lower = 0.0
    saved_upper = 0.0
    saved_cost_count = 0
    currency = None
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        savings = metadata.get("savings_estimate") if isinstance(metadata.get("savings_estimate"), dict) else {}
        avoided_shots += max(0, _integer(savings.get("avoided_shot_count"), 0))
        seconds = _number(savings.get("estimated_generation_seconds_saved"))
        if seconds is not None:
            saved_seconds += max(0.0, seconds)
            saved_seconds_count += 1
        lower = _number(savings.get("estimated_cost_saved_lower_bound"))
        upper = _number(savings.get("estimated_cost_saved_upper_bound"))
        if lower is not None and upper is not None:
            saved_lower += max(0.0, lower)
            saved_upper += max(0.0, upper)
            saved_cost_count += 1
            estimate = savings.get("full_rerender_cost_estimate")
            if isinstance(estimate, dict) and estimate.get("currency"):
                currency = estimate["currency"]
    return {
        "completed_batches": len(events),
        "avoided_shot_count": avoided_shots,
        "estimated_generation_seconds_saved": (
            round(saved_seconds, 3) if saved_seconds_count else None
        ),
        "estimated_cost_saved_lower_bound": (
            round(saved_lower, 4) if saved_cost_count else None
        ),
        "estimated_cost_saved_upper_bound": (
            round(saved_upper, 4) if saved_cost_count else None
        ),
        "currency": currency,
        "cost_estimate_coverage_count": saved_cost_count,
    }


def _model_summary(
    generations: list[dict[str, Any]], outcomes: dict[str, str]
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in generations:
        route = record.get("route") if isinstance(record.get("route"), dict) else {}
        profile_id = str(route.get("profile_id") or "") or None
        provider_id = str(route.get("provider_id") or "unknown")
        model_id = str(route.get("model_id") or "unknown")
        key = f"profile:{profile_id}" if profile_id else f"model:{provider_id}:{model_id}"
        target = groups.setdefault(key, {
            "key": key,
            "profile_id": profile_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "generation_count": 0,
            "request_attempts": 0,
            "retry_count": 0,
            "sample_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "generation_seconds_total": 0.0,
            "estimated_cost_total": 0.0,
            "actual_cost_total": 0.0,
            "actual_cost_count": 0,
        })
        target["generation_count"] += 1
        target["request_attempts"] += _integer(record.get("request_attempts"), 0)
        target["retry_count"] += _integer(record.get("retry_count"), 0)
        target["generation_seconds_total"] += _number(record.get("generation_seconds")) or 0.0
        billing = _billing(record)
        target["estimated_cost_total"] += _number(billing.get("estimated_upper_bound")) or 0.0
        if billing.get("actual_cost") is not None:
            target["actual_cost_total"] += _number(billing.get("actual_cost")) or 0.0
            target["actual_cost_count"] += 1
        outcome = outcomes.get(str(record.get("generation_id") or ""))
        if outcome:
            target["sample_count"] += 1
            target[f"{outcome}_count"] += 1

    result = []
    for item in groups.values():
        samples = item["sample_count"]
        accepted = item["accepted_count"]
        result.append({
            **item,
            "acceptance_rate": round(accepted / samples, 4) if samples else None,
            "mean_generation_seconds": round(item["generation_seconds_total"] / item["generation_count"], 3) if item["generation_count"] else None,
            "estimated_cost_per_accepted_shot": round(item["estimated_cost_total"] / accepted, 4) if accepted else None,
            "routing_eligible": samples >= MIN_ROUTING_SAMPLES,
        })
    return sorted(result, key=lambda item: item["key"])


def _sum_costs(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    lower = upper = actual = 0.0
    actual_count = 0
    estimated_count = 0
    currencies: set[str] = set()
    for record in records:
        billing = _billing(record)
        lower += _number(billing.get("estimated_lower_bound")) or 0.0
        upper += _number(billing.get("estimated_upper_bound")) or 0.0
        if billing.get("actual_cost") is not None:
            actual += _number(billing.get("actual_cost")) or 0.0
            actual_count += 1
        if billing.get("status") == "estimate_only" or billing.get("estimated_unit_cost") is not None:
            estimated_count += 1
        if billing.get("currency"):
            currencies.add(str(billing["currency"]))
    return {
        "estimated_lower_bound": round(lower, 4),
        "estimated_upper_bound": round(upper, 4),
        "actual_total": round(actual, 4),
        "actual_record_count": actual_count,
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "basis": (
            "mixed_actual_and_estimate" if actual_count and estimated_count
            else "provider_reported_actual" if actual_count
            else "profile_estimate" if estimated_count
            else "unavailable"
        ),
    }


def _billing(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("billing")
    return value if isinstance(value, dict) else {}


def _render_plan_generation(
    scene_dir: Path, scene_index: int, shot_index: int
) -> dict[str, Any] | None:
    shot_dir = scene_dir / "shots" / str(shot_index)
    plan = _read_json(shot_dir / "render_plan.json", {})
    production = plan.get("production") if isinstance(plan, dict) else None
    if isinstance(production, dict):
        return {**production, "scene_index": scene_index, "shot_index": shot_index}
    video = shot_dir / "video.mp4"
    if not video.is_file():
        return None
    modified = datetime.fromtimestamp(
        video.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="milliseconds")
    provider = plan.get("provider") if isinstance(plan, dict) else None
    return {
        "schema_version": 0,
        "generation_id": f"legacy:{scene_index}:{shot_index}:{video.stat().st_mtime_ns}",
        "scene_index": scene_index,
        "shot_index": shot_index,
        "status": "completed",
        "started_at": None,
        "completed_at": modified,
        "route": {"provider_id": provider} if provider else {},
        "request_attempts": 0,
        "retry_count": 0,
        "queue_seconds": None,
        "generation_seconds": None,
        "download_seconds": None,
        "legacy_imported": True,
        "billing": {
            "status": "unavailable",
            "estimated_lower_bound": 0.0,
            "estimated_upper_bound": 0.0,
            "actual_cost": None,
            "currency": None,
        },
    }


def _scene_directories(root: Path) -> list[tuple[int, Path]]:
    idea = root / "idea2video"
    result = []
    if idea.is_dir():
        for path in idea.glob("scene_*"):
            try:
                result.append((int(path.name.rsplit("_", 1)[1]), path))
            except (IndexError, ValueError):
                continue
    script = root / "script2video"
    if script.is_dir() and ((script / "storyboard.json").is_file() or (script / "shots").is_dir()):
        result.append((0, script))
    return sorted(result, key=lambda item: item[0])


def _scene_directory(root: Path, scene_index: int) -> Path:
    script = root / "script2video"
    if scene_index == 0 and ((script / "storyboard.json").is_file() or (script / "shots").is_dir()):
        return script
    return root / "idea2video" / f"scene_{scene_index}"


def _shot_indexes(scene_dir: Path, extras: dict[int, Any] | None = None) -> list[int]:
    indexes = set((extras or {}).keys())
    storyboard = _read_json(scene_dir / "storyboard.json", [])
    if isinstance(storyboard, list):
        for position, shot in enumerate(storyboard):
            indexes.add(_integer(shot.get("idx"), position) if isinstance(shot, dict) else position)
    shots_dir = scene_dir / "shots"
    if shots_dir.is_dir():
        indexes.update(int(path.name) for path in shots_dir.iterdir() if path.is_dir() and path.name.isdigit())
    return sorted(index for index in indexes if index >= 0)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    result.append(item)
    except OSError:
        return []
    return result


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mean(values: Iterable[float | None]) -> float | None:
    normalized = [float(item) for item in values if item is not None]
    return round(sum(normalized) / len(normalized), 3) if normalized else None


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
