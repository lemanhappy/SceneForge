from __future__ import annotations

from typing import Any

from .base import MessagingChannel


class WeChatChannel(MessagingChannel):
    """Placeholder WeChat channel. Personal-WeChat automation carries compliance
    and stability risk (design §6), so it is disabled by default and not yet
    implemented; instantiating + using it raises a clear error rather than
    silently failing."""

    type = "wechat"

    def __init__(self, adapter: str = "personal_wechat", **kwargs: Any):
        self.adapter = adapter

    async def send_text(self, target: str, text: str) -> Any:
        raise NotImplementedError("WeChat channel is not implemented yet; enable Feishu instead.")

    async def send_artifact(self, target: str, artifact: Any) -> Any:
        raise NotImplementedError("WeChat channel is not implemented yet; enable Feishu instead.")
