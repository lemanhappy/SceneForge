"""Central logging configuration.

The codebase emits ``logging.warning``/``info`` in many places (retry, atomic
writes, ffmpeg mux, TTS, ...), but without a configured root handler those never
surface. ``configure_logging`` installs a consistent stderr handler (and an
optional rotating file) so server/console runs have visible, persistable logs.
Idempotent: safe to call once per entry point.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = False


def configure_logging(level: str = "INFO", logfile: Optional[str] = None,
                      max_bytes: int = 5_000_000, backups: int = 3) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if logfile:
        try:
            fh = RotatingFileHandler(logfile, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            fh.setFormatter(formatter)
            root.addHandler(fh)
        except OSError:
            pass  # never let logging setup break startup

    # quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
