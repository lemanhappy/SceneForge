from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

REVIEW_STAGES = ("character", "script", "storyboard", "shot_video", "final")
REVIEW_STATUSES = ("pending", "approved", "rejected", "revised")


class ReviewTask(BaseModel):
    """A structured human-review gate for one production stage (design §7).

    Persisted as a plain dict in the session record; this model is the
    validated shape used by callers and by channel rendering (format_review
    duck-types stage / summary / artifact_refs).
    """

    review_id: str
    session_id: str
    stage: str = Field(description="One of character/script/storyboard/shot_video/final.")
    artifact_version: str = "v1"
    status: str = "pending"
    summary: str = ""
    artifact_refs: List[str] = Field(default_factory=list)
    created_at: str = ""
    resolved_at: Optional[str] = None
