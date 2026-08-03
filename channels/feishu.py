from __future__ import annotations

import json
from typing import Any, List, Optional

import requests

from .base import MessagingChannel, format_review


class FeishuChannel(MessagingChannel):
    """Feishu (Lark) outbound channel via the official OpenAPI.

    Inbound events require an event-subscription webhook server, which is a
    separate deployment concern; ``receive`` returns [] for now. ``session`` is
    injectable so tests can run without network or credentials.
    """

    type = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        receive_id_type: str = "open_id",
        base_url: str = "https://open.feishu.cn",
        session: Any = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id_type = receive_id_type
        self.base_url = base_url.rstrip("/")
        self._session = session or requests
        self._token: Optional[str] = None

    # ----- low level ----------------------------------------------------

    def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        resp = self._session.post(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token request failed: {data}")
        self._token = data["tenant_access_token"]
        return self._token

    def _send_message(self, target: str, msg_type: str, content: dict) -> dict:
        token = self._get_token()
        resp = self._session.post(
            f"{self.base_url}/open-apis/im/v1/messages",
            params={"receive_id_type": self.receive_id_type},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json={"receive_id": target, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)},
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu send ({msg_type}) failed: {data}")
        return data

    # ----- MessagingChannel --------------------------------------------

    async def send_text(self, target: str, text: str) -> dict:
        return self._send_message(target, "text", {"text": text})

    async def send_review(self, target: str, review_task: Any) -> dict:
        # Plain text keeps the first version simple; interactive cards can be
        # added later by switching msg_type to "interactive".
        return await self.send_text(target, format_review(review_task))

    async def send_artifact(self, target: str, artifact: Any) -> dict:
        url = getattr(artifact, "url", None) or (artifact.get("url") if isinstance(artifact, dict) else str(artifact))
        name = getattr(artifact, "name", None) or (artifact.get("name") if isinstance(artifact, dict) else "")
        text = "成片已生成：\n" + (f"标题：{name}\n" if name else "") + f"链接：{url}"
        return await self.send_text(target, text)

    async def receive(self) -> List[Any]:
        return []
