from __future__ import annotations

from typing import Any, List

from .base import MessagingChannel


class ConsoleChannel(MessagingChannel):
    """Prints outbound messages and records them; useful for local runs and as
    a test double. Inbound is drained from an in-memory queue."""

    type = "console"

    def __init__(self, echo: bool = True):
        self.echo = echo
        self.sent: List[dict] = []
        self._inbound: List[Any] = []

    async def send_text(self, target: str, text: str) -> dict:
        entry = {"kind": "text", "target": target, "text": text}
        self.sent.append(entry)
        if self.echo:
            print(f"[console -> {target}]\n{text}")
        return entry

    async def send_artifact(self, target: str, artifact: Any) -> dict:
        url = getattr(artifact, "url", None) or (artifact.get("url") if isinstance(artifact, dict) else str(artifact))
        entry = {"kind": "artifact", "target": target, "url": url}
        self.sent.append(entry)
        if self.echo:
            print(f"[console -> {target}] artifact: {url}")
        return entry

    def push_inbound(self, message: Any) -> None:
        self._inbound.append(message)

    async def receive(self) -> List[Any]:
        drained, self._inbound = self._inbound, []
        return drained
