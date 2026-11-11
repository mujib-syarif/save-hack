import hashlib
import threading
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

from savehack.config import settings
from savehack.engines.base import JobContext
from savehack.engines.direct import DirectEngine, extension_for, kind_for, parse_content_disposition
from savehack.errors import AppError, Cancelled, ErrorCode

from tests.helpers import PAYLOAD, PAYLOAD_SHA, NetCase


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FilenameDetectionTests(NetCase):
    def test_content_disposition_rfc6266(self):
        self.assertEqual(parse_content_disposition("attachment; filename=\"report 2024.pdf\""), "report 2024.pdf")
        self.assertEqual(parse_content_disposition("attachment; filename*=UTF-8''la%20p%C3%B6rt%20%E2%82%AC.pdf"), "la pört €.pdf")
        self.assertEqual(parse_content_disposition("inline"), "")
        self.assertEqual(parse_content_disposition(""), "")

    def test_extension_from_content_type_when_name_has_none(self):
        self.assertEqual(extension_for("download", "video/mp4"), ".mp4")
        self.assertEqual(extension_for("download", "application/pdf; charset=binary"), ".pdf")
        self.assertEqual(extension_for("clip.WebM", "application/octet-stream"), ".webm")
        self.assertEqual(extension_for("blob", "application/octet-stream"), "")

    def test_kind_detection(self):
        self.assertEqual(kind_for(".mp4", ""), "video")
        self.assertEqual(kind_for("", "audio/mpeg"), "audio")
        self.assertEqual(kind_for(".png", ""), "image")
        self.assertEqual(kind_for(".zip", "application/zip"), "file")

    def test_probe_reads_disposition_filename_and_type(self):
        info = DirectEngine().analyze(self.server.url("/cd"))
        self.assertEqual(info.engine, "direct")
        self.assertEqual(info.formats[0].payload["filename"], "la pört €.pdf")
        self.assertEqual(info.formats[0].ext, "pdf")
        self.assertEqual(info.kind, "file")

    def test_probe_adds_extension_from_content_type(self):
        info = DirectEngine().analyze(self.server.url("/noext"))
        self.assertEqual(info.formats[0].payload["filename"], "noext.pdf")

    def test_probe_reports_size_and_range_support(self):
        info = DirectEngine().analyze(self.server.url("/video.mp4"))
        self.assertEqual(info.kind, "video")
        self.assertEqual(info.filesize, len(PAYLOAD))
        self.assertTrue(info.payload["accept_ranges"])
        self.assertEqual(info.formats[0].filesize, len(PAYLOAD))

    def test_html_page_is_not_a_file(self):
        self.assertIsNone(DirectEngine().analyze(self.server.url("/fake.mp4")))

    def test_http_errors_map_to_categories(self):
        for path, code in (("/error404", ErrorCode.MEDIA_NOT_FOUND), ("/error403", ErrorCode.SOURCE_BLOCKED), ("/error429", ErrorCode.RATE_LIMITED)):
            with self.assertRaises(AppError) as cm:
                DirectEngine().analyze(self.server.url(path))
            self.assertEqual(cm.exception.code, code, path)

    def test_connection_refused_is_network_error(self):
        with patch("savehack.security.ALLOWED_PORTS", {None, 80, 443, 1}):
            with self.assertRaises(AppError) as cm:
                DirectEngine().analyze("http://127.0.0.1:1/x.mp4")
        self.assertEqual(cm.exception.code, ErrorCode.NETWORK_ERROR)


