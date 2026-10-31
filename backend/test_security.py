import unittest
from unittest.mock import patch

from fastapi import HTTPException

import socket
import main
from main import validate_url, egress_guard, is_public_ip, parse_origins, analyze, MediaRequest


class ValidateUrlTests(unittest.TestCase):
    def test_rejects_non_https(self):
        with self.assertRaises(HTTPException):
            validate_url("http://youtube.com/watch?v=abc")

    def test_rejects_embedded_credentials(self):
        with self.assertRaises(HTTPException):
            validate_url("https://user:pass@youtube.com/watch?v=abc")

    def test_rejects_nonstandard_port(self):
        with self.assertRaises(HTTPException):
            validate_url("https://youtube.com:8443/watch?v=abc")

    @patch("main.socket.getaddrinfo")
    def test_rejects_localhost(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 443))
        ]
        with self.assertRaises(HTTPException):
            validate_url("https://localhost/video.mp4")

    @patch("main.socket.getaddrinfo")
    def test_accepts_allowlisted_host_resolving_to_public_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ]
        url = "https://youtube.com/watch?v=abc"
        self.assertEqual(validate_url(url), url)

    @patch("main.socket.getaddrinfo")
    def test_rejects_host_resolving_to_private_ip(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 443))
        ]
        with self.assertRaises(HTTPException):
            validate_url("https://youtube.com/watch?v=abc")


class EgressGuardTests(unittest.TestCase):
    def _gai(self, *addrs):
        return [(2, 1, 6, "", (a, 443)) for a in addrs]

    def test_is_public_ip_handles_mapped_and_special_ranges(self):
        for bad in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "garbage"):
            self.assertFalse(is_public_ip(bad), bad)
        self.assertTrue(is_public_ip("93.184.216.34"))

    def test_guard_is_inactive_by_default(self):
        with patch.object(main, "_REAL_GETADDRINFO", return_value=self._gai("127.0.0.1")):
            self.assertEqual(socket.getaddrinfo("localhost", 443), self._gai("127.0.0.1"))

    def test_guard_blocks_private_only_results(self):
        with patch.object(main, "_REAL_GETADDRINFO", return_value=self._gai("169.254.169.254")):
            with egress_guard():
                with self.assertRaises(socket.gaierror):
                    socket.getaddrinfo("metadata.internal", 80)

    def test_guard_filters_mixed_results_to_public(self):
        with patch.object(main, "_REAL_GETADDRINFO", return_value=self._gai("10.0.0.5", "93.184.216.34")):
            with egress_guard():
                res = socket.getaddrinfo("example.com", 443)
        self.assertEqual([r[4][0] for r in res], ["93.184.216.34"])

    def test_guard_blocks_dns_rebinding_between_validation_and_connect(self):
        answers = iter([self._gai("93.184.216.34"), self._gai("127.0.0.1")])
        with patch.object(main, "_REAL_GETADDRINFO", side_effect=lambda *a, **k: next(answers)):
            self.assertEqual(validate_url("https://youtube.com/watch?v=abc"), "https://youtube.com/watch?v=abc")
            with egress_guard():
                with self.assertRaises(socket.gaierror):
                    socket.getaddrinfo("youtube.com", 443)

    def test_guard_released_after_context_and_after_error(self):
        with self.assertRaises(RuntimeError):
            with egress_guard():
                self.assertGreater(main._GUARD_DEPTH, 0)
                raise RuntimeError()
        self.assertEqual(main._GUARD_DEPTH, 0)

    def test_analyze_runs_ytdlp_under_guard(self):
        seen = {}

        class FakeYDL:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                seen["depth"] = main._GUARD_DEPTH
                return {"title": "t"}

        with patch.object(main, "validate_url", return_value="https://youtube.com/watch?v=abc"), \
             patch.object(main.yt_dlp, "YoutubeDL", FakeYDL):
            analyze(MediaRequest(url="https://youtube.com/watch?v=abc", rights_confirmed=True))
        self.assertGreater(seen["depth"], 0)
        self.assertEqual(main._GUARD_DEPTH, 0)


class CorsConfigTests(unittest.TestCase):
    def test_default_is_wildcard(self):
        self.assertEqual(parse_origins(None), ["*"])
        self.assertEqual(parse_origins(" "), ["*"])

    def test_parses_comma_separated_origins(self):
        self.assertEqual(parse_origins("https://a.example, https://b.example"), ["https://a.example", "https://b.example"])


if __name__ == "__main__":
    unittest.main()
