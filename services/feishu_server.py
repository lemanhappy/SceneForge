"""Minimal Feishu event-subscription webhook that runs the whole inbound chain:

    HTTP POST  ->  handle_event  ->  event_to_command  ->  TriggerService  ->  sceneforge tools

The request-handling core (`FeishuWebhookHandler.handle_request`) is separated
from socket binding so it is testable without a network. `serve` is a thin
stdlib http.server wrapper around it.

Live use needs FEISHU_APP_ID/SECRET (for replies) and the event-subscription
configured in the Feishu developer console. Encrypt Key is optional; if set,
install `cryptography` and pass encrypt_key.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from typing import Any, Optional

from channels import event_to_command, handle_event, verify_signature
from channels.feishu import FeishuChannel
from commands import parse_user_command


class FeishuWebhookHandler:
    def __init__(
        self,
        trigger_service,
        channel: Optional[FeishuChannel] = None,
        encrypt_key: Optional[str] = None,
        verification_token: Optional[str] = None,
        reply_target: Optional[str] = None,
        ack_text: str = "已收到，正在处理。",
        dedup_size: int = 2048,
    ):
        self.trigger_service = trigger_service
        self.channel = channel
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.reply_target = reply_target
        self.ack_text = ack_text
        # Feishu re-delivers events on timeout/error; dedup by event_id so the
        # same message never triggers generation twice.
        self._seen: "OrderedDict[str, bool]" = OrderedDict()
        self._seen_size = dedup_size
        self._seen_lock = threading.Lock()

    def _is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._seen_lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = True
            while len(self._seen) > self._seen_size:
                self._seen.popitem(last=False)
            return False

    @classmethod
    def from_config(cls, config: dict, trigger_service, channel: Optional[FeishuChannel] = None) -> "FeishuWebhookHandler":
        feishu_cfg = {}
        for ch in ((config or {}).get("messaging") or {}).get("channels", []) or []:
            if ch.get("type") == "feishu":
                feishu_cfg = ch
                break
        return cls(
            trigger_service=trigger_service,
            channel=channel,
            encrypt_key=feishu_cfg.get("encrypt_key") or None,
            verification_token=feishu_cfg.get("verification_token") or None,
            reply_target=feishu_cfg.get("default_target") or None,
        )

    def _signature_ok(self, headers: Optional[dict], raw_body: Any) -> bool:
        # Only enforced when an Encrypt Key is configured and a signature header
        # is present (Feishu only signs when encryption is enabled).
        if not self.encrypt_key or not headers:
            return True
        signature = headers.get("X-Lark-Signature") or headers.get("x-lark-signature")
        if not signature:
            return True
        timestamp = headers.get("X-Lark-Request-Timestamp") or headers.get("x-lark-request-timestamp") or ""
        nonce = headers.get("X-Lark-Request-Nonce") or headers.get("x-lark-request-nonce") or ""
        return verify_signature(timestamp, nonce, self.encrypt_key, raw_body, signature)

    async def handle_request(self, raw_body: Any, headers: Optional[dict] = None) -> dict:
        """Process one event POST. Returns {"status": int, "body": dict}."""
        if not self._signature_ok(headers, raw_body):
            return {"status": 401, "body": {"error": "invalid signature"}}

        try:
            parsed = handle_event(raw_body, encrypt_key=self.encrypt_key)
        except Exception as exc:
            return {"status": 400, "body": {"error": str(exc)}}

        if parsed.get("kind") == "challenge":
            return {"status": 200, "body": {"challenge": parsed.get("challenge", "")}}

        if parsed.get("kind") != "message":
            return {"status": 200, "body": {"ignored": True}}

        # Idempotency: ack duplicates fast without re-dispatching.
        if self._is_duplicate(parsed.get("event_id", "")):
            return {"status": 200, "body": {"ok": True, "dedup": True}}

        command = event_to_command(parsed)
        result = await self.trigger_service.handle_command(
            command, sender_id=parsed.get("sender_id") or "", channel="feishu"
        )

        reply = result.get("note") or self.ack_text if isinstance(result, dict) else self.ack_text
        target = self.reply_target or parsed.get("sender_id")
        if self.channel is not None and target:
            try:
                await self.channel.send_text(target, reply)
            except Exception:
                pass  # reply is best-effort; never fail the webhook on it

        return {"status": 200, "body": {"ok": bool(result.get("ok")), "command_type": command.command_type if command else None}}


def serve(handler: FeishuWebhookHandler, host: str = "0.0.0.0", port: int = 8080):  # pragma: no cover - socket binding
    """Block serving Feishu events over HTTP using only the standard library."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            headers = {k: v for k, v in self.headers.items()}
            try:
                response = asyncio.run(handler.handle_request(raw, headers=headers))
            except Exception as exc:
                response = {"status": 500, "body": {"error": str(exc)}}
            payload = json.dumps(response["body"], ensure_ascii=False).encode("utf-8")
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # quiet

    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"Feishu webhook listening on http://{host}:{port}")
    server.serve_forever()
