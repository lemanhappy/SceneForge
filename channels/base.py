from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional


def format_review(review: Any) -> str:
    """Render a review task (object or dict) into a plain-text message.

    Duck-typed so it works with a structured ReviewTask later or a simple dict
    now. Recognised fields: stage, summary, artifact_refs, options.
    """
    def field(name, default=""):
        if isinstance(review, dict):
            return review.get(name, default)
        return getattr(review, name, default)

    stage = field("stage", "review")
    titles = {
        "character": "角色设定审核",
        "script": "剧本审核",
        "storyboard": "分镜脚本审核",
        "shot_video": "分镜视频审核",
        "final": "成片审核",
    }
    lines = [f"【{titles.get(str(stage), '审核')}】"]
    summary = field("summary")
    if summary:
        lines += ["", str(summary)]
    refs = field("artifact_refs") or []
    if refs:
        lines += ["", "相关产物："] + [f"- {r}" for r in refs]
    options = field("options") or ["通过", "修改：你的修改意见", "取消"]
    lines += ["", "请回复："] + [str(o) for o in options]
    return "\n".join(lines)


class MessagingChannel(ABC):
    """Unified inbound/outbound channel interface (design §6).

    All entry points (Feishu, WeChat, console) implement this so the workflow
    engine drives one command model regardless of source.
    """

    type: str = "base"

    @abstractmethod
    async def send_text(self, target: str, text: str) -> Any: ...

    async def send_review(self, target: str, review_task: Any) -> Any:
        return await self.send_text(target, format_review(review_task))

    @abstractmethod
    async def send_artifact(self, target: str, artifact: Any) -> Any: ...

    async def receive(self) -> List[Any]:
        """Pull inbound messages. Channels without an inbound path return []."""
        return []
