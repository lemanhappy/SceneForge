import re
from typing import Optional

from .models import UserCommand

_APPROVE = {"通过", "批准", "同意", "确认", "好的", "发布", "approve", "ok", "publish"}
_STATUS = {"查看状态", "状态", "进度", "status"}
_PAUSE = {"暂停", "pause"}
_RESUME = {"继续", "恢复", "resume"}
_CANCEL = {"取消", "终止", "cancel"}

_REGEN_HINT = ("重生成", "重新生成", "regenerate")
_REVISE_PREFIX = re.compile(r"^(修改|调整|revise|change)\s*[:：]?\s*", re.IGNORECASE)


def parse_user_command(text: str, source: str = "", session_id: Optional[str] = None) -> UserCommand:
    """Map a free-form inbound message to a structured UserCommand.

    Order matters: regenerate and revise are checked before the keyword sets so
    that "重生成第 3 镜" / "修改：..." are not misread, and anything unrecognised
    is treated as a new topic submission.
    """
    raw = (text or "").strip()
    low = raw.lower()

    def cmd(command_type, **kwargs):
        return UserCommand(command_type=command_type, text=raw, source=source, session_id=session_id, **kwargs)

    if any(h in raw for h in _REGEN_HINT) or "regenerate" in low:
        num_match = re.search(r"(\d+)", raw)
        return cmd("regenerate", shot_idx=int(num_match.group(1)) if num_match else None)

    if _REVISE_PREFIX.match(raw):
        remainder = _REVISE_PREFIX.sub("", raw).strip()
        return UserCommand(command_type="revise", text=remainder or raw, source=source, session_id=session_id)

    if raw in _APPROVE or low in _APPROVE:
        return cmd("approve")
    if raw in _STATUS or low in _STATUS:
        return cmd("status")
    if raw in _PAUSE or low in _PAUSE:
        return cmd("pause")
    if raw in _RESUME or low in _RESUME:
        return cmd("resume")
    if raw in _CANCEL or low in _CANCEL:
        return cmd("cancel")

    return cmd("new_topic")
