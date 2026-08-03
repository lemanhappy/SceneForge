from __future__ import annotations

import os
import re
from typing import Any, List, Optional, Tuple

from .base import MessagingChannel
from .console import ConsoleChannel
from .feishu import FeishuChannel
from .wechat import WeChatChannel

_ENV_RE = re.compile(r"\$\{([^}]+)\}")

_CHANNEL_TYPES = {
    "console": ConsoleChannel,
    "feishu": FeishuChannel,
    "wechat": WeChatChannel,
}


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} references against the environment in string values."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


class ChannelDispatcher:
    """Routes outbound messages to the enabled channels and drains inbound
    (design §6/§20). One command layer, many channels."""

    def __init__(self, channels: Optional[List[Tuple[MessagingChannel, Optional[str]]]] = None,
                 inbound_enabled: bool = True, outbound_enabled: bool = True):
        # each entry: (channel, default_target)
        self.channels: List[Tuple[MessagingChannel, Optional[str]]] = channels or []
        self.inbound_enabled = inbound_enabled
        self.outbound_enabled = outbound_enabled

    @classmethod
    def from_config(cls, config: dict) -> Optional["ChannelDispatcher"]:
        section = (config or {}).get("messaging") or {}
        channel_configs = section.get("channels") or []
        built: List[Tuple[MessagingChannel, Optional[str]]] = []
        for ch in channel_configs:
            if not ch.get("enabled"):
                continue
            ctype = ch.get("type")
            factory = _CHANNEL_TYPES.get(ctype)
            if factory is None:
                raise ValueError(f"Unknown messaging channel type: {ctype}")
            kwargs = {k: _expand_env(v) for k, v in ch.items() if k not in {"type", "enabled", "default_target"}}
            built.append((factory(**kwargs), _expand_env(ch.get("default_target"))))
        if not built:
            return None
        return cls(
            channels=built,
            inbound_enabled=bool(section.get("inbound_enabled", True)),
            outbound_enabled=bool(section.get("outbound_enabled", True)),
        )

    def _targets(self, target: Optional[str]):
        for channel, default_target in self.channels:
            resolved = target or default_target
            if resolved:
                yield channel, resolved

    async def broadcast_text(self, text: str, target: Optional[str] = None) -> List[Any]:
        if not self.outbound_enabled:
            return []
        return [await channel.send_text(t, text) for channel, t in self._targets(target)]

    async def broadcast_review(self, review_task: Any, target: Optional[str] = None) -> List[Any]:
        if not self.outbound_enabled:
            return []
        return [await channel.send_review(t, review_task) for channel, t in self._targets(target)]

    async def broadcast_artifact(self, artifact: Any, target: Optional[str] = None) -> List[Any]:
        if not self.outbound_enabled:
            return []
        return [await channel.send_artifact(t, artifact) for channel, t in self._targets(target)]

    async def receive(self) -> List[Any]:
        if not self.inbound_enabled:
            return []
        messages: List[Any] = []
        for channel, _ in self.channels:
            messages.extend(await channel.receive())
        return messages
