import socket
import unittest
from unittest.mock import patch

from savehack import security
from savehack.config import _origins
from savehack.errors import AppError, ErrorCode
from savehack.net import open_stream
from savehack.security import (egress_guard, filename_from_url, guard_depth, is_public_ip, normalize_url, safe_filename, validate_url)

from tests.helpers import NetCase


def gai(*addrs):
    return [(2, 1, 6, "", (a, 443)) for a in addrs]


class ValidateUrlTests(unittest.TestCase):
    def assertInvalid(self, url):
        with self.assertRaises(AppError) as cm:
            validate_url(url)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_URL, url)

    def test_rejects_dangerous_schemes(self):
        for u in ("ftp://example.com/a.mp4", "file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)", "data:text/html,hi", "ws://example.com/"):
            self.assertInvalid(u)

    def test_rejects_credentials_and_odd_ports(self):
        self.assertInvalid("https://user:pass@example.com/x")
        self.assertInvalid("https://example.com:8443/x")
        self.assertInvalid("http://example.com:22/x")

    def test_rejects_blocked_names_without_dns(self):
        for u in ("http://localhost/", "https://metadata.google.internal/", "https://foo.internal/", "https://printer.local/", "https://intranet/", "http://2130706433/"):
            self.assertInvalid(u)

    def test_rejects_private_ip_literals(self):
        for u in ("http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                  "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/", "http://0.0.0.0/", "http://[fd00::1]/"):
            self.assertInvalid(u)

    def test_rejects_empty_whitespace_and_control_chars(self):
        for u in ("", "   ", "https://exa mple.com/", "https://example.com/\r\nHost: evil", "https://\x00example.com"):
            self.assertInvalid(u)

    def test_rejects_overlong_url(self):
        self.assertInvalid("https://example.com/" + "a" * 3000)

    def test_cleans_copied_markdown_and_trailing_punctuation(self):
        with patch.object(security, "_REAL_GETADDRINFO", return_value=gai("93.184.216.34")):
            self.assertEqual(validate_url("(<https://Example.com/video.mp4>),"), "https://example.com/video.mp4")

    def test_normalisation_strips_fragment_and_lowercases_host(self):
        self.assertEqual(normalize_url("HTTP://Example.COM/a?b=1#frag"), "http://example.com/a?b=1")
        self.assertEqual(normalize_url("https://example.com"), "https://example.com/")

    @patch.object(security, "_REAL_GETADDRINFO")
    def test_hostname_resolving_to_private_is_rejected(self, g):
        for addr in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
            g.return_value = gai(addr)
            self.assertInvalid("https://innocent.example.com/video.mp4")

    @patch.object(security, "_REAL_GETADDRINFO")
    def test_mixed_public_and_private_answers_are_rejected(self, g):
        g.return_value = gai("93.184.216.34", "10.0.0.5")
        self.assertInvalid("https://mixed.example.com/")

    @patch.object(security, "_REAL_GETADDRINFO")
    def test_alternate_ip_notation_resolving_to_loopback_is_rejected(self, g):
        g.return_value = gai("127.0.0.1")
        self.assertInvalid("http://0x7f.0.0.1/")

    @patch.object(security, "_REAL_GETADDRINFO", side_effect=socket.gaierror(-2, "unknown"))
    def test_unresolvable_host_is_invalid(self, _g):
        self.assertInvalid("https://does-not-exist.example.com/")

    @patch.object(security, "_REAL_GETADDRINFO")
    def test_accepts_public_http_and_https(self, g):
        g.return_value = gai("93.184.216.34")
        self.assertEqual(validate_url("http://example.com/a.mp4"), "http://example.com/a.mp4")
        self.assertEqual(validate_url("https://example.com/watch?v=abc"), "https://example.com/watch?v=abc")

    def test_public_ip_literal_is_accepted(self):
        self.assertEqual(validate_url("https://93.184.216.34/file.zip"), "https://93.184.216.34/file.zip")


class IpClassificationTests(unittest.TestCase):
    def test_non_public_ranges(self):
        for bad in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "garbage", ""):
            self.assertFalse(is_public_ip(bad), bad)

    def test_embedded_ipv4_in_nat64_and_6to4_is_checked(self):
        self.assertFalse(is_public_ip("64:ff9b::7f00:1"))  # NAT64 -> 127.0.0.1
        self.assertFalse(is_public_ip("64:ff9b::a9fe:a9fe"))  # NAT64 -> 169.254.169.254
        self.assertFalse(is_public_ip("2002:7f00:0001::1"))  # 6to4 -> 127.0.0.1
        self.assertTrue(is_public_ip("64:ff9b::5db8:d822"))  # NAT64 -> 93.184.216.34

    def test_public_addresses(self):
        for ok in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(is_public_ip(ok), ok)


