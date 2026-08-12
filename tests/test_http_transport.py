import pytest

from server.app import parse_byte_range


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("", 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=50-", 100, (50, 99)),
        ("bytes=-20", 100, (80, 99)),
        ("bytes=90-200", 100, (90, 99)),
    ],
)
def test_parse_byte_range(header, size, expected):
    assert parse_byte_range(header, size) == expected


@pytest.mark.parametrize(
    "header",
    ["items=0-1", "bytes=", "bytes=1-0", "bytes=100-", "bytes=0-1,4-5", "bytes=-0"],
)
def test_invalid_or_unsatisfiable_ranges_are_rejected(header):
    with pytest.raises(ValueError):
        parse_byte_range(header, 100)
