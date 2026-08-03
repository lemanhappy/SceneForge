"""Tests for the local web access token."""

import unittest

from server.app import allowed_cors_origin, authorized
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
