"""Content moderation gate (compliance).

Pluggable: the built-in ``keyword`` provider blocks topics/scripts containing
configured sensitive terms (a cheap first line of defence, fully offline). A
cloud provider (e.g. 阿里云内容安全 / 腾讯天御) can be slotted in by implementing
``_cloud_check`` — that needs the vendor's credentials, so it's left as an
explicit extension point rather than a half-wired integration.

Disabled by default (no keywords) so it's a no-op until configured.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class ContentModerator:
    def __init__(self, enabled: bool = False, provider: str = "keyword",
                 keywords: Optional[List[str]] = None):
        self.enabled = bool(enabled)
        self.provider = (provider or "keyword").lower()
        self.keywords = [str(k).lower() for k in (keywords or []) if str(k).strip()]

    @classmethod
    def from_config(cls, config: dict) -> "ContentModerator":
        m = (config or {}).get("moderation") or {}
        return cls(enabled=bool(m.get("enabled")), provider=m.get("provider", "keyword"),
                   keywords=m.get("keywords") or [])

    def check(self, text: str) -> Dict[str, Any]:
        """Return {ok, flagged, reason?, matched?}. ok=True means allowed."""
        if not self.enabled or not text:
            return {"ok": True, "flagged": False}
        if self.provider == "cloud":
            return self._cloud_check(text)
        low = str(text).lower()
        matched = [k for k in self.keywords if k in low]
        if matched:
            return {"ok": False, "flagged": True, "reason": "命中敏感词", "matched": matched}
        return {"ok": True, "flagged": False}

    def _cloud_check(self, text: str) -> Dict[str, Any]:  # pragma: no cover - needs vendor creds
        """Extension point for a cloud moderation API. Until wired with a
        provider + credentials, fail open (allow) but mark it unverified so it's
        never silently assumed safe."""
        return {"ok": True, "flagged": False, "reason": "cloud provider 未接入（缺凭证），未实际审核"}
