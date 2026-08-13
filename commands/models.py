from pydantic import BaseModel
from typing import Literal, Optional


class UserCommand(BaseModel):
    """Unified command model shared by console and message channels (design §4).

    A single command layer keeps every entry point driving the same state
    machine, so the same parsing applies to Feishu/WeChat/console alike.
    """

    command_type: Literal[
        "new_topic",
        "approve",
        "publish",
        "revise",
        "regenerate",
        "status",
        "pause",
        "resume",
        "cancel",
    ]
    session_id: Optional[str] = None
    stage: Optional[str] = None
    text: Optional[str] = None
    source: str = ""
    # For regenerate: the shot number as the user stated it (e.g. "第 3 镜" -> 3).
    # 1-based vs 0-based mapping is the workflow layer's decision.
    shot_idx: Optional[int] = None
