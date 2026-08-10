"""Orchestrates the workflow as background jobs with completion notifications.

start_topic / approve / revise / regenerate_shot return immediately (a job
record) and run the slow generation in the background via JobRunner. When a job
finishes, if a ``target`` was given, the summary is pushed through ``notifier``
(a ChannelDispatcher-style async callable) so the message-channel review loop
works: user replies "通过" -> ack now -> next stage generates -> push "请审核".

Both the web (poll job/snapshot, target=None -> no push) and message channels
(target=sender -> push) share one instance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from domain.jobs import GenerationJob, JobSpec


class ProductionService:
    _START_TOPIC = "workflow.start_topic"
    _APPROVE = "workflow.approve"
    _REVISE = "workflow.revise"
    _RESUME = "workflow.resume"
    _REGENERATE_SHOT = "workflow.regenerate_shot"
    _REGENERATE_SHOTS = "workflow.regenerate_shots"
    _PREVIEW_KEYFRAMES = "workflow.preview_keyframes"
    _RENDER_EDIT_PLAN = "workflow.render_edit_plan"
    _RESET_EDIT_PLAN = "workflow.reset_edit_plan"

    _LOCK_DIMENSIONS = {"identity", "composition", "motion", "audio"}

    def __init__(self, engine: Any, runner: Any, adapters: Any = None,
                 notifier: Optional[Callable[[str, str], Awaitable[None]]] = None):
        self.engine = engine
        self.runner = runner
        self.adapters = adapters
        self.notifier = notifier
        self._durable = hasattr(runner, "submit_job") and hasattr(runner, "register_handler")
        if self._durable:
            runner.register_handler(self._START_TOPIC, self._handle_start_topic)
            runner.register_handler(self._APPROVE, self._handle_approve)
            runner.register_handler(self._REVISE, self._handle_revise)
            runner.register_handler(self._RESUME, self._handle_resume)
            runner.register_handler(self._REGENERATE_SHOT, self._handle_regenerate_shot)
            runner.register_handler(self._REGENERATE_SHOTS, self._handle_regenerate_shots)
            runner.register_handler(self._PREVIEW_KEYFRAMES, self._handle_preview_keyframes)
            runner.register_handler(self._RENDER_EDIT_PLAN, self._handle_render_edit_plan)
            runner.register_handler(self._RESET_EDIT_PLAN, self._handle_reset_edit_plan)
            runner.set_terminal_hook(self._durable_terminal)
            runner.start()

    # ----- background operations ---------------------------------------

    # Numeric/id meta fields forwarded to polling clients so the UI can render a
    # determinate progress bar (e.g. 第 X/N 镜). Paths and other bulky/sensitive
    # fields are intentionally dropped.
    _META_KEYS = ("shot_count", "shot_idx", "scene_idx", "camera_idx", "camera_count",
                  "frame_type", "count", "character_count", "line_count", "attempt",
                  "max_attempts", "wait_seconds", "max_concurrent", "batch_index",
                  "batch_count", "affected_count", "clip_count", "candidate_index",
                  "candidate_count", "selected_candidate")

    @classmethod
    def _sink(cls, prog: list):
        """A progress callback that appends compact entries to ``prog`` (capped),
        so polling clients can show live stage messages + a progress bar."""
        def report(stage, message, meta=None):
            try:
                if hasattr(prog, "cancel_requested") and prog.cancel_requested():
                    raise RuntimeError("job cancellation requested")
                entry = {"stage": stage, "message": message}
                if isinstance(meta, dict):
                    m = {k: meta[k] for k in cls._META_KEYS if k in meta}
                    internal = dict(m)
                    for key in (
                        "task_id", "job_id", "provider", "model", "status",
                        "base_url", "polling_url", "artifact_path",
                    ):
                        if key in meta:
                            internal[key] = meta[key]
                    if hasattr(prog, "event"):
                        prog.event(stage, message, internal)
                        return
                    if m:
                        entry["meta"] = m
                if hasattr(prog, "event"):
                    prog.event(stage, message, {})
                else:
                    prog.append(entry)
                    if len(prog) > 100:
                        del prog[:-100]
            except RuntimeError:
                raise
            except Exception:
                pass
        return report

    def start_topic(self, idea: str, target: Optional[str] = None, user_requirement: str = "", style: str = "", domain: str = "", character_asset_ids: Optional[list] = None, prop_asset_ids: Optional[list] = None, scene_asset_ids: Optional[list] = None, lora_ids: Optional[list] = None, mode: str = "idea", script: str = "", target_language: Optional[str] = None, aspect_ratio: Optional[str] = None, overrides: Optional[dict] = None, quality_tier: str = "balanced", continuity_source_session_id: Optional[str] = None, series_id: Optional[str] = None, episode_number: Optional[int] = None, episode_title: str = "", episode_outline: str = "", previous_episode_id: Optional[str] = None, series_context: Optional[dict] = None) -> dict:
        payload = {
            "idea": idea,
            "user_requirement": user_requirement,
            "style": style,
            "domain": domain,
            "character_asset_ids": list(character_asset_ids or []),
            "prop_asset_ids": list(prop_asset_ids or []),
            "scene_asset_ids": list(scene_asset_ids or []),
            "lora_ids": list(lora_ids or []),
            "mode": mode,
            "script": script,
            "target_language": target_language,
            "aspect_ratio": aspect_ratio,
            "overrides": dict(overrides or {}),
            "quality_tier": quality_tier,
            "continuity_source_session_id": str(continuity_source_session_id or ""),
            "series_id": str(series_id or ""),
            "episode_number": episode_number,
            "episode_title": str(episode_title or ""),
            "episode_outline": str(episode_outline or ""),
            "previous_episode_id": str(previous_episode_id or ""),
            "series_context": dict(series_context or {}),
        }
        if self._durable:
            return self._submit_durable(self._START_TOPIC, payload, target=target)
        prog: list = []
        async def work():
            kwargs = {
                "user_requirement": user_requirement, "style": style, "domain": domain,
                "character_asset_ids": character_asset_ids, "prop_asset_ids": prop_asset_ids,
                "scene_asset_ids": scene_asset_ids, "mode": mode, "script": script,
                "target_language": target_language, "aspect_ratio": aspect_ratio,
                "overrides": overrides, "quality_tier": quality_tier, "progress": self._sink(prog),
            }
            if lora_ids:
                kwargs["lora_ids"] = lora_ids
            if continuity_source_session_id:
                kwargs["continuity_source_session_id"] = continuity_source_session_id
            if series_id:
                kwargs.update({
                    "series_id": series_id,
                    "episode_number": episode_number,
                    "episode_title": episode_title,
                    "episode_outline": episode_outline,
                    "previous_episode_id": previous_episode_id,
                    "series_context": dict(series_context or {}),
                })
            return await self.engine.start_topic(idea, **kwargs)
        return self.runner.submit(work, key=None, on_done=self._notify_cb(target), progress=prog)

    def approve(self, session_id: str, target: Optional[str] = None) -> dict:
        if self._durable:
            return self._submit_durable(self._APPROVE, {"session_id": session_id}, key=session_id, target=target)
        prog: list = []
        async def work():
            return await self.engine.approve(session_id, progress=self._sink(prog))
        return self.runner.submit(work, key=session_id, on_done=self._notify_cb(target), progress=prog)

    def revise(self, session_id: str, instruction: str, target: Optional[str] = None) -> dict:
        if self._durable:
            return self._submit_durable(
                self._REVISE,
                {"session_id": session_id, "instruction": instruction},
                key=session_id,
                target=target,
            )
        prog: list = []
        async def work():
            return await self.engine.revise(session_id, instruction, progress=self._sink(prog))
        return self.runner.submit(work, key=session_id, on_done=self._notify_cb(target), progress=prog)

    def resume(self, session_id: str, target: Optional[str] = None) -> dict:
        if self._durable:
            return self._submit_durable(self._RESUME, {"session_id": session_id}, key=session_id, target=target)
        prog: list = []
        async def work():
            return await self.engine.resume_generation(session_id, progress=self._sink(prog))
        return self.runner.submit(work, key=session_id, on_done=self._notify_cb(target), progress=prog)

    def edit_plan(self, session_id: str) -> dict:
        return self._timeline_editor(session_id).get_plan()

    def save_edit_plan(self, session_id: str, plan: Any) -> dict:
        return self._timeline_editor(session_id).save_plan(plan)

    def subtitle_timeline(self, session_id: str) -> dict:
        return self._subtitle_timeline(session_id).get_plan()

    def save_subtitle_timeline(self, session_id: str, plan: Any) -> dict:
        return self._subtitle_timeline(session_id).save_plan(plan)

    def reset_subtitle_timeline(self, session_id: str) -> dict:
        return self._subtitle_timeline(session_id).reset()

    def subtitle_file(self, session_id: str) -> Path:
        return self._subtitle_timeline(session_id).download_path()

    def render_edit_plan(
        self, session_id: str, plan: Any, target: Optional[str] = None
    ) -> dict:
        editor = self._timeline_editor(session_id)
        normalized = editor.save_plan(plan)
        payload = {
            "session_id": session_id,
            "plan": normalized,
            "input_fingerprint": editor.output_fingerprint(),
        }
        if self._durable:
            return self._submit_durable(
                self._RENDER_EDIT_PLAN, payload, key=session_id, target=target
            )

        async def work():
            return await asyncio.to_thread(self._execute_timeline_render, payload)

        return self.runner.submit(
            work, key=session_id, on_done=self._notify_cb(target)
        )

    def reset_edit_plan(
        self, session_id: str, target: Optional[str] = None
    ) -> dict:
        editor = self._timeline_editor(session_id)
        if not editor.has_original_source():
            raise ValueError("the project does not have an original edit source")
        payload = {
            "session_id": session_id,
            "input_fingerprint": editor.output_fingerprint(),
        }
        if self._durable:
            return self._submit_durable(
                self._RESET_EDIT_PLAN, payload, key=session_id, target=target
            )

        async def work():
            return await asyncio.to_thread(self._execute_timeline_reset, payload)

        return self.runner.submit(
            work, key=session_id, on_done=self._notify_cb(target)
        )

    def regenerate_shot(self, session_id: str, shot_idx: Any, scene_index: Optional[int] = None,
                        keep_description: bool = True, description: Optional[dict] = None,
                        reason: str = "user_requested", dimensions: Optional[list[str]] = None,
                        locked_dimensions: Optional[list[str]] = None,
                        target: Optional[str] = None) -> dict:
        args = {
            "session_id": session_id,
            "shot_idx": shot_idx,
            "keep_description": keep_description,
            "reason": str(reason or "user_requested"),
            "dimensions": list(dimensions or []),
            "locked_dimensions": self._normalize_locked_dimensions(locked_dimensions),
        }
        if scene_index is not None:
            args["scene_index"] = scene_index
        # An edited prompt forces re-decomposition (the new description is written to
        # the storyboard before regenerating).
        if description:
            args.update({k: v for k, v in description.items() if v is not None})

        if self._durable:
            return self._submit_durable(self._REGENERATE_SHOT, args, key=session_id, target=target)

        async def work():
            result = await self.adapters.sceneforge_regenerate_shot(args)
            if result.ok:
                if hasattr(self.engine, "rebuild_after_shot_regeneration"):
                    rebuilt = await self.engine.rebuild_after_shot_regeneration(session_id)
                    if result.metadata is not None:
                        result.metadata.update(rebuilt)
                self._record_regenerated_versions(args, result.metadata or {})
            return {"ok": result.ok, **(result.metadata or {})}
        return self.runner.submit(work, key=session_id, on_done=self._notify_cb(target))

    def regenerate_shots(
        self,
        session_id: str,
        shots: Any,
        *,
        reason: str = "user_requested",
        dimensions: Optional[list[str]] = None,
        locked_dimensions: Optional[list[str]] = None,
        target: Optional[str] = None,
    ) -> dict:
        preview = self.batch_regeneration_impact(
            session_id,
            shots,
            dimensions=dimensions,
            locked_dimensions=locked_dimensions,
        )
        payload = {
            "session_id": session_id,
            "shots": preview["execution_roots"],
            "requested_shots": preview["requested_shots"],
            "affected_shots": preview["affected_shots"],
            "savings_estimate": preview.get("savings_estimate") or {},
            "reason": str(reason or "user_requested"),
            "dimensions": list(dimensions or []),
            "locked_dimensions": preview["locked_dimensions"],
        }
        if self._durable:
            return self._submit_durable(
                self._REGENERATE_SHOTS, payload, key=session_id, target=target)

        progress: list = []

        async def work():
            results = []
            for index, shot in enumerate(payload["shots"]):
                args = self._batch_shot_args(payload, shot)
                progress.append({
                    "stage": "batch_shot_start",
                    "message": f"Regenerating batch shot {index + 1}/{len(payload['shots'])}",
                    "meta": {"batch_index": index + 1, "batch_count": len(payload["shots"])},
                })
                result = await self.adapters.sceneforge_regenerate_shot(args)
                metadata = dict(result.metadata or {})
                results.append({"ok": result.ok, **metadata})
                if not result.ok:
                    return {"ok": False, "results": results, **self._batch_result_summary(payload)}
                self._record_regenerated_versions(args, metadata)
            rebuilt = {}
            if hasattr(self.engine, "rebuild_after_shot_regeneration"):
                rebuilt = await self.engine.rebuild_after_shot_regeneration(session_id)
            self._record_rework_savings(payload)
            return {
                "ok": True,
                "results": results,
                **self._batch_result_summary(payload),
                **(rebuilt or {}),
            }

        return self.runner.submit(
            work,
            key=session_id,
            on_done=self._notify_cb(target),
            progress=progress,
        )

    def regeneration_impact(
        self,
        session_id: str,
        shot_idx: Any,
        scene_index: Optional[int] = None,
        dimensions: Any = None,
    ) -> dict:
        render_dir, resolved_scene = self._render_dir_for_versions(
            Path(self.engine.session_index.working_dir(session_id)),
            int(scene_index) if scene_index is not None else None,
        )
        if render_dir is None:
            raise ValueError("render artifacts are not available")
        index = int(shot_idx)
        from quality import load_continuity_ledger, regeneration_impact

        ledger = load_continuity_ledger(render_dir / "continuity_ledger.json")
        if ledger.get("shots"):
            selected_dimensions = (
                dimensions if isinstance(dimensions, list)
                else [dimensions] if dimensions else []
            )
            result = regeneration_impact(ledger, index, selected_dimensions)
            result["source"] = "continuity_ledger"
        else:
            indexes = self._affected_shot_indexes(render_dir, index)
            result = {
                "shot_idx": index,
                "dimensions": [],
                "scope": "continuity_chain" if len(indexes) > 1 else "single_shot",
                "affected_count": len(indexes),
                "affected_shots": [
                    {
                        "shot_idx": affected,
                        "reasons": ["camera_dependency" if affected != index else "direct_edit"],
                    }
                    for affected in indexes
                ],
                "source": "camera_tree_fallback",
            }
        result["scene_index"] = resolved_scene
        result["cost_estimate"] = self._regeneration_cost_estimate(
            session_id, int(result.get("affected_count") or 1))
        result["savings_estimate"] = self._regeneration_savings_estimate(
            session_id,
            int(result.get("affected_count") or 1),
            result["cost_estimate"],
        )
        return result

    def batch_regeneration_impact(
        self,
        session_id: str,
        shots: Any,
        *,
        dimensions: Any = None,
        locked_dimensions: Any = None,
    ) -> dict:
        if not isinstance(shots, list) or not shots:
            raise ValueError("shots must be a non-empty list")
        if len(shots) > 20:
            raise ValueError("a batch can contain at most 20 shots")

        requested = []
        seen = set()
        for raw in shots:
            if not isinstance(raw, dict):
                raise ValueError("each shot must contain scene_index and shot_idx")
            try:
                shot_index = int(raw.get("shot_idx"))
                scene_index = raw.get("scene_index")
                scene_index = None if scene_index is None else int(scene_index)
            except (TypeError, ValueError):
                raise ValueError("scene_index and shot_idx must be integers") from None
            impact = self.regeneration_impact(
                session_id, shot_index, scene_index=scene_index, dimensions=dimensions)
            resolved_scene = int(impact["scene_index"])
            key = (resolved_scene, shot_index)
            if key in seen:
                continue
            seen.add(key)
            requested.append({
                "scene_index": resolved_scene,
                "shot_idx": shot_index,
                "impact": impact,
            })
        if not requested:
            raise ValueError("shots must contain at least one unique shot")

        requested.sort(key=lambda item: (item["scene_index"], item["shot_idx"]))
        execution_roots = []
        covered = set()
        for item in requested:
            key = (item["scene_index"], item["shot_idx"])
            if key in covered:
                continue
            execution_roots.append({"scene_index": key[0], "shot_idx": key[1]})
            covered.update(
                (key[0], int(affected["shot_idx"]))
                for affected in item["impact"].get("affected_shots") or []
            )

        affected_map = {}
        for item in requested:
            for affected in item["impact"].get("affected_shots") or []:
                key = (item["scene_index"], int(affected["shot_idx"]))
                entry = affected_map.setdefault(key, {
                    "scene_index": key[0],
                    "shot_idx": key[1],
                    "reasons": [],
                })
                entry["reasons"] = sorted(set(
                    entry["reasons"] + list(affected.get("reasons") or [])
                ))
        affected_shots = sorted(
            affected_map.values(), key=lambda item: (item["scene_index"], item["shot_idx"])
        )
        cost_estimate = self._regeneration_cost_estimate(
            session_id, len(affected_shots)
        )
        return {
            "session_id": session_id,
            "requested_shots": [
                {"scene_index": item["scene_index"], "shot_idx": item["shot_idx"]}
                for item in requested
            ],
            "execution_roots": execution_roots,
            "affected_shots": affected_shots,
            "requested_count": len(requested),
            "execution_count": len(execution_roots),
            "affected_count": len(affected_shots),
            "dimensions": list(dimensions or []) if isinstance(dimensions, list) else [],
            "locked_dimensions": self._normalize_locked_dimensions(locked_dimensions),
            "cost_estimate": cost_estimate,
            "savings_estimate": self._regeneration_savings_estimate(
                session_id, len(affected_shots), cost_estimate
            ),
        }

    def production_metrics(self, session_id: str) -> dict:
        from services.production_metrics import aggregate_production_metrics

        return aggregate_production_metrics(
            self.engine.session_index.working_dir(session_id),
            session_id=session_id,
        )

    def artifact_annotations(self, session_id: str, artifact_id: str) -> list[dict]:
        version = self._artifact_version_for_session(session_id, artifact_id)
        from services.production_metrics import list_artifact_annotations

        return list_artifact_annotations(
            self.engine.session_index.working_dir(session_id), version.artifact_id
        )

    def add_artifact_annotation(
        self,
        session_id: str,
        artifact_id: str,
        text: Any,
        *,
        timecode_seconds: Any = None,
        author: Any = "",
    ) -> dict:
        version = self._artifact_version_for_session(session_id, artifact_id)
        content = str(text or "").strip()
        if not content:
            raise ValueError("annotation text is required")
        if len(content) > 1000:
            raise ValueError("annotation text must not exceed 1000 characters")
        timecode = None
        if timecode_seconds not in (None, ""):
            try:
                timecode = round(float(timecode_seconds), 3)
            except (TypeError, ValueError):
                raise ValueError("timecode_seconds must be a number") from None
            if timecode < 0 or timecode > 86_400:
                raise ValueError("timecode_seconds must be between 0 and 86400")
        normalized_author = str(author or "").strip()[:80]

        from services.production_metrics import append_decision

        event = append_decision(
            self.engine.session_index.working_dir(session_id),
            "artifact_annotation",
            scene_index=int(version.scene_index),
            shot_index=int(version.shot_index),
            reason="version_review",
            metadata={
                "artifact_id": version.artifact_id,
                "artifact_type": version.artifact_type.value,
                "artifact_version": version.version,
                "text": content,
                "timecode_seconds": timecode,
                "author": normalized_author,
            },
        )
        return {
            "annotation_id": event["event_id"],
            "artifact_id": version.artifact_id,
            "scene_index": int(version.scene_index),
            "shot_index": int(version.shot_index),
            "text": content,
            "timecode_seconds": timecode,
            "author": normalized_author,
            "created_at": event["timestamp"],
        }

    def accept_shots(self, session_id: str, shots: Any) -> dict:
        requested = self._normalize_shot_requests(session_id, shots)
        metrics_by_shot = {
            (int(item["scene_index"]), int(item["shot_index"])): item
            for item in self.production_metrics(session_id).get("shots") or []
        }
        working_dir = Path(self.engine.session_index.working_dir(session_id))
        from domain.artifacts import ShotReadiness
        from services.production_metrics import (
            append_decision,
            current_generation_id,
            rebuild_provider_performance,
        )

        accepted = []
        skipped = []
        versions = getattr(self.engine, "artifact_versions", None)
        for shot in requested:
            key = (shot["scene_index"], shot["shot_idx"])
            render_dir, _ = self._render_dir_for_versions(working_dir, key[0])
            video = render_dir / "shots" / str(key[1]) / "video.mp4" if render_dir else None
            if video is None or not video.is_file():
                raise ValueError(f"video is not available for scene {key[0]} shot {key[1]}")
            generation_id = current_generation_id(working_dir, key[0], key[1])
            metric = metrics_by_shot.get(key) or {}
            if metric.get("accepted") and (
                not generation_id
                or str((metric.get("current_generation") or {}).get("generation_id") or "")
                == str(generation_id)
            ):
                skipped.append(shot)
                continue
            active_artifact_id = None
            active_input_hash = ""
            if versions is not None:
                history = versions.list_versions(session_id, key[0], key[1], "video")
                active = next((item for item in history if item.status.value == "active"), None)
                if active is not None:
                    active_artifact_id = active.artifact_id
                    active_input_hash = active.input_hash
            append_decision(
                working_dir,
                "accepted",
                scene_index=key[0],
                shot_index=key[1],
                generation_id=generation_id,
                reason="batch_review_accepted",
                metadata={
                    "artifact_id": active_artifact_id,
                    "artifact": str(video.relative_to(working_dir).as_posix()),
                },
            )
            if versions is not None:
                versions.repository.set_shot_state(
                    session_id,
                    key[0],
                    key[1],
                    ShotReadiness.APPROVED,
                    input_hash=active_input_hash,
                )
            accepted.append(shot)
        rebuild_provider_performance(self.engine.workspace_root, self.engine.session_index)
        return {
            "ok": True,
            "requested_count": len(requested),
            "accepted_count": len(accepted),
            "skipped_count": len(skipped),
            "accepted_shots": accepted,
            "skipped_shots": skipped,
        }

    async def restore_previous_versions(
        self, session_id: str, shots: Any, artifact_type: str = "video"
    ) -> dict:
        requested = self._normalize_shot_requests(session_id, shots)
        versions = getattr(self.engine, "artifact_versions", None)
        if versions is None:
            raise ValueError("artifact versioning is unavailable")

        targets = []
        skipped = []
        for shot in requested:
            history = versions.list_versions(
                session_id, shot["scene_index"], shot["shot_idx"], artifact_type
            )
            active = next((item for item in history if item.status.value == "active"), None)
            candidates = [
                item for item in history
                if active is not None
                and item.version < active.version
                and item.status.value == "archived"
            ]
            target = None
            for candidate in candidates:
                try:
                    versions.resolve_version_path(candidate.artifact_id)
                    target = candidate
                    break
                except (FileNotFoundError, KeyError, ValueError):
                    continue
            if target is None:
                skipped.append({**shot, "reason": "no_previous_usable_version"})
            else:
                targets.append(target)

        restored = []
        for target in targets:
            activated = versions.rollback(target.artifact_id)
            self.record_artifact_selection(
                session_id, activated, reason="previous_usable_version_restored"
            )
            restored.append({
                "scene_index": int(activated.scene_index),
                "shot_idx": int(activated.shot_index),
                "artifact_id": activated.artifact_id,
                "version": activated.version,
            })
        rebuilt = {}
        if restored and artifact_type == "video" and hasattr(
            self.engine, "rebuild_after_shot_regeneration"
        ):
            rebuilt = await self.engine.rebuild_after_shot_regeneration(session_id)
        return {
            "ok": True,
            "requested_count": len(requested),
            "restored_count": len(restored),
            "skipped_count": len(skipped),
            "restored_shots": restored,
            "skipped_shots": skipped,
            **(rebuilt or {}),
        }

    def record_artifact_selection(
        self, session_id: str, version: Any, *, reason: str = "version_selected"
    ) -> None:
        from services.production_metrics import (
            append_decision,
            current_generation_id,
            rebuild_provider_performance,
        )

        working_dir = self.engine.session_index.working_dir(session_id)
        scene_index = int(version.scene_index)
        shot_index = int(version.shot_index)
        append_decision(
            working_dir,
            "artifact_selected",
            scene_index=scene_index,
            shot_index=shot_index,
            generation_id=current_generation_id(
                working_dir, scene_index, shot_index
            ),
            reason=reason,
            metadata={
                "artifact_id": version.artifact_id,
                "artifact_type": version.artifact_type.value,
                "version": version.version,
            },
        )
        rebuild_provider_performance(
            self.engine.workspace_root, self.engine.session_index
        )

    def continuity_status(
        self,
        session_id: str,
        scene_index: Optional[int] = None,
    ) -> dict:
        from quality import evaluate_asset_invalidations, load_continuity_ledger

        session = self.engine.session_index.get(session_id)
        if session is None:
            raise ValueError(f"unknown session: {session_id}")
        working_dir = Path(self.engine.session_index.working_dir(session_id))
        script_dir = working_dir / "script2video"
        if (script_dir / "continuity_ledger.json").is_file():
            candidates = [(0, script_dir)]
        else:
            candidates = []
            for path in (working_dir / "idea2video").glob("scene_*"):
                if not (path / "continuity_ledger.json").is_file():
                    continue
                try:
                    index = int(path.name.split("_", 1)[1])
                except (IndexError, ValueError):
                    continue
                if scene_index is None or index == int(scene_index):
                    candidates.append((index, path))
            candidates.sort(key=lambda item: item[0])

        registry_resolver = getattr(self.engine, "_registry_for", None)
        reusable_resolver = getattr(self.engine, "_reusable_assets", None)
        registry = registry_resolver(session) if callable(registry_resolver) else None
        reusable = reusable_resolver(session) if callable(reusable_resolver) else None
        scenes = []
        for index, render_dir in candidates:
            ledger = load_continuity_ledger(render_dir / "continuity_ledger.json")
            evaluation = evaluate_asset_invalidations(
                ledger,
                character_assets=registry,
                reusable_assets=reusable,
            )
            scenes.append({
                "scene_index": index,
                "status": evaluation["status"],
                "summary": evaluation["summary"],
                "changed_assets": evaluation["changed_assets"],
                "shots": evaluation["shots"],
                "inheritance": ledger.get("inheritance") or {"enabled": False},
            })
        changed_asset_ids = sorted({
            item["asset_id"]
            for scene in scenes
            for item in scene.get("changed_assets") or []
        })
        stale_shots = sum(int((scene.get("summary") or {}).get("stale_shot_count", 0)) for scene in scenes)
        return {
            "session_id": session_id,
            "status": ("untracked" if not scenes else "stale" if changed_asset_ids else "current"),
            "summary": {
                "scene_count": len(scenes),
                "changed_asset_count": len(changed_asset_ids),
                "stale_shot_count": stale_shots,
            },
            "scenes": scenes,
        }

    def enrich_continuity_manifest(self, session_id: str, manifest: dict) -> dict:
        status = self.continuity_status(session_id)
        evaluations = {item["scene_index"]: item for item in status.get("scenes") or []}
        for scene in manifest.get("scenes") or []:
            evaluation = evaluations.get(scene.get("scene_index"))
            if evaluation is None:
                continue
            scene["continuity_health"] = {
                "status": evaluation["status"],
                "summary": evaluation["summary"],
                "changed_assets": evaluation["changed_assets"],
                "inheritance": evaluation["inheritance"],
            }
            shot_status = evaluation.get("shots") or {}
            for shot in scene.get("shots") or []:
                changes = shot_status.get(str(shot.get("idx"))) or {}
                continuity = dict(shot.get("continuity") or {})
                continuity["invalidations"] = list(changes.get("invalidations") or [])
                suggestions = [
                    *(continuity.get("repair_suggestions") or []),
                    *(changes.get("repair_suggestions") or []),
                ]
                continuity["repair_suggestions"] = list({
                    str(item.get("code") or index): item
                    for index, item in enumerate(suggestions)
                    if isinstance(item, dict)
                }.values())
                shot["continuity"] = continuity
        manifest["continuity_health"] = status.get("summary") or {}
        manifest["continuity_status"] = status.get("status") or "current"
        return manifest

    def preview_keyframes(
        self,
        session_id: str,
        target: Optional[str] = None,
        scene_index: Optional[int] = None,
        shot_index: Optional[int] = None,
        force: bool = False,
    ) -> dict:
        payload = {"session_id": session_id}
        if scene_index is not None:
            payload["scene_index"] = scene_index
        if shot_index is not None:
            payload["shot_index"] = shot_index
        if force:
            payload["force"] = True
        if self._durable:
            return self._submit_durable(
                self._PREVIEW_KEYFRAMES,
                payload,
                key=session_id,
                target=target,
            )
        prog: list = []

        async def work():
            if scene_index is None and shot_index is None and not force:
                return await self.engine.preview_keyframes(
                    session_id, progress=self._sink(prog))
            return await self.engine.preview_keyframes(
                session_id,
                progress=self._sink(prog),
                scene_index=scene_index,
                shot_index=shot_index,
                force=force,
            )

        return self.runner.submit(
            work, key=session_id, on_done=self._notify_cb(target), progress=prog)

    def job(self, job_id: str) -> Optional[dict]:
        return self.runner.get(job_id)

    def cancel_job(self, job_id: str) -> dict:
        if not hasattr(self.runner, "request_cancel"):
            return {"ok": False, "error": "cancel_not_supported"}
        return {"ok": True, "job": self.runner.request_cancel(job_id)}

    def continue_cancelled(self, session_id: str, target: Optional[str] = None) -> dict:
        """Repeat the exact project operation most recently cancelled by the user."""
        record = self.runner.last_job(session_id)
        if not record or str(record.get("internal_state") or "") != "canceled":
            return {"ok": False, "error": "no_cancelled_job", "note": "没有可继续的已终止任务。"}
        get_spec = getattr(self.runner, "last_job_spec", None)
        spec = get_spec(session_id) if callable(get_spec) else None
        if spec is None:
            return {"ok": False, "error": "resume_not_supported", "note": "当前任务无法从终止位置继续。"}

        payload = dict(spec.payload or {})
        job_type = str(spec.job_type or "")
        if job_type == self._APPROVE:
            return self.approve(session_id, target=target)
        if job_type == self._REVISE:
            return self.revise(session_id, str(payload.get("instruction") or ""), target=target)
        if job_type == self._RESUME:
            return self.resume(session_id, target=target)
        if job_type == self._PREVIEW_KEYFRAMES:
            return self.preview_keyframes(
                session_id,
                target=target,
                scene_index=payload.get("scene_index"),
                shot_index=payload.get("shot_index"),
                force=bool(payload.get("force", False)),
            )
        if job_type == self._REGENERATE_SHOT:
            description = {
                key: payload.get(key)
                for key in (
                    "visual_desc", "audio_desc", "screen_text", "screen_text_pos",
                    "duration_sec", "director_desc", "beats", "visual_style", "avoid",
                )
                if payload.get(key) is not None
            }
            return self.regenerate_shot(
                session_id,
                payload.get("shot_idx"),
                scene_index=payload.get("scene_index"),
                keep_description=bool(payload.get("keep_description", True)),
                description=description or None,
                reason=str(payload.get("reason") or "user_requested"),
                dimensions=list(payload.get("dimensions") or []),
                locked_dimensions=list(payload.get("locked_dimensions") or []),
                target=target,
            )
        if job_type == self._REGENERATE_SHOTS:
            return self.regenerate_shots(
                session_id,
                list(payload.get("requested_shots") or payload.get("shots") or []),
                reason=str(payload.get("reason") or "user_requested"),
                dimensions=list(payload.get("dimensions") or []),
                locked_dimensions=list(payload.get("locked_dimensions") or []),
                target=target,
            )
        if job_type == self._RENDER_EDIT_PLAN:
            return self.render_edit_plan(
                session_id, payload.get("plan") or {}, target=target
            )
        if job_type == self._RESET_EDIT_PLAN:
            return self.reset_edit_plan(session_id, target=target)
        return {
            "ok": False,
            "error": "resume_not_supported",
            "note": "这个已终止任务暂不支持继续，请从当前阶段重新执行。",
        }

    def _submit_durable(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
        target: str | None = None,
    ) -> dict:
        stored_payload = dict(payload)
        if target:
            stored_payload["_notification_target"] = target
        digest_source = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        idempotency_key = f"{job_type}:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
        spec = JobSpec(
            job_type=job_type,
            payload=stored_payload,
            project_id=key,
            entity_type=("project" if key else None),
            entity_id=key,
            concurrency_key=key,
            idempotency_key=idempotency_key,
        )
        return self.runner.submit_job(spec, key=key)

    async def _handle_start_topic(self, spec: JobSpec, context) -> dict:
        payload = spec.payload
        kwargs = {
            "user_requirement": str(payload.get("user_requirement") or ""),
            "style": str(payload.get("style") or ""),
            "domain": str(payload.get("domain") or ""),
            "character_asset_ids": list(payload.get("character_asset_ids") or []),
            "prop_asset_ids": list(payload.get("prop_asset_ids") or []),
            "scene_asset_ids": list(payload.get("scene_asset_ids") or []),
            "mode": str(payload.get("mode") or "idea"),
            "script": str(payload.get("script") or ""),
            "target_language": payload.get("target_language"),
            "aspect_ratio": payload.get("aspect_ratio"),
            "overrides": dict(payload.get("overrides") or {}),
            "quality_tier": str(payload.get("quality_tier") or "balanced"),
            "progress": self._sink(context),
        }
        lora_ids = list(payload.get("lora_ids") or [])
        if lora_ids:
            kwargs["lora_ids"] = lora_ids
        if payload.get("continuity_source_session_id"):
            kwargs["continuity_source_session_id"] = str(payload["continuity_source_session_id"])
        if payload.get("series_id"):
            kwargs.update({
                "series_id": str(payload["series_id"]),
                "episode_number": int(payload.get("episode_number") or 1),
                "episode_title": str(payload.get("episode_title") or ""),
                "episode_outline": str(payload.get("episode_outline") or ""),
                "previous_episode_id": str(payload.get("previous_episode_id") or ""),
                "series_context": dict(payload.get("series_context") or {}),
            })
        return await self.engine.start_topic(str(payload.get("idea") or ""), **kwargs)

    async def _handle_approve(self, spec: JobSpec, context) -> dict:
        return await self.engine.approve(str(spec.payload["session_id"]), progress=self._sink(context))

    async def _handle_revise(self, spec: JobSpec, context) -> dict:
        return await self.engine.revise(
            str(spec.payload["session_id"]),
            str(spec.payload.get("instruction") or ""),
            progress=self._sink(context),
        )

    async def _handle_resume(self, spec: JobSpec, context) -> dict:
        return await self.engine.resume_generation(
            str(spec.payload["session_id"]),
            progress=self._sink(context),
        )

    async def _handle_regenerate_shot(self, spec: JobSpec, context) -> dict:
        args = {key: value for key, value in spec.payload.items() if not key.startswith("_")}
        from agent_runtime.tools import ToolRuntimeContext

        def forward(payload: dict[str, Any]) -> None:
            if context.cancel_requested():
                raise RuntimeError("job cancellation requested")
            progress = dict(payload.get("progress") or {})
            context.event(
                str(progress.get("stage") or "running"),
                str(progress.get("message") or "Shot regeneration in progress"),
                dict(progress.get("metadata") or {}),
            )

        runtime = ToolRuntimeContext(
            requested_name="sceneforge_regenerate_shot",
            canonical_name="sceneforge_regenerate_shot",
            progress_callback=forward,
        )
        result = await self.adapters.sceneforge_regenerate_shot(args, runtime)
        if result.ok:
            if hasattr(self.engine, "rebuild_after_shot_regeneration"):
                rebuilt = await self.engine.rebuild_after_shot_regeneration(
                    str(args["session_id"]), progress=self._sink(context)
                )
                if result.metadata is not None:
                    result.metadata.update(rebuilt)
            self._record_regenerated_versions(args, result.metadata or {})
        return {"ok": result.ok, **(result.metadata or {})}

    async def _handle_regenerate_shots(self, spec: JobSpec, context) -> dict:
        payload = {
            key: value for key, value in spec.payload.items() if not key.startswith("_")
        }
        shots = list(payload.get("shots") or [])
        results = []
        from agent_runtime.tools import ToolRuntimeContext

        for index, shot in enumerate(shots):
            args = self._batch_shot_args(payload, shot)

            def forward(event: dict[str, Any], batch_index=index) -> None:
                if context.cancel_requested():
                    raise RuntimeError("job cancellation requested")
                progress = dict(event.get("progress") or {})
                metadata = dict(progress.get("metadata") or {})
                metadata.update({
                    "batch_index": batch_index + 1,
                    "batch_count": len(shots),
                })
                context.event(
                    str(progress.get("stage") or "running"),
                    str(progress.get("message") or "Batch shot regeneration in progress"),
                    metadata,
                )

            context.event(
                "batch_shot_start",
                f"Regenerating batch shot {index + 1}/{len(shots)}",
                {
                    "batch_index": index + 1,
                    "batch_count": len(shots),
                    "shot_idx": args["shot_idx"],
                    "scene_idx": args.get("scene_index"),
                },
            )
            runtime = ToolRuntimeContext(
                requested_name="sceneforge_regenerate_shot",
                canonical_name="sceneforge_regenerate_shot",
                progress_callback=forward,
            )
            result = await self.adapters.sceneforge_regenerate_shot(args, runtime)
            metadata = dict(result.metadata or {})
            results.append({"ok": result.ok, **metadata})
            if not result.ok:
                return {
                    "ok": False,
                    "results": results,
                    **self._batch_result_summary(payload),
                }
            self._record_regenerated_versions(args, metadata)

        rebuilt = {}
        if hasattr(self.engine, "rebuild_after_shot_regeneration"):
            rebuilt = await self.engine.rebuild_after_shot_regeneration(
                str(payload["session_id"]), progress=self._sink(context)
            )
        self._record_rework_savings(payload)
        return {
            "ok": True,
            "results": results,
            **self._batch_result_summary(payload),
            **(rebuilt or {}),
        }

    async def _handle_preview_keyframes(self, spec: JobSpec, context) -> dict:
        if (
            spec.payload.get("scene_index") is None
            and spec.payload.get("shot_index") is None
            and not spec.payload.get("force", False)
        ):
            return await self.engine.preview_keyframes(
                str(spec.payload["session_id"]), progress=self._sink(context))
        return await self.engine.preview_keyframes(
            str(spec.payload["session_id"]),
            progress=self._sink(context),
            scene_index=spec.payload.get("scene_index"),
            shot_index=spec.payload.get("shot_index"),
            force=bool(spec.payload.get("force", False)),
        )

    async def _handle_render_edit_plan(self, spec: JobSpec, context) -> dict:
        context.event("timeline_render_start", "Rendering edited timeline", {
            "clip_count": len((spec.payload.get("plan") or {}).get("clips") or [])
        })
        result = await asyncio.to_thread(
            self._execute_timeline_render,
            {key: value for key, value in spec.payload.items() if not key.startswith("_")},
        )
        context.event("timeline_render_done", "Edited timeline is ready", {
            "clip_count": result.get("clip_count"),
        })
        return result

    async def _handle_reset_edit_plan(self, spec: JobSpec, context) -> dict:
        context.event("timeline_reset_start", "Restoring original final video", {})
        result = await asyncio.to_thread(
            self._execute_timeline_reset,
            {key: value for key, value in spec.payload.items() if not key.startswith("_")},
        )
        context.event("timeline_reset_done", "Original final video restored", {})
        return result

    def _execute_timeline_render(self, payload: dict) -> dict:
        session_id = str(payload["session_id"])
        result = self._timeline_editor(session_id).render(payload.get("plan") or {})
        self._record_timeline_decision(session_id, "timeline_rendered", result)
        return result

    def _execute_timeline_reset(self, payload: dict) -> dict:
        session_id = str(payload["session_id"])
        result = self._timeline_editor(session_id).reset()
        self._record_timeline_decision(session_id, "timeline_reset", result)
        return result

    def _timeline_editor(self, session_id: str):
        from services.timeline_editor import TimelineEditService

        session_index = getattr(self.engine, "session_index", None)
        if session_index is None:
            session_index = getattr(self.engine, "index", None)
        if session_index is None:
            raise ValueError("session storage is unavailable")
        return TimelineEditService(session_index.working_dir(session_id))

    def _subtitle_timeline(self, session_id: str):
        from services.subtitle_timeline import SubtitleTimelineService

        session_index = getattr(self.engine, "session_index", None)
        if session_index is None:
            session_index = getattr(self.engine, "index", None)
        if session_index is None:
            raise ValueError("session storage is unavailable")
        return SubtitleTimelineService(session_index.working_dir(session_id))

    def _record_timeline_decision(
        self, session_id: str, event_type: str, result: dict
    ) -> None:
        from services.production_metrics import append_decision

        session_index = getattr(self.engine, "session_index", None)
        if session_index is None:
            session_index = getattr(self.engine, "index", None)
        if session_index is None:
            return
        append_decision(
            session_index.working_dir(session_id),
            event_type,
            reason="final_timeline_edit",
            metadata={
                "clip_count": result.get("clip_count"),
                "output_duration": result.get("output_duration"),
                "archive_path": result.get("archive_path"),
            },
        )

    def _record_regenerated_versions(self, args: dict, metadata: dict) -> None:
        versions = getattr(self.engine, "artifact_versions", None)
        if versions is None:
            return
        try:
            session_id = str(args["session_id"])
            shot_index = int(args["shot_idx"])
            requested_scene = args.get("scene_index", metadata.get("scene_index"))
            requested_scene = None if requested_scene is None else int(requested_scene)
            working_dir = self.engine.session_index.working_dir(session_id)
            render_dir, scene_index = self._render_dir_for_versions(
                working_dir, requested_scene)
            if render_dir is None:
                return

            affected = self._affected_shot_indexes(render_dir, shot_index)
            description_keys = {
                "visual_desc", "audio_desc", "screen_text", "screen_text_pos",
                "duration_sec", "director_desc", "beats", "visual_style", "avoid",
            }
            if description_keys.intersection(args):
                storyboard_path = render_dir / "storyboard.json"
                storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
                if isinstance(storyboard, list) and 0 <= shot_index < len(storyboard):
                    versions.record_json_item(
                        session_id,
                        scene_index,
                        shot_index,
                        storyboard[shot_index],
                        live_path=storyboard_path,
                        input_values={"shot": storyboard[shot_index], "source": "shot_regeneration"},
                    )

            request_inputs = {
                key: value for key, value in args.items()
                if key not in {"session_id", "scene_index", "shot_idx"}
            }
            for affected_index in affected:
                shot_dir = render_dir / "shots" / str(affected_index)
                try:
                    description = json.loads(
                        (shot_dir / "shot_description.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    description = {}
                inputs = {
                    "description": description,
                    "request": request_inputs,
                    "dependency_root_shot": shot_index,
                }
                first_frame = shot_dir / "first_frame.png"
                if first_frame.is_file():
                    versions.record_file(
                        session_id,
                        scene_index,
                        affected_index,
                        "keyframe",
                        first_frame,
                        input_values=inputs,
                        metadata={"source": "shot_regeneration"},
                    )
                video = shot_dir / "video.mp4"
                if video.is_file():
                    versions.record_file(
                        session_id,
                        scene_index,
                        affected_index,
                        "video",
                        video,
                        input_values={**inputs, "keyframe_sha256": self._sha256(first_frame)},
                        metadata={"source": "shot_regeneration"},
                    )
        except Exception:
            logging.exception("Failed to record regenerated artifact versions")

    @classmethod
    def _normalize_locked_dimensions(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            value = [value] if value else []
        return sorted({
            str(item).strip().lower()
            for item in value
            if str(item).strip().lower() in cls._LOCK_DIMENSIONS
        })

    @staticmethod
    def _batch_shot_args(payload: dict, shot: dict) -> dict:
        return {
            "session_id": str(payload["session_id"]),
            "scene_index": int(shot["scene_index"]),
            "shot_idx": int(shot["shot_idx"]),
            "keep_description": True,
            "reason": str(payload.get("reason") or "user_requested"),
            "dimensions": list(payload.get("dimensions") or []),
            "locked_dimensions": list(payload.get("locked_dimensions") or []),
        }

    @staticmethod
    def _batch_result_summary(payload: dict) -> dict:
        return {
            "requested_shots": list(payload.get("requested_shots") or []),
            "execution_roots": list(payload.get("shots") or []),
            "affected_shots": list(payload.get("affected_shots") or []),
            "requested_count": len(payload.get("requested_shots") or []),
            "execution_count": len(payload.get("shots") or []),
            "affected_count": len(payload.get("affected_shots") or []),
            "locked_dimensions": list(payload.get("locked_dimensions") or []),
            "savings_estimate": dict(payload.get("savings_estimate") or {}),
        }

    def _record_rework_savings(self, payload: dict) -> None:
        savings = dict(payload.get("savings_estimate") or {})
        if not savings:
            return
        from services.production_metrics import append_decision

        session_index = getattr(self.engine, "session_index", None)
        if session_index is None:
            session_index = getattr(self.engine, "index", None)
        if session_index is None:
            return
        append_decision(
            session_index.working_dir(str(payload["session_id"])),
            "local_rework_completed",
            reason=str(payload.get("reason") or "user_requested"),
            dimensions=list(payload.get("dimensions") or []),
            metadata={
                "requested_shots": list(payload.get("requested_shots") or []),
                "execution_roots": list(payload.get("shots") or []),
                "affected_shots": list(payload.get("affected_shots") or []),
                "locked_dimensions": list(payload.get("locked_dimensions") or []),
                "savings_estimate": savings,
            },
        )

    def _regeneration_cost_estimate(self, session_id: str, affected_count: int) -> dict:
        session_getter = getattr(self.engine.session_index, "get", None)
        session = session_getter(session_id) if callable(session_getter) else None
        session = session or {"session_id": session_id}
        route_resolver = getattr(self.engine, "selected_video_route", None)
        route = route_resolver(session) if callable(route_resolver) else {}
        config_resolver = getattr(self.engine, "_effective_config", None)
        config = config_resolver(session) if callable(config_resolver) else {}
        generation = config.get("generation") or {}
        candidate_count = max(1, int(generation.get("video_candidates", 1) or 1))
        retry_limit = max(1, int(generation.get("render_retries", 1) or 1))
        raw_unit_cost = route.get("estimated_cost")
        try:
            unit_cost = max(0.0, float(raw_unit_cost)) if raw_unit_cost is not None else None
        except (TypeError, ValueError):
            unit_cost = None
        currency = route.get("currency") or (config.get("cost") or {}).get("currency")
        currency = {"¥": "CNY", "￥": "CNY", "$": "USD"}.get(currency, currency)
        base_requests = max(0, int(affected_count)) * candidate_count
        lower = round(unit_cost * base_requests, 4) if unit_cost is not None else None
        upper = round(lower * retry_limit, 4) if lower is not None else None
        return {
            "available": unit_cost is not None,
            "basis": "selected_profile_estimate" if unit_cost is not None else "unavailable",
            "currency": currency,
            "estimated_unit_cost": unit_cost,
            "estimated_lower_bound": lower,
            "estimated_upper_bound": upper,
            "affected_count": max(0, int(affected_count)),
            "candidate_count": candidate_count,
            "retry_limit": retry_limit,
            "profile_id": route.get("profile_id"),
            "provider_id": route.get("provider_id") or route.get("provider"),
            "model_id": route.get("model_id") or route.get("model"),
            "note": (
                "下限按每镜候选数估算，上限包含配置的重试次数；供应商实际账单可能不同。"
                if unit_cost is not None
                else "当前视频模型未配置单次预估价格，提交前无法给出金额。"
            ),
        }

    def _regeneration_savings_estimate(
        self, session_id: str, affected_count: int, rework_cost: dict
    ) -> dict:
        metrics = self.production_metrics(session_id)
        summary = metrics.get("summary") or {}
        total_shots = max(
            int(summary.get("total_shots") or 0), max(0, int(affected_count))
        )
        avoided_shots = max(0, total_shots - max(0, int(affected_count)))
        full_cost = self._regeneration_cost_estimate(session_id, total_shots)
        saved_cost = self._regeneration_cost_estimate(session_id, avoided_shots)
        mean_seconds = summary.get("mean_generation_seconds")
        try:
            mean_seconds = max(0.0, float(mean_seconds))
        except (TypeError, ValueError):
            mean_seconds = None
        savings_rate = round(avoided_shots / total_shots, 4) if total_shots else 0.0
        return {
            "comparison": "full_project_rerender",
            "full_rerender_shot_count": total_shots,
            "local_rework_shot_count": max(0, int(affected_count)),
            "avoided_shot_count": avoided_shots,
            "shot_savings_rate": savings_rate,
            "estimated_cost_savings_rate": savings_rate if rework_cost.get("available") else None,
            "estimated_cost_saved_lower_bound": saved_cost.get("estimated_lower_bound"),
            "estimated_cost_saved_upper_bound": saved_cost.get("estimated_upper_bound"),
            "estimated_generation_seconds_saved": (
                round(mean_seconds * avoided_shots, 3)
                if mean_seconds is not None else None
            ),
            "full_rerender_cost_estimate": full_cost,
            "note": "按当前 Profile、候选数和重试配置与整片全部镜头重做比较。耗时节省使用本项目历史单镜平均生成时间估算。",
        }

    def _artifact_version_for_session(self, session_id: str, artifact_id: str):
        versions = getattr(self.engine, "artifact_versions", None)
        if versions is None:
            raise ValueError("artifact versioning is unavailable")
        version = versions.repository.get_version(str(artifact_id or "").strip())
        if version is None or version.project_id != session_id:
            raise ValueError("artifact version not found")
        return version

    def _normalize_shot_requests(self, session_id: str, shots: Any) -> list[dict]:
        if not isinstance(shots, list) or not shots:
            raise ValueError("shots must be a non-empty list")
        if len(shots) > 20:
            raise ValueError("a batch can contain at most 20 shots")
        working_dir = Path(self.engine.session_index.working_dir(session_id))
        normalized = []
        seen = set()
        for raw in shots:
            if not isinstance(raw, dict):
                raise ValueError("each shot must contain scene_index and shot_idx")
            try:
                shot_index = int(raw.get("shot_idx"))
                scene_index = raw.get("scene_index")
                scene_index = None if scene_index is None else int(scene_index)
            except (TypeError, ValueError):
                raise ValueError("scene_index and shot_idx must be integers") from None
            if shot_index < 0 or (scene_index is not None and scene_index < 0):
                raise ValueError("scene_index and shot_idx must be non-negative")
            render_dir, resolved_scene = self._render_dir_for_versions(
                working_dir, scene_index
            )
            if render_dir is None:
                raise ValueError("render artifacts are not available")
            key = (int(resolved_scene), shot_index)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"scene_index": key[0], "shot_idx": key[1]})
        if not normalized:
            raise ValueError("shots must contain at least one unique shot")
        return sorted(normalized, key=lambda item: (item["scene_index"], item["shot_idx"]))

    @staticmethod
    def _render_dir_for_versions(
        working_dir: Path, scene_index: int | None
    ) -> tuple[Path | None, int]:
        script_dir = working_dir / "script2video"
        if (script_dir / "camera_tree.json").is_file():
            return script_dir, 0
        idea_dir = working_dir / "idea2video"
        if scene_index is not None:
            candidate = idea_dir / f"scene_{scene_index}"
            return (candidate, scene_index) if candidate.is_dir() else (None, scene_index)
        candidates = [
            candidate for candidate in sorted(idea_dir.glob("scene_*"))
            if (candidate / "camera_tree.json").is_file()
        ]
        if len(candidates) != 1:
            return None, 0
        try:
            resolved_index = int(candidates[0].name.split("_", 1)[1])
        except (IndexError, ValueError):
            resolved_index = 0
        return candidates[0], resolved_index

    @staticmethod
    def _affected_shot_indexes(render_dir: Path, shot_index: int) -> list[int]:
        try:
            from interfaces import Camera
            from pipelines.script2video_pipeline import Script2VideoPipeline

            raw = json.loads((render_dir / "camera_tree.json").read_text(encoding="utf-8"))
            tree = [Camera.model_validate(item) for item in raw]
            return Script2VideoPipeline._collect_dependent_shots(shot_index, tree)
        except Exception:
            return [shot_index]

    @staticmethod
    def _sha256(path: Path) -> str:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _durable_terminal(self, job: GenerationJob) -> None:
        target = str(job.spec.payload.get("_notification_target") or "")
        if not target or self.notifier is None:
            return
        record = self.runner.get(job.job_id)
        if record is None:
            return
        try:
            asyncio.run(self.notifier(self.format_result(record), target))
        except Exception:
            pass

    # ----- notification -------------------------------------------------

    def _notify_cb(self, target: Optional[str]):
        if not target or self.notifier is None:
            return None

        def cb(record: dict) -> None:
            try:
                asyncio.run(self.notifier(self.format_result(record), target))
            except Exception:
                pass
        return cb

    @staticmethod
    def format_result(record: dict) -> str:
        if record.get("state") == "failed":
            return f"⚠️ 生成失败：{record.get('error')}（修复后可点击页面右上角的阶段按钮重试）"
        result = record.get("result") or {}
        if not result.get("ok", True):
            return f"⚠️ {result.get('note') or result.get('error') or '处理未成功'}"
        stage = result.get("stage", "")
        summary = result.get("summary", "")
        if stage == "completed":
            return f"🎬 已发布完成。{summary}".strip()
        if stage:
            return f"【{stage} 已就绪】\n{summary}\n请回复：通过 / 修改：<意见>".strip()
        return summary or "✅ 处理完成"
