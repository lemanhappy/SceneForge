import hashlib
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Optional

from .models import HostedArtifact


class ArtifactHost:
    """Publishes finished artifacts and returns a shareable URL (design §18).

    First version: ``local_static`` copies the file into a public web root and
    builds ``<public_base_url>/<name>``. Cloud backends (S3/OSS/COS/Feishu) plug
    in later behind the same ``upload`` interface.
    """

    def __init__(self, public_base_url: str, local_root: str):
        self.public_base_url = public_base_url.rstrip("/")
        self.local_root = Path(local_root)

    @classmethod
    def from_config(cls, config: dict) -> Optional["ArtifactHost"]:
        section = (config or {}).get("hosting") or {}
        host_type = section.get("type")
        if not host_type:
            return None
        if host_type != "local_static":
            raise ValueError(f"Unsupported hosting.type: {host_type} (only 'local_static' is implemented)")
        return cls(
            public_base_url=str(section.get("public_base_url", "")),
            local_root=str(section.get("local_root", ".public_artifacts")),
        )

    def _public_name(self, path: Path) -> str:
        # Stable per source path, collision-safe across different sessions that
        # share a filename (e.g. every session's final_video.mp4).
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        return f"{digest}_{path.name}"

    async def upload(self, path) -> HostedArtifact:
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"Artifact does not exist: {src}")
        self.local_root.mkdir(parents=True, exist_ok=True)
        name = self._public_name(src)
        dst = self.local_root / name
        if src.resolve() != dst.resolve():
            shutil.copy(src, dst)
        url = f"{self.public_base_url}/{name}" if self.public_base_url else dst.as_uri()
        return HostedArtifact(
            name=name,
            url=url,
            local_path=str(dst),
            size=os.path.getsize(dst),
            content_type=mimetypes.guess_type(str(dst))[0],
        )
