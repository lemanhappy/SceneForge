"""Background job runner for long async operations (script/storyboard/video).

The web server and message channels must return quickly (Feishu requires a ~3s
ack), but a generation stage can take minutes. JobRunner runs an async coroutine
in a daemon thread (its own event loop) and tracks status, so callers ack
immediately and observe completion via status / an on_done callback.

Single-flight: a job submitted with a ``key`` (e.g. session_id) is rejected while
another job for that key is still running, preventing double "通过" from running
a stage twice concurrently.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Dict, Optional


class JobRunner:
    def __init__(self, max_concurrent: Optional[int] = None):
        self._jobs: Dict[str, dict] = {}
        self._by_key: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._max_concurrent = max_concurrent
        self._running = 0

    def submit(self, coro_factory: Callable[[], Awaitable[Any]], key: Optional[str] = None,
               on_done: Optional[Callable[[dict], None]] = None,
               progress: Optional[list] = None) -> dict:
        """Run ``coro_factory()`` in the background. Returns a record dict with
        ``accepted`` and ``job_id``; ``accepted`` is False if a job for ``key`` is
        already running (state "busy") or the global concurrency cap is reached
        (state "at_capacity").

        ``progress`` is a shared list the running coroutine appends progress
        entries to; it's stored on the record so ``get()`` exposes live progress
        for polling clients."""
        with self._lock:
            if key and key in self._by_key:
                return {"accepted": False, "state": "busy", "job_id": self._by_key[key], "key": key}
            if self._max_concurrent and self._running >= self._max_concurrent:
                return {"accepted": False, "state": "at_capacity", "running": self._running, "limit": self._max_concurrent}
            self._counter += 1
            job_id = f"job_{self._counter}"
            record = {"job_id": job_id, "key": key, "state": "running", "result": None, "error": None,
                      "accepted": True, "progress": progress if progress is not None else []}
            self._jobs[job_id] = record
            self._running += 1
            if key:
                self._by_key[key] = job_id

        def worker() -> None:
            try:
                result = asyncio.run(coro_factory())
                record["result"] = result
                record["state"] = "done"
            except Exception as exc:  # noqa: BLE001 - report any failure as job state
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["state"] = "failed"
            finally:
                with self._lock:
                    self._running -= 1
                    if key and self._by_key.get(key) == job_id:
                        del self._by_key[key]
                if on_done is not None:
                    try:
                        on_done(record)
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True, name=f"sceneforge-{job_id}").start()
        return dict(record)

    def get(self, job_id: str) -> Optional[dict]:
        rec = self._jobs.get(job_id)
        if not rec:
            return None
        snap = dict(rec)
        # copy the live progress list so callers get a stable snapshot
        snap["progress"] = list(rec.get("progress") or [])
        return snap

    def is_running(self, key: str) -> bool:
        with self._lock:
            return key in self._by_key

    def running_job_id(self, key: str) -> Optional[str]:
        """The in-flight job id for ``key`` (session), or None. Lets the UI
        re-attach to a running job's progress after any re-render."""
        with self._lock:
            return self._by_key.get(key)

    def last_job(self, key: str) -> Optional[dict]:
        """The most recently submitted job for ``key`` (running or finished), or
        None. Lets the UI surface the outcome of the last action — e.g. a budget
        rejection (``result.ok == False``) or a crash — even after a page refresh,
        when the job is no longer in-flight and so isn't being polled."""
        with self._lock:
            jobs = [r for r in self._jobs.values() if r.get("key") == key]
        if not jobs:
            return None

        def _num(rec: dict) -> int:
            try:
                return int(str(rec.get("job_id", "job_0")).rsplit("_", 1)[-1])
            except Exception:
                return 0

        rec = max(jobs, key=_num)
        snap = dict(rec)
        snap["progress"] = list(rec.get("progress") or [])
        return snap

    def list_recent(self, limit: int = 50) -> list:
        """Recent jobs (newest first) as compact records — an in-session task
        history for the UI. Progress lists are summarized to their last entry."""
        with self._lock:
            jobs = list(self._jobs.values())
        out = []
        for rec in jobs[-limit:][::-1]:
            prog = rec.get("progress") or []
            out.append({"job_id": rec.get("job_id"), "key": rec.get("key"), "state": rec.get("state"),
                        "error": rec.get("error"), "steps": len(prog),
                        "last": (prog[-1].get("message") if prog else "")})
        return out