class DownloadTests(NetCase):
    def run_download(self, path, ctx=None):
        eng = DirectEngine()
        info = eng.analyze(self.server.url(path))
        ctx = ctx or JobContext(workdir=self.workdir)
        out = eng.download(info, info.formats[0], ctx)
        return out, ctx

    def test_full_download_matches_source_and_reports_progress(self):
        out, ctx = self.run_download("/file.bin")
        self.assertEqual(sha(out), PAYLOAD_SHA)
        self.assertEqual(ctx.progress.downloaded, len(PAYLOAD))
        self.assertEqual(ctx.progress.total, len(PAYLOAD))
        self.assertEqual(ctx.progress.percent, 100.0)
        self.assertEqual([p.name for p in self.workdir.iterdir()], ["file.bin"])  # no leftover .part

    def test_download_through_redirect(self):
        out, _ = self.run_download("/redirect")
        self.assertEqual(sha(out), PAYLOAD_SHA)

    def test_interrupted_transfer_resumes_with_range_and_if_range(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/flaky.bin"))
        self.server.httpd.flaky_hits = 0  # the probe above used up a hit; make the first *download* connection die
        out = eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(sha(out), PAYLOAD_SHA)
        ranged = [h for p, h in self.server.requests if p == "/flaky.bin" and "Range" in h and h["Range"] != "bytes=0-0"]
        self.assertTrue(ranged, "retry should have sent a Range request")
        offset = int(ranged[0]["Range"][len("bytes="):-1])
        self.assertTrue(0 < offset <= 100_000, ranged[0]["Range"])  # resumed from what had been written, not from zero
        self.assertEqual(ranged[0].get("If-Range"), '"abc123"')

    def test_resume_from_existing_partial_file_across_job_retries(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        (self.workdir / "file.bin.part").write_bytes(PAYLOAD[:123_456])
        ctx = JobContext(workdir=self.workdir)
        out = eng.download(info, info.formats[0], ctx)
        self.assertEqual(sha(out), PAYLOAD_SHA)
        self.assertTrue(any(h.get("Range") == "bytes=123456-" for p, h in self.server.requests))

    def test_server_ignoring_range_restarts_cleanly(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/norange.bin"))
        (self.workdir / "norange.bin.part").write_bytes(b"stale-garbage" * 100)
        out = eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(sha(out), PAYLOAD_SHA)

    def test_partial_without_validator_is_not_resumed(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        info.payload["etag"] = None
        info.payload["last_modified"] = None
        (self.workdir / "file.bin.part").write_bytes(b"?" * 1000)
        ctx = JobContext(workdir=self.workdir)
        ctx.state.update(etag=None, last_modified=None)
        out = eng.download(info, info.formats[0], ctx)
        self.assertEqual(sha(out), PAYLOAD_SHA)
        self.assertFalse(any(h.get("Range", "").startswith("bytes=1000-") for p, h in self.server.requests))

    def test_retries_are_bounded(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        info.formats[0].payload["direct_url"] = self.server.url("/error404")
        with self.assertRaises(AppError) as cm:
            eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(cm.exception.code, ErrorCode.MEDIA_NOT_FOUND)
        self.assertEqual(len([p for p, _ in self.server.requests if p == "/error404"]), 1)  # non-retryable: one attempt

    def test_transient_server_error_is_retried_with_backoff(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        calls = []

        real_fetch = DirectEngine._fetch

        def flaky(self_, url, part, ctx, state):
            calls.append(1)
            if len(calls) < 3:
                raise AppError(ErrorCode.NETWORK_ERROR, detail="boom")
            return real_fetch(self_, url, part, ctx, state)

        with patch.object(DirectEngine, "_fetch", flaky):
            out = eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sha(out), PAYLOAD_SHA)

    def test_gives_up_after_max_retries(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        with patch.object(DirectEngine, "_fetch", side_effect=AppError(ErrorCode.NETWORK_ERROR, detail="down")) as f:
            with self.assertRaises(AppError):
                eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(f.call_count, settings.max_retries + 1)

    def test_declared_size_over_limit_is_rejected_before_downloading(self):
        with patch.object(settings, "max_download_mb", 1):
            eng = DirectEngine()
            info = eng.analyze(self.server.url("/big.bin"))
            with self.assertRaises(AppError) as cm:
                eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)
        self.assertFalse(any((self.workdir / "big.bin").exists() for _ in [0]))

    def test_undeclared_size_stream_is_cut_at_limit(self):
        with patch.object(settings, "max_download_mb", 1):
            eng = DirectEngine()
            info = eng.analyze(self.server.url("/chunked-endless.bin"))
            with self.assertRaises(AppError) as cm:
                eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)
        part = self.workdir / "chunked-endless.bin.part"
        self.assertLessEqual(part.stat().st_size, 1024 * 1024 + settings.chunk_size)

    def test_cancellation_stops_transfer_promptly(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/slow.bin"))
        ctx = JobContext(workdir=self.workdir)
        threading.Timer(0.3, ctx.cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            eng.download(info, info.formats[0], ctx)
        self.assertLess(time.monotonic() - t0, 2.0)  # the full transfer would take ~2.5 s

    def test_cancel_during_backoff_wait(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        ctx = JobContext(workdir=self.workdir)
        with patch.object(settings, "retry_backoff", 30.0), patch.object(DirectEngine, "_fetch", side_effect=AppError(ErrorCode.NETWORK_ERROR, detail="x")):
            threading.Timer(0.2, ctx.cancel.set).start()
            t0 = time.monotonic()
            with self.assertRaises(Cancelled):
                eng.download(info, info.formats[0], ctx)
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_expired_deadline_aborts(self):
        eng = DirectEngine()
        info = eng.analyze(self.server.url("/file.bin"))
        ctx = JobContext(workdir=self.workdir, deadline=time.monotonic() - 1)
        with self.assertRaises(AppError) as cm:
            eng.download(info, info.formats[0], ctx)
        self.assertEqual(cm.exception.code, ErrorCode.EXTRACTION_TIMEOUT)

    def test_large_file_streams_without_buffering_in_memory(self):
        from tests import helpers
        big = b"\x01" * (24 * 1024 * 1024)
        with patch.object(helpers, "PAYLOAD", big), patch.object(settings, "max_download_mb", 64):
            eng = DirectEngine()
            info = eng.analyze(self.server.url("/file.bin"))
            tracemalloc.start()
            try:
                out = eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(out.stat().st_size, len(big))
        self.assertLess(peak, 6 * 1024 * 1024, f"peak traced memory {peak / 1e6:.1f} MB suggests the file was buffered")


if __name__ == "__main__":
    import unittest
    unittest.main()
