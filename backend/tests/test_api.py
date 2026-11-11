"""HTTP-level tests. Require fastapi + httpx (installed in CI); skipped where they are unavailable."""
import hashlib
import time
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from savehack import api
    HAVE_API = True
except Exception:  # pragma: no cover - depends on the environment
    HAVE_API = False

from savehack.ratelimit import RateLimiter
from tests.helpers import PAYLOAD_SHA, NetCase


@unittest.skipUnless(HAVE_API, "fastapi/httpx not installed")
class ApiTests(NetCase):
    def setUp(self):
        super().setUp()
        self._ctx = TestClient(api.app)
        self.client = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        for name, lim in (("ANALYZE_LIMIT", RateLimiter(100)), ("DOWNLOAD_LIMIT", RateLimiter(100))):
            p = patch.object(api, name, lim)
            p.start()
            self.addCleanup(p.stop)
        api.ROUTER._cache.clear()

    def analyze(self, path, **kw):
        return self.client.post("/api/analyze", json={"url": self.server.url(path), "rights_confirmed": True}, **kw)

    def wait_done(self, job_id, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = self.client.get(f"/api/jobs/{job_id}").json()
            if data["status"] in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        for key in ("version", "yt_dlp_version", "ffmpeg", "engines", "storage", "limits"):
            self.assertIn(key, body)
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")

    def test_analyze_requires_rights_confirmation(self):
        r = self.client.post("/api/analyze", json={"url": self.server.url("/file.bin"), "rights_confirmed": False})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "INVALID_URL")
        self.assertFalse(r.json()["ok"])

    def test_malformed_body_returns_structured_error(self):
        r = self.client.post("/api/analyze", json={"nope": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "INVALID_URL")

    def test_private_url_is_rejected(self):
        for u in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.2/x.mp4", "file:///etc/passwd", "https://localhost/a.mp4"):
            r = self.client.post("/api/analyze", json={"url": u, "rights_confirmed": True})
            self.assertEqual(r.status_code, 400, u)
            self.assertEqual(r.json()["error"]["code"], "INVALID_URL", u)

    def test_analyze_response_shape_and_no_private_fields(self):
        r = self.analyze("/video.mp4")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["media"]["engine"], "direct")
        self.assertEqual(body["formats"][0]["id"], "file")
        self.assertEqual(body["default_format"], "file")
        text = r.text
        for leak in ("direct_url", "payload", "selector", "etag"):
            self.assertNotIn(leak, text)

    def test_full_flow_analyze_download_poll_fetch(self):
        job_id = self.analyze("/file.bin").json()["job_id"]
        r = self.client.post("/api/download", json={"job_id": job_id, "format_id": "file"})
        self.assertEqual(r.status_code, 202)
        done = self.wait_done(job_id)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["progress"]["percent"], 100.0)
        f = self.client.get(f"/api/download/{job_id}")
        self.assertEqual(f.status_code, 200)
        self.assertEqual(hashlib.sha256(f.content).hexdigest(), PAYLOAD_SHA)
        self.assertIn("attachment", f.headers.get("content-disposition", "").lower())

    def test_fetch_before_ready_is_409_json(self):
        job_id = self.analyze("/file.bin").json()["job_id"]
        r = self.client.get(f"/api/download/{job_id}")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "MEDIA_NOT_FOUND")

    def test_unknown_job_and_unknown_format(self):
        self.assertEqual(self.client.get("/api/jobs/" + "0" * 32).status_code, 404)
        job_id = self.analyze("/file.bin").json()["job_id"]
        r = self.client.post("/api/download", json={"job_id": job_id, "format_id": "sneaky"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "FORMAT_UNAVAILABLE")

    def test_client_cannot_inject_its_own_download_target(self):
        job_id = self.analyze("/file.bin").json()["job_id"]
        r = self.client.post("/api/download", json={"job_id": job_id, "format_id": "file", "url": "http://169.254.169.254/", "selector": "x"})
        self.assertEqual(r.status_code, 202)  # extra fields ignored; target stays the analysed one
        self.wait_done(job_id)
        self.assertFalse(any("169.254" in p for p, _ in self.server.requests))

    def test_delete_cancels_and_removes(self):
        job_id = self.analyze("/slow.bin").json()["job_id"]
        self.client.post("/api/download", json={"job_id": job_id, "format_id": "file"})
        time.sleep(0.2)
        r = self.client.delete(f"/api/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

    def test_failed_download_reports_error_without_internals(self):
        job_id = self.analyze("/file.bin").json()["job_id"]
        api.MANAGER.get(job_id).info.formats[0].payload["direct_url"] = self.server.url("/error404")
        self.client.post("/api/download", json={"job_id": job_id, "format_id": "file"})
        done = self.wait_done(job_id)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "MEDIA_NOT_FOUND")
        self.assertNotIn("127.0.0.1", str(done))

    def test_analyze_rate_limit(self):
        with patch.object(api, "ANALYZE_LIMIT", RateLimiter(2)):
            codes = [self.analyze("/video.mp4").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_rate_limit_body_is_structured(self):
        with patch.object(api, "ANALYZE_LIMIT", RateLimiter(1)):
            self.analyze("/video.mp4")
            r = self.analyze("/video.mp4")
        self.assertEqual(r.json()["error"]["code"], "RATE_LIMITED")
        self.assertTrue(r.json()["error"]["retryable"])

    def test_client_ip_uses_trusted_forwarded_hop_only(self):
        class Req:
            client = type("C", (), {"host": "10.9.9.9"})()
            headers = {"x-forwarded-for": "6.6.6.6, 1.2.3.4"}
        with patch.object(api.settings, "trusted_proxy_hops", 1):
            self.assertEqual(api.client_ip(Req()), "1.2.3.4")  # a spoofed left-most entry is ignored
        with patch.object(api.settings, "trusted_proxy_hops", 0):
            self.assertEqual(api.client_ip(Req()), "10.9.9.9")

    def test_unsafe_content_types_are_served_as_opaque_downloads(self):
        job_id = self.analyze("/file.bin").json()["job_id"]
        self.client.post("/api/download", json={"job_id": job_id, "format_id": "file"})
        self.wait_done(job_id)
        job = api.MANAGER.get(job_id)
        html_path = job.result_path.with_name("evil.html")
        job.result_path.rename(html_path)
        job.result_path, job.result_name = html_path, "evil.html"
        r = self.client.get(f"/api/download/{job_id}")
        self.assertEqual(r.headers["content-type"], "application/octet-stream")

    def test_cors_preflight_and_origin_header(self):
        r = self.client.options("/api/analyze", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("access-control-allow-origin", r.headers)

    def test_legacy_analyze_and_download_still_work(self):
        r = self.client.post("/analyze", json={"url": self.server.url("/video.mp4"), "rights_confirmed": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["formats"], ["mp4", "mp3"])
        d = self.client.post("/download", json={"url": self.server.url("/video.mp4"), "rights_confirmed": True, "format": "mp4"})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(hashlib.sha256(d.content).hexdigest(), PAYLOAD_SHA)

    def test_legacy_download_mp3_without_audio_is_clean_error(self):
        d = self.client.post("/download", json={"url": self.server.url("/video.mp4"), "rights_confirmed": True, "format": "mp3"})
        self.assertEqual(d.status_code, 422)
        self.assertEqual(d.json()["error"]["code"], "FORMAT_UNAVAILABLE")

    def test_legacy_download_error_has_detail_field_for_old_clients(self):
        d = self.client.post("/download", json={"url": "http://169.254.169.254/x.mp4", "rights_confirmed": True, "format": "mp4"})
        self.assertEqual(d.status_code, 400)
        self.assertIsInstance(d.json()["detail"], str)


if __name__ == "__main__":
    unittest.main()
