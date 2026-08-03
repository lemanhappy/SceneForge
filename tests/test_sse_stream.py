"""Tests for the Server-Sent Events progress stream (server/app._sse_stream).

Uses a fake HTTP handler (buffer instead of a socket) and a fake api that returns
a scripted sequence of job snapshots, with time.sleep patched out so the loop
runs instantly."""

import io
import json
import unittest
from unittest import mock

from server.app import _sse_stream


class _FakeHandler:
    def __init__(self):
        self.command = "GET"
        self.wfile = io.BytesIO()
        self._status = None
        self._headers = []

    def send_response(self, code):
        self._status = code

    def send_header(self, k, v):
        self._headers.append((k, v))

    def end_headers(self):
        pass


class _FakeApi:
    """handle() returns the next scripted (status, snapshot), holding on the last."""
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    async def handle(self, method, path, body):
        i = min(self.calls, len(self.seq) - 1)
        self.calls += 1
        return self.seq[i]


class TestSSEStream(unittest.TestCase):
    def _run(self, handler, api):
        with mock.patch("time.sleep", lambda *_a, **_k: None):
            _sse_stream(handler, api, "/api/production/jobs/j1")

    def test_sets_event_stream_headers(self):
        h = _FakeHandler()
        api = _FakeApi([(200, {"state": "done", "progress": []})])
        self._run(h, api)
        self.assertEqual(h._status, 200)
        self.assertTrue(any(k == "Content-Type" and "text/event-stream" in v for k, v in h._headers))
        self.assertTrue(any(k == "Cache-Control" and "no-cache" in v for k, v in h._headers))

    def test_streams_data_then_done(self):
        seq = [
            (200, {"state": "running", "progress": [{"stage": "x", "message": "a"}]}),
            (200, {"state": "done", "progress": [{"stage": "x", "message": "a"}],
                   "result": {"summary": "ok"}}),
        ]
        h = _FakeHandler()
        self._run(h, _FakeApi(seq))
        out = h.wfile.getvalue().decode("utf-8")
        self.assertIn("data: ", out)          # progress frame while running
        self.assertIn("event: done", out)     # terminal frame
        self.assertIn("ok", out)              # final result rode along

    def test_unknown_job_emits_done_error(self):
        h = _FakeHandler()
        self._run(h, _FakeApi([(404, {"error": "unknown job"})]))
        out = h.wfile.getvalue().decode("utf-8")
        self.assertIn("event: done", out)
        self.assertIn("unknown job", out)

    def test_client_disconnect_stops_cleanly(self):
        # wfile that raises on write simulates the client going away; the loop must
        # return without propagating the error.
        class _DeadWfile:
            def write(self, *_a):
                raise BrokenPipeError()
            def flush(self):
                pass
        h = _FakeHandler()
        h.wfile = _DeadWfile()
        api = _FakeApi([(200, {"state": "running", "progress": [{"stage": "x", "message": "a"}]})])
        self._run(h, api)  # must not raise


if __name__ == "__main__":
    unittest.main()
