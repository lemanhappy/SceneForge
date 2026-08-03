"""Feishu event-subscription inbound handling (design §4/§6).

Covers the parts that run without credentials or network: URL-verification
challenge, request signature verification, event decoding, and mapping a message
to a UserCommand. Event-body decryption (Feishu's optional "Encrypt Key") is
supported via a lazy `cryptography` import; if that package is absent and an
Encrypt Key is configured, a clear error is raised rather than failing silently.

The actual webhook server (receiving HTTP POSTs) is a deployment concern and is
intentionally out of scope; these helpers are what such a server would call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Optional

from commands import UserCommand, parse_user_command


def verify_signature(timestamp: str, nonce: str, encrypt_key: str, body: Any, signature: str) -> bool:
    """Feishu v2 signature = sha256(timestamp + nonce + encrypt_key + raw_body)."""
    body_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    payload = (timestamp + nonce + encrypt_key).encode("utf-8") + body_bytes
    digest = hashlib.sha256(payload).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def decrypt(encrypt_key: str, encrypt_b64: str) -> str:
    """AES-256-CBC decrypt a Feishu encrypted event body (IV prefixed, PKCS7)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "Feishu event decryption needs the 'cryptography' package. Install it, "
            "or disable Encrypt Key in the Feishu event subscription configuration."
        ) from exc
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    data = base64.b64decode(encrypt_b64)
    iv, ciphertext = data[:16], data[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    pad = plain[-1]
    return plain[: -pad].decode("utf-8")


def handle_event(raw_body: Any, encrypt_key: Optional[str] = None) -> dict:
    """Decode a Feishu event POST body.

    Returns one of:
    - {"kind": "challenge", "challenge": "..."} for URL verification
    - {"kind": "message", "text", "sender_id", "message_id", "event_type"}
    - {"kind": "ignored", "event_type": ...} for non-message events
    """
    body = json.loads(raw_body) if isinstance(raw_body, (str, bytes, bytearray)) else dict(raw_body)

    if "encrypt" in body:
        if not encrypt_key:
            raise RuntimeError("Received an encrypted Feishu event but no encrypt_key was configured.")
        body = json.loads(decrypt(encrypt_key, body["encrypt"]))

    if body.get("type") == "url_verification" or "challenge" in body:
        return {"kind": "challenge", "challenge": body.get("challenge", "")}

    header = body.get("header") or {}
    event = body.get("event") or {}
    event_type = header.get("event_type") or body.get("type") or ""
    event_id = header.get("event_id") or body.get("uuid") or ""
    message = event.get("message") or {}
    msg_type = message.get("message_type") or message.get("msg_type")

    if msg_type != "text":
        return {"kind": "ignored", "event_type": event_type, "event_id": event_id}

    text = ""
    content = message.get("content")
    if content:
        try:
            text = json.loads(content).get("text", "")
        except (json.JSONDecodeError, TypeError):
            text = str(content)

    sender = event.get("sender") or {}
    sender_id = (sender.get("sender_id") or {}).get("open_id") or sender.get("open_id")
    return {
        "kind": "message",
        "text": text,
        "sender_id": sender_id,
        "message_id": message.get("message_id"),
        "event_type": event_type,
        "event_id": event_id,
    }


def event_to_command(parsed: dict, session_id: Optional[str] = None) -> Optional[UserCommand]:
    """Turn a decoded message event into a UserCommand (None for non-messages)."""
    if parsed.get("kind") != "message":
        return None
    return parse_user_command(parsed.get("text", ""), source="feishu", session_id=session_id)
