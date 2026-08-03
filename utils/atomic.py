"""Atomic, lock-guarded text file writes for config/registry persistence.

The web server is multi-threaded (ThreadingHTTPServer) and several services now
write the same pipeline configs (BGM, voice, model config). A naive
``path.write_text`` can interleave writes and leave a half-written YAML, which
corrupts the whole config. ``atomic_write_text`` writes to a temp file then
``os.replace`` (atomic on the same filesystem); ``file_lock`` lets a caller wrap
a full read-modify-write so concurrent updates to one file don't lose each other.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def file_lock(path):
    """Serialize a read-modify-write cycle on ``path`` within this process."""
    lock = _lock_for(path)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace), under the
    path's lock so concurrent writers never observe a partial file."""
    path = Path(path)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix or ".tmp")
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(text)
            # os.replace is atomic, but on Windows it fails with PermissionError
            # when a reader currently holds the destination open; retry briefly.
            for attempt in range(20):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.02)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
