from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, Optional, Set, Tuple


class Authorizer:
    """Gate inbound triggers by (channel, user_id) allowlist (design §19).

    When no ``authorized_sources`` are configured the gate is disabled (allow
    all) so console/local usage is unaffected; once any source is listed, only
    listed (channel, user_id) pairs are authorized.
    """

    def __init__(self, allowed: Optional[Set[Tuple[Optional[str], Optional[str]]]] = None):
        self.allowed: Set[Tuple[Optional[str], Optional[str]]] = set(allowed or set())
        self.enabled = bool(self.allowed)

    @classmethod
    def from_config(cls, config: dict) -> "Authorizer":
        sources = ((config or {}).get("security") or {}).get("authorized_sources") or []
        return cls({(s.get("channel"), s.get("user_id")) for s in sources})

    def is_authorized(self, channel: Optional[str], user_id: Optional[str]) -> bool:
        if not self.enabled:
            return True
        return (channel, user_id) in self.allowed


class InboundRateLimiter:
    """Non-blocking per-user daily cap for inbound triggers (reject when over).

    Distinct from utils.RateLimiter, which blocks API calls until allowed; an
    inbound trigger should be refused, not queued. ``day_provider`` is injectable
    for testing.
    """

    def __init__(self, max_requests_per_day: Optional[int] = None,
                 day_provider: Optional[Callable[[], str]] = None):
        self.max_requests_per_day = max_requests_per_day
        self._day_provider = day_provider or (lambda: datetime.now().strftime("%Y-%m-%d"))
        self._counts: Dict[Tuple[str, str], int] = {}

    @classmethod
    def from_config(cls, config: dict, day_provider: Optional[Callable[[], str]] = None) -> "InboundRateLimiter":
        per_user = ((config or {}).get("rate_limits") or {}).get("per_user") or {}
        return cls(max_requests_per_day=per_user.get("max_requests_per_day"), day_provider=day_provider)

    def allow(self, user_id: Optional[str]) -> bool:
        if not self.max_requests_per_day or self.max_requests_per_day <= 0:
            return True
        key = (self._day_provider(), str(user_id or ""))
        count = self._counts.get(key, 0)
        if count >= self.max_requests_per_day:
            return False
        self._counts[key] = count + 1
        return True
