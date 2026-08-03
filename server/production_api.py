"""HTTP API for the production page: drive the staged, review-gated workflow.

Long stages (script/storyboard/video, regenerate-shot) run as background jobs via
ProductionService, so requests return immediately and the browser polls a job /
the session snapshot instead of blocking for minutes.

Routes (under /api/production):
  GET  /api/production                         -> list sessions
  POST /api/production/topic                   -> start a topic {idea} -> {job_id}
  GET  /api/production/jobs/{job_id}           -> background job status/result
  POST /api/production/jobs/{job_id}/cancel    -> request durable job cancellation
  GET  /api/production/{sid}                   -> session snapshot (stage, reviews, artifacts)
  PUT  /api/production/{sid}/script            -> save an in-place script edit
  GET  /api/production/{sid}/artifact-versions -> per-shot version history
  POST /api/production/{sid}/artifact-versions/{id}/rollback -> restore version
  GET  /api/production/{sid}/artifact-versions/{id}/annotations -> version annotations
  POST /api/production/{sid}/artifact-versions/{id}/annotations -> add version annotation
  POST /api/production/{sid}/artifact-versions/restore-previous -> batch restore
  GET  /api/production/{sid}/metrics            -> quality, rework, timing and billing facts
  POST /api/production/{sid}/approve           -> approve current review (bg) -> {job_id}
  POST /api/production/{sid}/continue-cancelled -> repeat the exact cancelled operation
  POST /api/production/{sid}/revise            -> revise current stage (bg) {instruction}
  POST /api/production/{sid}/regenerate-shot   -> {shot_idx, scene_index} (bg)
  POST /api/production/{sid}/regeneration-preview -> batch impact and added-cost preview
  POST /api/production/{sid}/regenerate-shots  -> batch shot regeneration (bg)
  POST /api/production/{sid}/review-shots/accept -> batch accept current shots
  GET  /api/production/{sid}/edit-plan          -> final timeline edit plan
  PUT  /api/production/{sid}/edit-plan          -> validate and save edit plan
  POST /api/production/{sid}/edit-plan/render   -> render saved final timeline (bg)
  POST /api/production/{sid}/edit-plan/reset    -> restore original final video (bg)
  GET  /api/production/{sid}/subtitles          -> editable project subtitle timeline
  PUT  /api/production/{sid}/subtitles          -> save project subtitle sidecar
  POST /api/production/{sid}/subtitles/reset    -> restore generated subtitle timing/text
  GET  /api/production/{sid}/subtitles/file     -> download project SRT
  POST /api/production/{sid}/publish           -> host + return link (sync, fast)
  GET  /api/production/{sid}/video             -> final video bytes
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any, Optional, Tuple

from .artifacts_reader import build_manifest, read_script, read_storyboard, resolve_file


class ProductionAPI:
    def __init__(self, session_index: Any, service: Any, adapters: Any, cost_estimator: Any = None,
                 housekeeping: Any = None):
        self.session_index = session_index
        self.service = service  # ProductionService
        self.adapters = adapters
        self.cost_estimator = cost_estimator
        self.housekeeping = housekeeping

    async def handle(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        method = method.upper()
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "production"]:
            return 404, {"error": "not found"}
        rest = parts[2:]
        body = body or {}
        try:
            if not rest:
                if method == "GET":
                    return 200, {"sessions": self._list_sessions()}
                return 405, {"error": "method not allowed"}

            if rest[0] == "topic" and method == "POST":
                idea = str(body.get("idea", "") or "").strip()
                mode = "script" if str(body.get("mode", "") or "") == "script" else "idea"
                script = str(body.get("script", "") or "").strip()
                if mode == "script":
                    if not script:
                        return 400, {"error": "script is required in script mode"}
                elif not idea:
                    return 400, {"error": "idea is required"}
                cast = body.get("character_asset_ids") or []
                cast = [str(x) for x in cast if str(x).strip()] if isinstance(cast, list) else []
                props = body.get("prop_asset_ids") or []
                props = [str(x) for x in props if str(x).strip()] if isinstance(props, list) else []
                scenes = body.get("scene_asset_ids") or []
                scenes = [str(x) for x in scenes if str(x).strip()] if isinstance(scenes, list) else []
                lora_ids = body.get("lora_ids") or []
                lora_ids = [str(x) for x in lora_ids if str(x).strip()] if isinstance(lora_ids, list) else []
                # per-video overrides (None when absent -> fall back to global config)
                tl = body.get("target_language"); tl = None if tl is None else str(tl)
                asp = body.get("aspect_ratio"); asp = None if asp is None else str(asp)
                quality_tier = str(body.get("quality_tier") or "balanced")
                if quality_tier not in {"economy", "balanced", "quality"}:
                    return 400, {"error": "quality_tier must be economy, balanced, or quality"}
                ov = {}
                if "subtitle_enabled" in body: ov["subtitle_enabled"] = bool(body.get("subtitle_enabled"))
                if "subtitle_burn_in" in body: ov["subtitle_burn_in"] = bool(body.get("subtitle_burn_in"))
                if "tts_enabled" in body: ov["tts_enabled"] = bool(body.get("tts_enabled"))
                if body.get("voice"): ov["voice"] = str(body.get("voice"))
                if body.get("bgm_track"): ov["bgm_track"] = str(body.get("bgm_track"))
                source_session_id = str(body.get("continuity_source_session_id") or "").strip()
                if source_session_id and self.session_index.get(source_session_id) is None:
                    return 400, {"error": "continuity source session does not exist"}
                start_kwargs = {
                    "user_requirement": str(body.get("user_requirement", "") or ""),
                    "style": str(body.get("style", "") or ""),
                    "domain": str(body.get("domain", "") or ""),
                    "character_asset_ids": cast,
                    "mode": mode,
                    "script": script,
                    "prop_asset_ids": props,
                    "scene_asset_ids": scenes,
                    "lora_ids": lora_ids,
                    "target_language": tl,
                    "aspect_ratio": asp,
                    "overrides": ov or None,
                    "quality_tier": quality_tier,
                }
                if source_session_id:
                    start_kwargs["continuity_source_session_id"] = source_session_id
                rec = self.service.start_topic(idea, **start_kwargs)
                return 200, rec

            if rest[0] == "domains" and method == "GET":
                from agents.domain_packs import list_domains
                return 200, {"domains": list_domains()}

            if rest[0] == "providers" and method == "GET":
                registry = getattr(self.service.engine, "provider_registry", None)
                return 200, {"providers": registry.public_catalog() if registry else []}

            if rest[0] == "quality-profiles" and method == "GET":
                from services.quality_profiles import public_quality_profiles
                return 200, {"profiles": public_quality_profiles()}

            if rest[0] == "jobs" and len(rest) == 2 and method == "GET":
                job = self.service.job(rest[1])
                return (200, job) if job else (404, {"error": "unknown job"})

            if rest[0] == "jobs" and len(rest) == 3 and rest[2] == "cancel" and method == "POST":
                if self.service.job(rest[1]) is None:
                    return 404, {"error": "unknown job"}
                result = self.service.cancel_job(rest[1])
                return (200, result) if result.get("ok") else (409, result)

            if rest[0] == "jobs" and len(rest) == 1 and method == "GET":
                return 200, {"jobs": self.service.runner.list_recent()}

            if rest[0] == "cleanup-all" and self.housekeeping is not None:
                dirs = [
                    str(self.session_index.working_dir(record["session_id"]))
                    for record in self.session_index.list_sessions()
                ]
                if method == "GET":
                    total, n = 0, 0
                    for wd in dirs:
                        p = self.housekeeping.preview(wd)
                        total += p["freeable_bytes"]
                        n += 1 if p["has_final"] else 0
                    return 200, {"sessions_with_final": n, "freeable_mb": round(total / 1048576, 2)}
                if method == "POST":
                    freed, cleaned = 0, 0
                    for wd in dirs:
                        r = self.housekeeping.cleanup(wd)
                        if r.get("ok"):
                            freed += r.get("freed_bytes", 0)
                            cleaned += 1
                    return 200, {"cleaned_sessions": cleaned, "freed_mb": round(freed / 1048576, 2)}

            sid = rest[0]
            if self.session_index.get(sid) is None:
                return 404, {"error": f"unknown session: {sid}"}

            if len(rest) == 1 and method == "DELETE":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法删除", "busy": True}
                ok = self.session_index.delete(sid)
                return (200, {"ok": True, "deleted": sid}) if ok else (404, {"error": f"unknown session: {sid}"})

            working_dir = self.session_index.working_dir(sid)
            if len(rest) == 1 and method == "GET":
                return 200, self._snapshot(sid)
            if rest[1:] == ["script"] and method == "GET":
                return 200, read_script(working_dir)
            if rest[1:] == ["script"] and method == "PUT":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法编辑剧本", "busy": True}
                text = body.get("text") if isinstance(body, dict) else None
                if not isinstance(text, str):
                    return 400, {"error": "text (string) is required"}
                result = self.service.engine.edit_script(sid, text)
                return (200 if result.get("ok") else 400), result
            if rest[1:] == ["storyboard"] and method == "GET":
                return 200, read_storyboard(working_dir)
            if rest[1:] == ["storyboard"] and method == "PUT":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法编辑分镜", "busy": True}
                scenes = body.get("scenes") if isinstance(body, dict) else None
                if not isinstance(scenes, list):
                    return 400, {"error": "scenes (list) is required"}
                result = self.service.engine.edit_storyboard(sid, scenes)
                return (200 if result.get("ok") else 400), result
            if rest[1:] == ["preview-keyframes"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，请稍候", "busy": True}
                return 200, self.service.preview_keyframes(
                    sid,
                    scene_index=body.get("scene_index") if isinstance(body, dict) else None,
                    shot_index=body.get("shot_index") if isinstance(body, dict) else None,
                    force=bool(body.get("force", False)) if isinstance(body, dict) else False,
                )
            if rest[1:] == ["rewrite-shot"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法重写分镜", "busy": True}
                result = await self.service.engine.rewrite_shot_description(
                    sid, body.get("scene_index"), body.get("shot_index"),
                    shots=body.get("shots"), instruction=str(body.get("instruction", "") or ""))
                return (200 if result.get("ok") else 400), result
            if rest[1:] == ["artifacts"] and method == "GET":
                manifest = build_manifest(working_dir)
                return 200, self.service.enrich_continuity_manifest(sid, manifest)
            if rest[1:] == ["continuity-status"] and method == "GET":
                query = parse_qs(urlsplit(path).query)
                raw_scene = query.get("scene_index", [None])[0]
                scene_index = int(raw_scene) if raw_scene not in (None, "") else None
                return 200, self.service.continuity_status(sid, scene_index=scene_index)
            if rest[1:] == ["artifact-versions"] and method == "GET":
                version_service = getattr(self.service.engine, "artifact_versions", None)
                if version_service is None:
                    return 409, {"error": "artifact versioning is unavailable for this storage backend"}
                query = parse_qs(urlsplit(path).query)
                try:
                    scene_index = int(query.get("scene_index", [""])[0])
                    shot_index = int(query.get("shot_index", [""])[0])
                    artifact_type = str(query.get("artifact_type", [""])[0])
                    versions = version_service.list_versions(
                        sid, scene_index, shot_index, artifact_type)
                except (TypeError, ValueError):
                    return 400, {
                        "error": "scene_index, shot_index and a valid artifact_type are required"
                    }
                return 200, {"versions": [self._artifact_version_payload(item) for item in versions]}
            if len(rest) == 4 and rest[1] == "artifact-versions" and rest[3] == "annotations":
                artifact_id = rest[2]
                if method == "GET":
                    try:
                        annotations = self.service.artifact_annotations(sid, artifact_id)
                    except ValueError as exc:
                        return 404, {"error": str(exc)}
                    return 200, {"annotations": annotations}
                if method == "POST":
                    try:
                        annotation = self.service.add_artifact_annotation(
                            sid,
                            artifact_id,
                            str(body.get("text") or ""),
                            timecode_seconds=body.get("timecode_seconds"),
                            author=str(body.get("author") or ""),
                        )
                    except ValueError as exc:
                        return 400, {"error": str(exc)}
                    return 201, {"annotation": annotation}
            if rest[1:] == ["artifact-versions", "restore-previous"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法恢复历史版本", "busy": True}
                try:
                    result = await self.service.restore_previous_versions(
                        sid,
                        body.get("shots"),
                        artifact_type=str(body.get("artifact_type") or "video"),
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, result
            if len(rest) == 4 and rest[1] == "artifact-versions" and rest[3] in ("rollback", "file"):
                version_service = getattr(self.service.engine, "artifact_versions", None)
                if version_service is None:
                    return 409, {"error": "artifact versioning is unavailable for this storage backend"}
                artifact_id = rest[2]
                version = version_service.repository.get_version(artifact_id)
                if version is None or version.project_id != sid:
                    return 404, {"error": "artifact version not found"}
                if rest[3] == "file" and method == "GET":
                    target = version_service.resolve_version_path(artifact_id)
                    return 200, {
                        "_file": str(target),
                        "_content_type": mimetypes.guess_type(str(target))[0]
                        or "application/octet-stream",
                    }
                if rest[3] == "rollback" and method == "POST":
                    if self.service.runner.is_running(sid):
                        return 409, {"error": "生成进行中，无法回滚版本", "busy": True}
                    activated = version_service.rollback(artifact_id)
                    self.service.record_artifact_selection(sid, activated)
                    return 200, {"ok": True, "version": self._artifact_version_payload(activated)}
            if rest[1:] == ["metrics"] and method == "GET":
                return 200, self.service.production_metrics(sid)
            if rest[1:] == ["subtitles"]:
                if method == "GET":
                    try:
                        return 200, {"timeline": self.service.subtitle_timeline(sid)}
                    except ValueError as exc:
                        return 409, {"error": str(exc)}
                if method == "PUT":
                    if self.service.runner.is_running(sid):
                        return 409, {"error": "项目任务进行中，无法保存字幕", "busy": True}
                    try:
                        timeline = self.service.save_subtitle_timeline(
                            sid, body.get("timeline") if isinstance(body.get("timeline"), dict) else body
                        )
                    except ValueError as exc:
                        return 400, {"error": str(exc)}
                    return 200, {"ok": True, "timeline": timeline}
            if rest[1:] == ["subtitles", "reset"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "项目任务进行中，无法恢复字幕", "busy": True}
                try:
                    timeline = self.service.reset_subtitle_timeline(sid)
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, {"ok": True, "timeline": timeline}
            if rest[1:] == ["subtitles", "file"] and method == "GET":
                try:
                    target = self.service.subtitle_file(sid)
                except ValueError as exc:
                    return 404, {"error": str(exc)}
                return 200, {"_file": str(target), "_content_type": "application/x-subrip"}
            if rest[1:] == ["edit-plan"]:
                if method == "GET":
                    try:
                        return 200, {"plan": self.service.edit_plan(sid)}
                    except ValueError as exc:
                        return 409, {"error": str(exc)}
                if method == "PUT":
                    if self.service.runner.is_running(sid):
                        return 409, {"error": "项目任务进行中，无法保存剪辑方案", "busy": True}
                    try:
                        plan = self.service.save_edit_plan(
                            sid, body.get("plan") if isinstance(body.get("plan"), dict) else body
                        )
                    except ValueError as exc:
                        return 400, {"error": str(exc)}
                    return 200, {"ok": True, "plan": plan}
            if rest[1:] == ["edit-plan", "render"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "项目任务进行中，无法重新合成", "busy": True}
                try:
                    result = self.service.render_edit_plan(
                        sid, body.get("plan") if isinstance(body.get("plan"), dict) else body
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, result
            if rest[1:] == ["edit-plan", "reset"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "项目任务进行中，无法恢复原始成片", "busy": True}
                try:
                    result = self.service.reset_edit_plan(sid)
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, result
            if rest[1:] == ["cost"] and method == "GET":
                if self.cost_estimator is None:
                    return 200, {"estimated_total": None, "note": "成本估算未启用"}
                record = self.session_index.get(sid) or {}
                if str(record.get("stage") or "").startswith("storyboard_review"):
                    preview = self.service.engine.budget_preview(sid)
                    if preview and preview.get("shots"):
                        return 200, self.cost_estimator.estimate_plan(
                            int(preview.get("scenes") or 1),
                            int(preview["shots"]),
                            str(record.get("quality_tier") or "balanced"),
                        )
                return 200, self.cost_estimator.estimate(working_dir)
            if rest[1:] == ["route-preview"] and method == "GET":
                return 200, self.service.engine.provider_route_preview(sid)
            if rest[1:] == ["cleanup"] and self.housekeeping is not None:
                if method == "GET":
                    return 200, self.housekeeping.preview(working_dir)
                if method == "POST":
                    return 200, self.housekeeping.cleanup(working_dir)
            if rest[1:] == ["file"] and method == "GET":
                rel = parse_qs(urlsplit(path).query).get("path", [""])[0]
                target = resolve_file(working_dir, rel)
                if target is None:
                    return 404, {"error": "file not found"}
                return 200, {"_file": str(target), "_content_type": mimetypes.guess_type(str(target))[0] or "application/octet-stream"}
            if rest[1:] == ["approve"] and method == "POST":
                return 200, self.service.approve(sid)
            if rest[1:] == ["revise"] and method == "POST":
                return 200, self.service.revise(sid, str(body.get("instruction", "") or ""))
            if rest[1:] == ["resume"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "已在生成中", "busy": True}
                return 200, self.service.resume(sid)
            if rest[1:] == ["continue-cancelled"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "已在生成中", "busy": True}
                result = self.service.continue_cancelled(sid)
                return (200 if result.get("ok", True) else 409), result
            if rest[1:] == ["reopen"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法退回", "busy": True}
                result = self.service.engine.reopen(sid, str(body.get("gate", "") or ""))
                return (200 if result.get("ok") else 400), result
            if rest[1:] == ["regeneration-impact"] and method == "POST":
                return 200, self.service.regeneration_impact(
                    sid,
                    body.get("shot_idx"),
                    scene_index=body.get("scene_index"),
                    dimensions=body.get("dimensions"),
                )
            if rest[1:] == ["regeneration-preview"] and method == "POST":
                try:
                    preview = self.service.batch_regeneration_impact(
                        sid,
                        body.get("shots"),
                        dimensions=body.get("dimensions"),
                        locked_dimensions=body.get("locked_dimensions"),
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, preview
            if rest[1:] == ["review-shots", "accept"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法批量接受镜头", "busy": True}
                try:
                    result = self.service.accept_shots(sid, body.get("shots"))
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, result
            if rest[1:] == ["regenerate-shot"] and method == "POST":
                # Optional edited prompt: when present, the shot's storyboard entry is
                # updated and the shot re-decomposed (keep_description forced False).
                desc = {k: body[k] for k in (
                            "visual_desc", "audio_desc", "screen_text", "screen_text_pos",
                            "duration_sec", "director_desc", "beats", "visual_style", "avoid",
                        )
                        if isinstance(body, dict) and k in body}
                dimensions = body.get("dimensions") if isinstance(body.get("dimensions"), list) else []
                locked_dimensions = body.get("locked_dimensions") if isinstance(body.get("locked_dimensions"), list) else []
                return 200, self.service.regenerate_shot(
                    sid,
                    body.get("shot_idx"),
                    scene_index=body.get("scene_index"),
                    keep_description=bool(body.get("keep_description", True)),
                    description=(desc or None),
                    reason=str(body.get("reason") or ("prompt_edit" if desc else "user_requested")),
                    dimensions=dimensions,
                    locked_dimensions=locked_dimensions,
                )
            if rest[1:] == ["regenerate-shots"] and method == "POST":
                if self.service.runner.is_running(sid):
                    return 409, {"error": "生成进行中，无法提交批量返工", "busy": True}
                dimensions = body.get("dimensions") if isinstance(body.get("dimensions"), list) else []
                locked_dimensions = body.get("locked_dimensions") if isinstance(body.get("locked_dimensions"), list) else []
                try:
                    result = self.service.regenerate_shots(
                        sid,
                        body.get("shots"),
                        reason=str(body.get("reason") or "user_requested"),
                        dimensions=dimensions,
                        locked_dimensions=locked_dimensions,
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                return 200, result
            if rest[1:] == ["publish"] and method == "POST":
                result = await self.adapters.sceneforge_publish({"session_id": sid})
                return (200 if result.ok else 400), result.metadata
            if rest[1:] == ["video"] and method == "GET":
                final = self.adapters._find_final_video(self.session_index.working_dir(sid))
                if final is None:
                    return 404, {"error": "no final video yet"}
                return 200, {"_file": str(final), "_content_type": "video/mp4"}
            return 404, {"error": "not found"}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _artifact_version_payload(version) -> dict:
        return {
            "artifact_id": version.artifact_id,
            "project_id": version.project_id,
            "scene_index": version.scene_index,
            "shot_index": version.shot_index,
            "artifact_type": version.artifact_type.value,
            "version": version.version,
            "status": version.status.value,
            "input_hash": version.input_hash,
            "relative_path": version.relative_path,
            "inputs": dict(version.inputs),
            "metadata": dict(version.metadata),
            "created_at": version.created_at,
            "activated_at": version.activated_at,
        }

    def _interrupted(self, sid: str, stage: str) -> bool:
        """A generation left mid-flight by a restart/crash: stage says *_generating
        / *_revision_requested but no job is actually running. Such a session can be
        resumed (re-run the gate, skipping artifacts already on disk)."""
        s = str(stage or "")
        if not (s.endswith("_generating") or s.endswith("_revision_requested")):
            return False
        try:
            return not self.service.runner.is_running(sid)
        except Exception:
            return False

    def _list_sessions(self) -> list:
        out = []
        for rec in self.session_index.list_sessions():
            if isinstance(rec, dict):
                sid = str(rec.get("session_id") or "")
                out.append({"session_id": sid, "stage": rec.get("stage", ""), "idea": str(rec.get("idea", ""))[:80],
                            "updated_at": rec.get("updated_at", ""), "interrupted": self._interrupted(sid, rec.get("stage", "")),
                            "character_asset_ids": list(rec.get("character_asset_ids") or []),
                            "prop_asset_ids": list(rec.get("prop_asset_ids") or []),
                            "scene_asset_ids": list(rec.get("scene_asset_ids") or []),
                            "continuity_source_session_id": str(rec.get("continuity_source_session_id") or ""),
                            "continuity_available": any(
                                path.is_file()
                                for pattern in (
                                    "idea2video/scene_*/continuity_ledger.json",
                                    "idea2video/scene_*/continuity_contracts.json",
                                )
                                for path in Path(self.session_index.working_dir(sid)).glob(pattern)
                            )})
        return out

    def _has_final(self, sid: str) -> bool:
        """Whether a final video exists yet — lets the UI show 看成片/发布/清理 only
        once there's something to act on (the final video is produced at the
        ``final`` stage)."""
        try:
            return self.adapters is not None and \
                self.adapters._find_final_video(self.session_index.working_dir(sid)) is not None
        except Exception:
            return False

    def _last_error(self, sid: str, busy: bool) -> Optional[dict]:
        """The last action's failure for ``sid`` so the UI can show it after a
        refresh (the failed job is no longer polled). A budget rejection surfaces
        as ``result.ok == False`` (job state "done"); a crash as job state
        "failed". ``None`` while busy or when the last job succeeded — so it
        self-clears once the user retries successfully."""
        if busy:
            return None
        try:
            lj = self.service.runner.last_job(sid)
        except Exception:
            return None
        if not lj:
            return None
        if str(lj.get("internal_state") or "") == "canceled":
            return None
        result = lj.get("result")
        if isinstance(result, dict) and result.get("ok") is False:
            return {"stage": result.get("stage"), "error": result.get("error"), "note": result.get("note")}
        if lj.get("state") == "failed" and lj.get("error"):
            error = str(lj.get("error"))
            job_type = str(lj.get("job_type") or "")
            has_handler = getattr(self.service.runner, "has_handler", None)
            if (
                "no handler registered for job_type" in error
                and job_type
                and callable(has_handler)
                and has_handler(job_type)
            ):
                return None
            return {"stage": None, "error": "exception", "note": error}
        return None

    def _last_cancelled(self, sid: str, busy: bool) -> bool:
        if busy:
            return False
        try:
            last = self.service.runner.last_job(sid)
        except Exception:
            return False
        return bool(last and str(last.get("internal_state") or "") == "canceled")

    def _last_cancelled_job_type(self, sid: str, busy: bool) -> Optional[str]:
        if busy:
            return None
        try:
            last = self.service.runner.last_job(sid)
        except Exception:
            return None
        if not last or str(last.get("internal_state") or "") != "canceled":
            return None
        return str(last.get("job_type") or "") or None

    def _snapshot(self, sid: str) -> dict:
        record = self.session_index.get(sid) or {}
        reviews = self.session_index.list_review_tasks(sid)
        pending = next((t for t in reviews if t.get("status") == "pending"), None)
        busy = self.service.runner.is_running(sid)
        # At the storyboard review, preview shot/scene counts vs the budget so the
        # UI can warn BEFORE 通过 (over-limit would otherwise only show as a failed
        # approve). Only meaningful at that stage; cheap to skip otherwise.
        budget_preview = None
        provider_route_preview = None
        if not busy and str(record.get("stage", "")).startswith("storyboard_review"):
            try:
                budget_preview = self.service.engine.budget_preview(sid)
            except Exception:
                budget_preview = None
            try:
                provider_route_preview = self.service.engine.provider_route_preview(sid)
            except Exception:
                provider_route_preview = None
        return {
            "session_id": sid,
            "stage": record.get("stage", ""),
            "idea": record.get("idea", ""),
            "summary": record.get("summary", ""),
            "pending_review": pending,
            "review_tasks": reviews,
            "artifacts": self.session_index.artifact_checklist(sid),
            "busy": busy,
            "job_id": self.service.runner.running_job_id(sid),
            "interrupted": self._interrupted(sid, record.get("stage", "")),
            "cancelled": self._last_cancelled(sid, busy),
            "cancelled_job_type": self._last_cancelled_job_type(sid, busy),
            "has_final": self._has_final(sid),
            "last_error": self._last_error(sid, busy),
            "budget_preview": budget_preview,
            "provider_route_preview": provider_route_preview,
            "provider_route": record.get("provider_route"),
            "quality_tier": record.get("quality_tier", "balanced"),
            "continuity_source_session_id": record.get("continuity_source_session_id", ""),
            "loras": [
                {
                    "lora_id": item.get("lora_id"),
                    "display_name": item.get("display_name") or item.get("lora_id"),
                    "application_mode": item.get("application_mode", "native"),
                    "default_weight": item.get("default_weight", 0.8),
                }
                for item in (record.get("lora_bindings") or []) if isinstance(item, dict)
            ],
        }
