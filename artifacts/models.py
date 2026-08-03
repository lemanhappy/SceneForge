from pydantic import BaseModel
from typing import Optional


class HostedArtifact(BaseModel):
    """A file that has been published to a host and is reachable via URL."""

    name: str
    url: str
    local_path: Optional[str] = None
    size: Optional[int] = None
    content_type: Optional[str] = None
