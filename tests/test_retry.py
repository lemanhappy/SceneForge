"""Tests for the async transient-failure retry helper used by video generation."""

import asyncio
import unittest

import requests

from utils.retry import (
    is_retryable_generation_error,
    is_retryable_http_status,
    retry_after_seconds,
    retry_async,
)


def _run(coro):
    return asyncio.run(coro)


class TestClassify(unittest.TestCase):
    def test_transient_markers(self):
        self.assertTrue(is_retryable_generation_error(RuntimeError("503 无可用渠道")))
        self.assertTrue(is_retryable_generation_error(Exception("Request timed out")))
        self.assertTrue(is_retryable_generation_error(requests.ConnectionError()))
        self.assertTrue(is_retryable_generation_error(RuntimeError(
            "\u5f53\u524d\u5206\u7ec4\u4e0a\u6e38\u8d1f\u8f7d\u5df2\u9971\u548c\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"
        )))

    def test_http_status_and_retry_after(self):
        self.assertTrue(is_retryable_http_status(429))
        self.assertTrue(is_retryable_http_status(503))
        self.assertFalse(is_retryable_http_status(401))
        self.assertEqual(retry_after_seconds({"Retry-After": "17"}, 1), 17)
        self.assertEqual(retry_after_seconds({}, 3), 20)
        self.assertEqual(retry_after_seconds({"Retry-After": "invalid"}, 4), 30)

    def test_non_transient(self):
        self.assertFalse(is_retryable_generation_error(ValueError("bad prompt")))
        err = requests.HTTPError()
        err.response = type("R", (), {"status_code": 400})()
        self.assertFalse(is_retryable_generation_error(err))


class TestRetryAsync(unittest.TestCase):
    def test_success_first_try(self):
        calls = {"n": 0}
        async def factory():
            calls["n"] += 1
            return "ok"
        self.assertEqual(_run(retry_async(factory, attempts=3, base_wait=0)), "ok")
        self.assertEqual(calls["n"], 1)

    def test_retries_then_succeeds(self):
        calls = {"n": 0}
        async def factory():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("503 service unavailable")
            return "ok"
        self.assertEqual(_run(retry_async(factory, attempts=3, base_wait=0)), "ok")
        self.assertEqual(calls["n"], 3)

    def test_fails_fast_on_non_transient(self):
        calls = {"n": 0}
        async def factory():
            calls["n"] += 1
            raise ValueError("deterministic")
        with self.assertRaises(ValueError):
            _run(retry_async(factory, attempts=5, base_wait=0))
        self.assertEqual(calls["n"], 1)  # no retries

    def test_exhausts_and_raises(self):
        calls = {"n": 0}
        async def factory():
            calls["n"] += 1
            raise RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            _run(retry_async(factory, attempts=3, base_wait=0))
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
