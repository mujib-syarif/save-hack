"""Shared test fixtures: a local HTTP server and a context that lets tests reach it.

Production code refuses loopback addresses (that is the point of the SSRF
guard). Tests re-enable *only* 127.0.0.1 and the test server's port by patching
`security.is_public_ip` and `security.ALLOWED_PORTS`; every other private range,
such as 169.254.169.254 or 10.0.0.0/8, stays blocked.
"""
import hashlib
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from savehack import security
from savehack.config import settings

_REAL_IS_PUBLIC = security.is_public_ip

PAYLOAD = bytes((i * 7 + 3) % 256 for i in range(300_000))
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
ETAG = '"abc123"'

HLS_MASTER = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio_en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Indonesia",LANGUAGE="id",URI="audio_id.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2",AUDIO="aud"
360p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",FRAME-RATE=30,AUDIO="aud"
720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud"
1080p.m3u8
"""

DASH_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1M30S">
 <Period>
  <AdaptationSet mimeType="video/mp4" contentType="video">
   <Representation id="v1" bandwidth="1000000" width="854" height="480" codecs="avc1.4d401f"/>
   <Representation id="v2" bandwidth="3000000" width="1920" height="1080" codecs="avc1.640028"/>
  </AdaptationSet>
  <AdaptationSet mimeType="audio/mp4" contentType="audio" lang="en" label="English">
   <Representation id="a1" bandwidth="128000" codecs="mp4a.40.2"/>
  </AdaptationSet>
 </Period>
</MPD>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_message = lambda *a, **k: None  # noqa: E731

    def _send(self, code, body=b"", headers=None, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        h = dict(headers or {})
        h.setdefault("Content-Length", str(len(body)))
        for k, v in h.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        srv.requests.append((self.path, dict(self.headers)))
        path = self.path.split("?")[0]
        rng = self.headers.get("Range")
        if path.startswith("/hls/") and getattr(srv, "hls_dir", None):
            f = (Path(srv.hls_dir) / path[5:]).resolve()
            if not str(f).startswith(str(Path(srv.hls_dir).resolve())) or not f.is_file():
                return self._send(404, b"nf", ctype="text/plain")
            ct = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t"
            return self._send(200, f.read_bytes(), ctype=ct)
        if path == "/file.bin":
            return self._ranged(PAYLOAD, rng, ETAG, support_range=True)
        if path == "/norange.bin":
            return self._send(200, PAYLOAD, {"ETag": ETAG})
        if path == "/video.mp4":
            return self._ranged(PAYLOAD, rng, ETAG, ctype="video/mp4", support_range=True)
        if path == "/redirect":
            return self._send(302, headers={"Location": "/file.bin", "Content-Length": "0"})
        if path == "/redirect-loop":
            return self._send(302, headers={"Location": "/redirect-loop", "Content-Length": "0"})
        if path == "/redirect-private":
            return self._send(302, headers={"Location": "http://169.254.169.254/latest/meta-data/", "Content-Length": "0"})
        if path == "/redirect-localname":
            return self._send(302, headers={"Location": "http://localhost:80/", "Content-Length": "0"})
        if path == "/cd":
            return self._send(200, b"hello", {"Content-Disposition": "attachment; filename*=UTF-8''la%20p%C3%B6rt%20%E2%82%AC.pdf"}, "application/pdf")
        if path == "/noext":
            return self._send(200, b"%PDF-1.4 fake", {}, "application/pdf")
        if path == "/page.html":
            return self._send(200, srv.page.encode(), ctype="text/html; charset=utf-8")
        if path == "/fake.mp4":  # claims to be media, is an HTML error page
            return self._send(200, b"<html>not found</html>", ctype="text/html")
        if path == "/error404":
            return self._send(404, b"nope", ctype="text/plain")
        if path == "/error403":
            return self._send(403, b"nope", ctype="text/plain")
        if path == "/error429":
            return self._send(429, b"slow down", ctype="text/plain")
        if path == "/master.m3u8":
            return self._send(200, HLS_MASTER.encode(), ctype="application/vnd.apple.mpegurl")
        if path == "/drm-master.m3u8":  # DRM only declared inside the variant playlist
            return self._send(200, b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\ndrm-var.m3u8\n', ctype="application/vnd.apple.mpegurl")
        if path == "/drm-var.m3u8":
            return self._send(200, b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n', ctype="application/vnd.apple.mpegurl")
        if path == "/live-master.m3u8":
            return self._send(200, b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlive-var.m3u8\n', ctype="application/vnd.apple.mpegurl")
        if path == "/live-var.m3u8":
            return self._send(200, b'#EXTM3U\n#EXTINF:4,\nseg1.ts\n', ctype="application/vnd.apple.mpegurl")
        if path == "/trickle.bin":  # one byte per 0.3 s, forever
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.3)
            except OSError:
                pass
            return None
        if path == "/manifest.mpd":
            return self._send(200, DASH_MPD.encode(), ctype="application/dash+xml")
        if path == "/flaky.bin":  # first connection dies half-way, later ones are served normally
            n = srv.flaky_hits = getattr(srv, "flaky_hits", 0) + 1
            if n == 1:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.send_header("ETag", ETAG)
                self.end_headers()
                self.wfile.write(PAYLOAD[:100_000])
                self.wfile.flush()
                self.close_connection = True
                return
            return self._ranged(PAYLOAD, rng, ETAG, support_range=True)
        if path == "/big.bin":
            return self._send(200, b"x" * 2_000_000)
        if path == "/chunked-endless.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(200):
                    chunk = b"y" * 65536
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
            self.close_connection = True
            return
        if path == "/slow.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(50 * 65536))
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b"z" * 65536)
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            self.close_connection = True
            return
        self._send(404, b"not found", ctype="text/plain")

    def _ranged(self, data, rng, etag, ctype="application/octet-stream", support_range=True):
        base = {"ETag": etag, "Accept-Ranges": "bytes"}
        if rng and support_range and rng.startswith("bytes="):
            if_range = self.headers.get("If-Range")
            if if_range and if_range != etag:
                return self._send(200, data, base, ctype)
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(data) - 1
            if start >= len(data):
                return self._send(416, b"", {"Content-Range": f"bytes */{len(data)}", **base}, ctype)
            body = data[start:end + 1]
            return self._send(206, body, {"Content-Range": f"bytes {start}-{start + len(body) - 1}/{len(data)}", **base}, ctype)
        return self._send(200, data, base, ctype)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # clients hanging up mid-test is expected
        pass


class LocalServer:
    def __init__(self):
        self.httpd = QuietServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.requests = []
        self.httpd.page = ""
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}{path}"

    @property
    def requests(self):
        return self.httpd.requests


def _loopback_public(addr):
    return str(addr).split("%")[0] in ("127.0.0.1", "::1") or _REAL_IS_PUBLIC(addr)


class NetCase(unittest.TestCase):
    """Starts a local server and lets production network code reach it (and only it)."""

    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer().start()
        cls._stack = ExitStack()
        cls._stack.enter_context(patch.object(security, "is_public_ip", _loopback_public))
        cls._stack.enter_context(patch.object(security, "ALLOWED_PORTS", {None, 80, 443, cls.server.port}))
        cls._stack.enter_context(patch.object(settings, "retry_backoff", 0.01))

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()
        cls.server.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        self.server.httpd.requests.clear()
        self.server.httpd.flaky_hits = 0
