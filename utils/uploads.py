import base64
import binascii
import os


DEFAULT_AUDIO_UPLOAD_BYTES = 32 * 1024 * 1024
DEFAULT_VIDEO_UPLOAD_BYTES = 256 * 1024 * 1024


def upload_limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def decode_base64_upload(data_b64: str, *, max_bytes: int) -> bytes:
    encoded = str(data_b64 or "").strip()
    if not encoded:
        raise ValueError("empty upload")
    estimated = (len(encoded) * 3) // 4
    if estimated > max_bytes + 2:
        raise ValueError(f"upload exceeds {max_bytes // (1024 * 1024)} MB limit")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 upload") from exc
    if not raw:
        raise ValueError("empty upload")
    if len(raw) > max_bytes:
        raise ValueError(f"upload exceeds {max_bytes // (1024 * 1024)} MB limit")
    return raw
