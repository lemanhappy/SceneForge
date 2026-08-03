from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from domain.jobs import GenerationJob, JobState
from infrastructure.sqlite.job_queue import WorkerLeaseLostError
from repositories.job_queue import JobQueue

from .job_handlers import JobContext, JobHandlerRegistry
from .remote_recovery import RemoteRecoveryAction, RemoteRecoveryResult


class LocalWorker:
    """Claims durable jobs and dispatches them through registered async handlers."""

    def __init__(
        self,
        queue: JobQueue,
        registry: JobHandlerRegistry,
        *,
        concurrency: int = 1,
        on_terminal: Callable[[GenerationJob], None] | None = None,
        poll_interval: float = 0.25,
        lease_ttl: float = 30.0,
        remote_reconciler: Callable[[GenerationJob, JobContext], Awaitable[RemoteRecoveryResult]] | None = None,
        remote_poll_interval: float = 5.0,
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.concurrency = max(1, int(concurrency))
        self.on_terminal = on_terminal
        self.poll_interval = max(0.05, float(poll_interval))
        self.lease_ttl = max(0.5, float(lease_ttl))
        self.remote_reconciler = remote_reconciler
        self.remote_poll_interval = max(0.25, float(remote_poll_interval))
        self.worker_group_id = f"worker_{uuid4().hex[:12]}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._heartbeat_thread: threading.Thread | None = None
        self._remote_thread: threading.Thread | None = None
        self._supervisor_thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._owns_lease = False

    def start(self) -> None:
        with self._start_lock:
            if self._threads or (
                self._supervisor_thread is not None and self._supervisor_thread.is_alive()
            ):
                return
            if self.queue.acquire_worker_lease(
                self.worker_group_id,
                ttl_seconds=self.lease_ttl,
            ):
                self._activate_locked()
                return
            self._supervisor_thread = threading.Thread(
                target=self._supervise,
                daemon=True,
                name="sceneforge-worker-supervisor",
            )
            self._supervisor_thread.start()

    def _supervise(self) -> None:
        retry_interval = min(1.0, max(0.1, self.lease_ttl / 3))
        while not self._stop_event.is_set():
            if self.queue.acquire_worker_lease(
                self.worker_group_id,
                ttl_seconds=self.lease_ttl,
            ):
                self._activate()
                return
            self._stop_event.wait(retry_interval)

    def _activate(self) -> None:
        with self._start_lock:
            self._activate_locked()

    def _activate_locked(self) -> None:
        if self._stop_event.is_set():
            self.queue.release_worker_lease(self.worker_group_id)
            return
        self._owns_lease = True
        self.queue.recover_interrupted()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            daemon=True,
            name="sceneforge-worker-heartbeat",
        )
        self._heartbeat_thread.start()
        if self.remote_reconciler is not None:
            self._remote_thread = threading.Thread(
                target=self._recover_remote_tasks,
                daemon=True,
                name="sceneforge-remote-recovery",
            )
            self._remote_thread.start()
        for index in range(self.concurrency):
            thread = threading.Thread(
                target=self._run,
                args=(f"{self.worker_group_id}_{index + 1}",),
                daemon=True,
                name=f"sceneforge-worker-{index + 1}",
            )
            self._threads.append(thread)
            thread.start()

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._supervisor_thread is not None:
            self._supervisor_thread.join(timeout=max(0.0, timeout))
            if not self._supervisor_thread.is_alive():
                self._supervisor_thread = None
        for thread in list(self._threads):
            thread.join(timeout=max(0.0, timeout))
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=max(0.0, timeout))
            if not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = None
        if self._remote_thread is not None:
            self._remote_thread.join(timeout=max(0.0, timeout))
            if not self._remote_thread.is_alive():
                self._remote_thread = None
        if self._owns_lease and not self._threads and self._remote_thread is None:
            self.queue.release_worker_lease(self.worker_group_id)
            self._owns_lease = False

    def _run(self, worker_id: str) -> None:
        while not self._stop_event.is_set():
            try:
                job = self.queue.claim(
                    worker_id,
                    lease_owner_id=self.worker_group_id,
                )
            except WorkerLeaseLostError:
                self._stop_event.set()
                self._wake_event.set()
                return
            if job is None:
                self._wake_event.wait(self.poll_interval)
                self._wake_event.clear()
                continue
            self._execute(job)

    def _recover_remote_tasks(self) -> None:
        while not self._stop_event.is_set():
            try:
                jobs = self.queue.list_remote_active(limit=50)
            except Exception:
                self._stop_event.wait(self.remote_poll_interval)
                continue
            for job in jobs:
                if self._stop_event.is_set():
                    return
                if str(job.worker_id or "").startswith(self.worker_group_id):
                    continue
                context = JobContext(self.queue, job.job_id, self.worker_group_id)
                try:
                    result = asyncio.run(self.remote_reconciler(job, context))
                    self._apply_remote_recovery(job, result)
                except WorkerLeaseLostError:
                    self._stop_event.set()
                    self._wake_event.set()
                    return
                except Exception as exc:
                    try:
                        context.event(
                            "remote_recovery_error",
                            "Remote task reconciliation failed",
                            {"status": "query_error", "error": str(exc)},
                        )
                    except WorkerLeaseLostError:
                        self._stop_event.set()
                        self._wake_event.set()
                        return
            self._stop_event.wait(self.remote_poll_interval)

    def _apply_remote_recovery(
        self,
        original: GenerationJob,
        result: RemoteRecoveryResult,
    ) -> None:
        if result.action is RemoteRecoveryAction.PENDING:
            return
        current = self.queue.get(original.job_id)
        if current is None or current.state in {
            JobState.SUCCEEDED,
            JobState.CANCELED,
            JobState.FAILED,
            JobState.INTERRUPTED,
        }:
            return
        if current.state is JobState.CANCEL_REQUESTED:
            terminal = self.queue.transition(
                current.job_id,
                JobState.CANCELED,
                lease_owner_id=self.worker_group_id,
            )
            self._notify_terminal(terminal)
            return
        if result.action is RemoteRecoveryAction.RETRY_WORKFLOW:
            try:
                self.queue.transition(
                    current.job_id,
                    JobState.RETRY_WAIT,
                    error_code=("RemoteTaskFailed" if result.error else None),
                    error_message=result.error,
                    lease_owner_id=self.worker_group_id,
                )
            except ValueError:
                terminal = self.queue.transition(
                    current.job_id,
                    JobState.FAILED,
                    error_code="RetryExhausted",
                    error_message=result.error or "remote task recovered but workflow retries are exhausted",
                    lease_owner_id=self.worker_group_id,
                )
                self._notify_terminal(terminal)
            else:
                self._wake_event.set()
            return
        terminal = self.queue.transition(
            current.job_id,
            JobState.FAILED,
            error_code="RemoteRecoveryFailed",
            error_message=result.error or "remote task could not be recovered",
            lease_owner_id=self.worker_group_id,
        )
        self._notify_terminal(terminal)

    def _notify_terminal(self, terminal: GenerationJob) -> None:
        if self.on_terminal is not None:
            try:
                self.on_terminal(terminal)
            except Exception:
                pass

    def _heartbeat(self) -> None:
        interval = max(0.1, self.lease_ttl / 3)
        while not self._stop_event.wait(interval):
            try:
                renewed = self.queue.renew_worker_lease(
                    self.worker_group_id,
                    ttl_seconds=self.lease_ttl,
                )
            except Exception:
                renewed = False
            if not renewed:
                self._stop_event.set()
                self._wake_event.set()
                return

    def _execute(self, job: GenerationJob) -> None:
        context = JobContext(self.queue, job.job_id, self.worker_group_id)
        terminal: GenerationJob
        try:
            result = asyncio.run(self.registry.dispatch(job.spec, context))
            if not isinstance(result, dict):
                raise TypeError(f"job handler {job.spec.job_type!r} must return a dict")
            project_id = result.get("session_id") or result.get("project_id")
            if project_id and job.spec.project_id is None:
                self.queue.attach_project(
                    job.job_id,
                    str(project_id),
                    lease_owner_id=self.worker_group_id,
                )
            current = self.queue.get(job.job_id)
            if current is not None and current.state is JobState.CANCEL_REQUESTED:
                terminal = self.queue.transition(
                    job.job_id,
                    JobState.CANCELED,
                    lease_owner_id=self.worker_group_id,
                )
            else:
                terminal = self.queue.transition(
                    job.job_id,
                    JobState.SUCCEEDED,
                    result=result,
                    lease_owner_id=self.worker_group_id,
                )
        except WorkerLeaseLostError:
            return
        except Exception as exc:
            current = self.queue.get(job.job_id)
            try:
                if current is not None and current.state is JobState.CANCEL_REQUESTED:
                    terminal = self.queue.transition(
                        job.job_id,
                        JobState.CANCELED,
                        lease_owner_id=self.worker_group_id,
                    )
                elif current is not None and current.state not in {
                    JobState.SUCCEEDED,
                    JobState.CANCELED,
                    JobState.FAILED,
                    JobState.INTERRUPTED,
                }:
                    terminal = self.queue.transition(
                        job.job_id,
                        JobState.FAILED,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                        lease_owner_id=self.worker_group_id,
                    )
                else:
                    terminal = current or job
            except WorkerLeaseLostError:
                return
        self._notify_terminal(terminal)
