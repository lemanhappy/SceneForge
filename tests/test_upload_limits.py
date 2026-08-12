import base64

import pytest

from utils.uploads import decode_base64_upload


def test_valid_upload_decodes():
    assert decode_base64_upload(base64.b64encode(b"video").decode(), max_bytes=8) == b"video"


def test_invalid_base64_is_rejected():
    with pytest.raises(ValueError, match="invalid base64"):
        decode_base64_upload("not base64!", max_bytes=1024)


def test_estimated_and_actual_limits_are_enforced():
    encoded = base64.b64encode(b"12345").decode()
    with pytest.raises(ValueError, match="exceeds"):
        decode_base64_upload(encoded, max_bytes=4)
