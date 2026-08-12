"""Tests for the local web access token."""

import unittest

from server.app import allowed_cors_origin, authorized
from server.http_security import query_token_allowed, request_body_limit, request_origin_allowed
from main_server import _is_loopback_host


class TestAuthorized(unittest.TestCase):
    def test_disabled_when_no_token(self):
        self.assertTrue(authorized({}, None))
        self.assertTrue(authorized({}, ""))

    def test_single_token_bearer(self):
        self.assertTrue(authorized({"Authorization": "Bearer abc"}, "abc"))
        self.assertFalse(authorized({"Authorization": "Bearer wrong"}, "abc"))

    def test_x_auth_token_header(self):
        self.assertTrue(authorized({"X-Auth-Token": "abc"}, "abc"))

    def test_query_token(self):
        self.assertTrue(authorized({}, "abc", query_token="abc"))
        self.assertFalse(authorized({}, "abc", query_token="nope"))

    def test_empty_provided_rejected(self):
        self.assertFalse(authorized({}, "abc"))


class TestCorsPolicy(unittest.TestCase):
    def test_allows_loopback_browser_origins(self):
        for origin in (
            "http://127.0.0.1:5173",
            "http://localhost:8770",
            "http://[::1]:8770",
            "https://127.0.0.2",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(allowed_cors_origin({"Origin": origin}), origin)

    def test_rejects_remote_or_malformed_origins(self):
        for origin in (
            "https://example.com",
            "null",
            "file://localhost/app.html",
            "http://user@127.0.0.1",
            "http://127.0.0.1:invalid",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(allowed_cors_origin({"origin": origin}), "")

    def test_request_without_origin_does_not_emit_cors(self):
        self.assertEqual(allowed_cors_origin({}), "")

    def test_mutations_allow_cli_loopback_and_same_origin(self):
        self.assertTrue(request_origin_allowed({}))
        self.assertTrue(request_origin_allowed({"Origin": "http://127.0.0.1:5173"}))
        self.assertTrue(request_origin_allowed({
            "Origin": "https://studio.example.com",
            "Host": "studio.example.com",
        }))

    def test_mutations_reject_cross_site_origin(self):
        self.assertFalse(request_origin_allowed({
            "Origin": "https://evil.example",
            "Host": "127.0.0.1:8770",
        }))
        self.assertFalse(request_origin_allowed({"Origin": "null"}))

    def test_query_tokens_are_get_only(self):
        self.assertTrue(query_token_allowed("GET", "/api/production/job-1/video"))
        self.assertTrue(query_token_allowed("GET", "/api/production/jobs/1/stream"))
        self.assertFalse(query_token_allowed("GET", "/api/config"))
        self.assertFalse(query_token_allowed("POST", "/api/production/job-1/video"))
        self.assertFalse(query_token_allowed("DELETE", "/api/edit/video"))

    def test_upload_routes_have_bounded_larger_limits(self):
        ordinary = request_body_limit("/api/config")
        audio = request_body_limit("/api/bgm/upload")
        video = request_body_limit("/api/edit/upload")
        self.assertGreater(audio, ordinary)
        self.assertGreater(video, audio)


class TestServerBindingPolicy(unittest.TestCase):
    def test_loopback_hosts_are_recognized(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(_is_loopback_host(host))

    def test_network_hosts_are_not_treated_as_loopback(self):
        for host in ("0.0.0.0", "192.168.1.20", "sceneforge.local", ""):
            with self.subTest(host=host):
                self.assertFalse(_is_loopback_host(host))


if __name__ == "__main__":
    unittest.main()
