from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_runtime.config import video_api_key, video_base_url
from domain.jobs import GenerationJob
from tools.remote_video import RemoteVideoProvider, RemoteVideoState


class RemoteRecoveryAction(str, Enum):
    PENDING = "pending"
    RETRY_WORKFLOW = "retry_workflow"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemoteRecoveryResult:
    action: RemoteRecoveryAction
    status: str
    error: str | None = None


class RemoteVideoRecovery:
    """Reconciles one orphaned provider task and restores its clip artifact."""

    def __init__(
        self,
        workspace_root: str | Path,
        session_index: Any,
        *,
        provider_factory: Callable[[GenerationJob], RemoteVideoProvider] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_index = session_index
        self.provider_factory = provider_factory or self._build_provider

    async def __call__(self, job: GenerationJob, context: Any) -> RemoteRecoveryResult:
        if not job.remote_task_id or not job.remote_provider:
            return RemoteRecoveryResult(
                RemoteRecoveryAction.FAILED,
                "missing_context",
                "remote recovery context is incomplete",
            )
        provider = self.provider_factory(job)
        inspection = await provider.inspect_remote_task(
            job.remote_task_id,
            model=job.spec.model,
            metadata=job.remote_metadata,
        )
        context.event(
            "remote_recovery_status",
            f"Remote task status: {inspection.status}",
            {"status": inspection.status},
        )
        if inspection.state is RemoteVideoState.PENDING:
            return RemoteRecoveryResult(RemoteRecoveryAction.PENDING, inspection.status)
        if inspection.state is RemoteVideoState.FAILED:
            return RemoteRecoveryResult(
                RemoteRecoveryAction.RETRY_WORKFLOW,
                inspection.status,
                inspection.error or "remote provider task failed",
            )
        if inspection.output is None:
            return RemoteRecoveryResult(
                RemoteRecoveryAction.FAILED,
                inspection.status,
                "remote provider completed without an output",
            )
        try:
            target = self._artifact_target(job)
        except (KeyError, ValueError) as exc:
            return RemoteRecoveryResult(RemoteRecoveryAction.FAILED, inspection.status, str(exc))

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{job.job_id}.recovering")
        try:
            await asyncio.to_thread(inspection.output.save, str(temporary))
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        context.event(
            "remote_recovery_completed",
            "Recovered remote video output",
            {
                "scene_idx": job.remote_metadata.get("scene_idx"),
                "shot_idx": job.remote_metadata.get("shot_idx"),
            },
        )
        return RemoteRecoveryResult(RemoteRecoveryAction.RETRY_WORKFLOW, inspection.status)

    def _artifact_target(self, job: GenerationJob) -> Path:
        if not job.spec.project_id:
            raise ValueError("remote task is not attached to a project")
        if not job.remote_artifact_path:
            raise ValueError("remote task is missing its artifact path")
        project_root = self.session_index.working_dir(job.spec.project_id).resolve()
        candidate = Path(job.remote_artifact_path)
        target = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
        if target == project_root or project_root not in target.parents:
            raise ValueError("remote artifact path escapes the project working directory")
        return target

    def _build_provider(self, job: GenerationJob) -> RemoteVideoProvider:
        from tools.video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI
        from tools.video_generator_omni_yunwu_api import VideoGeneratorOmniYunwuAPI
        from tools.video_generator_openrouter_api import VideoGeneratorOpenRouterAPI
        from tools.video_generator_veo_yunwu_api import VideoGeneratorVeoYunwuAPI

        api_key = video_api_key(self.workspace_root)
        if not api_key:
            raise RuntimeError("video API key is required to recover a remote task")
        provider = str(job.remote_provider).strip().lower()
        model = str(job.spec.model or job.remote_metadata.get("model") or "").strip()
        base_url = str(
            job.remote_metadata.get("base_url") or video_base_url(self.workspace_root)
        ).rstrip("/")
        if provider == "openrouter":
            return VideoGeneratorOpenRouterAPI(api_key=api_key, model=model, base_url=base_url)
        if provider == "seedance_yunwu":
            return VideoGeneratorDoubaoSeedanceYunwuAPI(
                api_key=api_key,
                t2v_model=model,
                ff2v_model=model,
                flf2v_model=model,
                base_url=base_url,
            )
        if provider == "omni_yunwu":
            return VideoGeneratorOmniYunwuAPI(
                api_key=api_key,
                t2v_model=model,
                i2v_model=model,
                base_url=_yunwu_root(base_url),
            )
        if provider == "veo_yunwu":
            return VideoGeneratorVeoYunwuAPI(
                api_key=api_key,
                t2v_model=model,
                ff2v_model=model,
                flf2v_model=model,
                base_url=_yunwu_root(base_url),
            )
        raise RuntimeError(f"unsupported remote video provider: {provider}")


def _yunwu_root(base_url: str) -> str:
    return base_url[:-3] if base_url.lower().endswith("/v1") else base_url