class EgressGuardTests(unittest.TestCase):
    def test_inactive_by_default(self):
        with patch.object(security, "_REAL_GETADDRINFO", return_value=gai("127.0.0.1")):
            self.assertEqual(socket.getaddrinfo("localhost", 443), gai("127.0.0.1"))

    def test_blocks_private_only_results(self):
        with patch.object(security, "_REAL_GETADDRINFO", return_value=gai("169.254.169.254")), egress_guard():
            with self.assertRaises(socket.gaierror):
                socket.getaddrinfo("metadata.internal", 80)

    def test_filters_mixed_results(self):
        with patch.object(security, "_REAL_GETADDRINFO", return_value=gai("10.0.0.5", "93.184.216.34")), egress_guard():
            res = socket.getaddrinfo("example.com", 443)
        self.assertEqual([r[4][0] for r in res], ["93.184.216.34"])

    def test_blocks_dns_rebinding_between_validation_and_connect(self):
        answers = iter([gai("93.184.216.34"), gai("127.0.0.1")])
        with patch.object(security, "_REAL_GETADDRINFO", side_effect=lambda *a, **k: next(answers)):
            self.assertEqual(validate_url("https://example.com/watch?v=abc"), "https://example.com/watch?v=abc")
            with egress_guard():
                with self.assertRaises(socket.gaierror):
                    socket.getaddrinfo("example.com", 443)

    def test_released_after_context_and_after_error(self):
        before = guard_depth()
        with self.assertRaises(RuntimeError):
            with egress_guard():
                self.assertGreater(guard_depth(), before)
                raise RuntimeError()
        self.assertEqual(guard_depth(), before)


class RedirectValidationTests(NetCase):
    def test_follows_safe_redirect(self):
        resp, final = open_stream(self.server.url("/redirect"))
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(final.endswith("/file.bin"))
        finally:
            resp.close()

    def test_redirect_to_cloud_metadata_is_blocked(self):
        with self.assertRaises(AppError) as cm:
            open_stream(self.server.url("/redirect-private"))
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_URL)
        self.assertIn("169.254.169.254", cm.exception.detail)

    def test_redirect_to_blocked_hostname_is_blocked(self):
        with self.assertRaises(AppError) as cm:
            open_stream(self.server.url("/redirect-localname"))
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_URL)

    def test_redirect_loop_is_bounded(self):
        with self.assertRaises(AppError) as cm:
            open_stream(self.server.url("/redirect-loop"))
        self.assertEqual(cm.exception.code, ErrorCode.NETWORK_ERROR)

    def test_environment_proxies_are_ignored(self):
        from savehack.net import get_session
        self.assertFalse(get_session().trust_env)


class FilenameTests(unittest.TestCase):
    def test_path_traversal_and_separators(self):
        for evil in ("../../etc/passwd", "..\\..\\windows\\system32\\cmd.exe", "/abs/path/file.txt", "a/b/c.mp4"):
            out = safe_filename(evil)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)
            self.assertNotIn("..", out.replace("...", ""))
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")

    def test_control_characters_and_reserved_chars_removed(self):
        self.assertEqual(safe_filename('vi\x00de\no<>:"|?*.mp4'), "video.mp4")

    def test_windows_reserved_names(self):
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("nul"), "_nul")

    def test_empty_and_dots_fall_back_to_default(self):
        self.assertEqual(safe_filename("", default="x"), "x")
        self.assertEqual(safe_filename("...", default="x"), "x")
        self.assertEqual(safe_filename("   ", default="x", ext=".mp4"), "x.mp4")

    def test_extension_preserved_and_lowercased_and_added_when_missing(self):
        self.assertEqual(safe_filename("Clip.MP4"), "Clip.mp4")
        self.assertEqual(safe_filename("clip", ext=".mp3"), "clip.mp3")

    def test_length_limit_keeps_extension_and_valid_utf8(self):
        out = safe_filename("é" * 500 + ".mp4", max_len=100)
        self.assertLessEqual(len(out.encode("utf-8")), 100)
        self.assertTrue(out.endswith(".mp4"))
        out.encode("utf-8").decode("utf-8")

    def test_unicode_kept(self):
        self.assertEqual(safe_filename("lagu ✓ baru.mp3"), "lagu ✓ baru.mp3")

    def test_filename_from_url(self):
        self.assertEqual(filename_from_url("https://x.com/a/b/clip.mp4?x=1"), "clip.mp4")
        self.assertEqual(filename_from_url("https://x.com/"), "")


class ConfigTests(unittest.TestCase):
    def test_default_origin_is_wildcard(self):
        self.assertEqual(_origins(None), ["*"])
        self.assertEqual(_origins(" "), ["*"])

    def test_parses_comma_separated_origins(self):
        self.assertEqual(_origins("https://a.example, https://b.example"), ["https://a.example", "https://b.example"])


if __name__ == "__main__":
    unittest.main()
