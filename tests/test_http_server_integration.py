from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(port: int, method: str, path: str, *, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_real_http_server_enforces_auth_origin_and_ranges(tmp_path):
    (tmp_path / "index.html").write_bytes(b"0123456789abcdefghij")
    port = _free_port()
    code = (
        "import sys; from server.app import AppAPI, serve; "
        "serve(AppAPI(static_dir=sys.argv[1]), port=int(sys.argv[2]), auth_token='test-secret')"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(port)],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise AssertionError("server exited early: " + (process.stderr.read() if process.stderr else ""))
            try:
                status, _, _ = _request(
                    port, "GET", "/api/health", headers={"Authorization": "Bearer test-secret"}
                )
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("server did not start")
            time.sleep(0.1)

        assert _request(port, "GET", "/api/health")[0] == 401
        assert _request(port, "GET", "/api/health?token=test-secret")[0] == 401
        assert _request(
            port,
            "POST",
            "/api/unknown",
            headers={
                "Authorization": "Bearer test-secret",
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )[0] == 403
        status, headers, body = _request(port, "GET", "/", headers={"Range": "bytes=5-9"})
        assert status == 206
        assert headers["Content-Range"] == "bytes 5-9/20"
        assert body == b"56789"
        status, headers, body = _request(port, "HEAD", "/")
        assert status == 200
        assert headers["Content-Length"] == "20"
        assert body == b""
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
