import unittest
from unittest.mock import patch

from fastapi import HTTPException

from main import validate_url


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

    def test_rejects_localhost(self):
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


if __name__ == "__main__":
    unittest.main()
