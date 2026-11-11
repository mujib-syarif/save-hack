import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from savehack.config import settings
from savehack.engines import ytdlp_engine
from savehack.engines.base import JobContext
from savehack.errors import AppError, Cancelled, ErrorCode, classify_exception, classify_message

import tempfile


class ErrorMappingTests(unittest.TestCase):
    CASES = {
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot": ErrorCode.SOURCE_BLOCKED,
        "ERROR: This video is DRM protected": ErrorCode.SOURCE_BLOCKED,
        "ERROR: Private video. Sign in if you've been granted access": ErrorCode.AUTHENTICATION_REQUIRED,
        "ERROR: Login required to view this": ErrorCode.AUTHENTICATION_REQUIRED,
        "ERROR: Unsupported URL: https://x.example.com/": ErrorCode.UNSUPPORTED_SOURCE,
        "ERROR: Requested format is not available": ErrorCode.FORMAT_UNAVAILABLE,
        "HTTP Error 404: Not Found": ErrorCode.MEDIA_NOT_FOUND,
        "Video unavailable": ErrorCode.MEDIA_NOT_FOUND,
        "HTTP Error 429: Too Many Requests": ErrorCode.RATE_LIMITED,
        "HTTP Error 403: Forbidden": ErrorCode.SOURCE_BLOCKED,
        "The read operation timed out": ErrorCode.EXTRACTION_TIMEOUT,
        "ffmpeg exited with code 1": ErrorCode.CONVERSION_FAILED,
        "No space left on device": ErrorCode.STORAGE_LIMIT,
        "Unable to download webpage: Connection reset": ErrorCode.NETWORK_ERROR,
    }

    def test_message_classification(self):
        for text, code in self.CASES.items():
            self.assertEqual(classify_message(text), code, text)

    def test_unknown_uses_fallback(self):
        self.assertEqual(classify_message("something odd"), ErrorCode.DOWNLOAD_FAILED)
        self.assertEqual(classify_message("something odd", ErrorCode.SERVER_ERROR), ErrorCode.SERVER_ERROR)

    def test_exception_types(self):
        self.assertEqual(classify_exception(socket.timeout()).code, ErrorCode.EXTRACTION_TIMEOUT)
        self.assertEqual(classify_exception(socket.gaierror(-2, "x")).code, ErrorCode.NETWORK_ERROR)
        self.assertEqual(classify_exception(ConnectionResetError()).code, ErrorCode.NETWORK_ERROR)
        self.assertEqual(classify_exception(OSError(28, "No space left")).code, ErrorCode.STORAGE_LIMIT)
        err = AppError(ErrorCode.INVALID_URL)
        self.assertIs(classify_exception(err), err)

    def test_public_form_has_no_technical_detail_or_traceback(self):
        err = classify_exception(RuntimeError("/srv/app/secret.py line 42 Traceback token=abc"))
        pub = str(err.to_public())
        for leak in ("/srv/app", "Traceback", "token=abc", "RuntimeError"):
            self.assertNotIn(leak, pub)
        self.assertIn("RuntimeError", err.detail)  # kept for server-side logs

    def test_every_code_has_message_and_status(self):
        from savehack.errors import DEFAULT_MESSAGES, HTTP_STATUS
        for code in ErrorCode:
            self.assertIn(code, DEFAULT_MESSAGES)
            self.assertIn(code, HTTP_STATUS)
        self.assertEqual(len(ErrorCode), 13)

    def test_retryable_flags(self):
        self.assertTrue(AppError(ErrorCode.NETWORK_ERROR).retryable)
        self.assertFalse(AppError(ErrorCode.AUTHENTICATION_REQUIRED).retryable)
        self.assertFalse(AppError(ErrorCode.INVALID_URL).retryable)


FIXTURE = {
    "title": "Demo", "uploader": "Chan", "duration": 100, "thumbnail": "https://i.example.com/t.jpg", "extractor_key": "Demo",
    "subtitles": {"en": [{}], "id": [{}], "live_chat": [{}], "../evil": [{}]},
    "formats": [
        {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "vcodec": "none", "abr": 128, "filesize": 1_000_000},
        {"format_id": "251", "ext": "webm", "acodec": "opus", "vcodec": "none", "abr": 160, "filesize": 1_200_000},
        {"format_id": "18", "ext": "mp4", "acodec": "mp4a.40.2", "vcodec": "avc1.42001E", "height": 360, "width": 640, "tbr": 500, "filesize": 5_000_000},
        {"format_id": "137", "ext": "mp4", "acodec": "none", "vcodec": "avc1.640028", "height": 1080, "width": 1920, "tbr": 4000, "filesize": 40_000_000},
        {"format_id": "248", "ext": "webm", "acodec": "none", "vcodec": "vp9", "height": 1080, "width": 1920, "tbr": 2500, "filesize": 30_000_000},
        {"format_id": "136", "ext": "mp4", "acodec": "none", "vcodec": "avc1.4d401f", "height": 720, "width": 1280, "tbr": 2000, "filesize": 20_000_000},
    ],
}


class BuildOptionsTests(unittest.TestCase):
    def setUp(self):
        self.opts = ytdlp_engine.build_options(FIXTURE)
        self.by_id = {o.id: o for o in self.opts}

    def test_expected_options(self):
        self.assertEqual([o.id for o in self.opts], ["mp4-best", "mp4-1080", "mp4-720", "mp4-360", "mp3", "audio-best", "sub-en", "sub-id"])

    def test_best_adds_audio_size_and_prefers_h264(self):
        best = self.by_id["mp4-best"]
        self.assertEqual(best.height, 1080)
        self.assertEqual(best.filesize, 40_000_000 + 1_000_000)  # video + best audio stream
        self.assertEqual(best.vcodec, "avc1")
        self.assertEqual(best.acodec, "mp4a")
        self.assertEqual(self.by_id["mp4-1080"].vcodec, "avc1")  # chose avc1 over vp9 at 1080p

    def test_muxed_format_does_not_double_count_audio(self):
        self.assertEqual(self.by_id["mp4-360"].filesize, 5_000_000)

    def test_audio_options(self):
        self.assertEqual(self.by_id["mp3"].ext, "mp3")
        self.assertTrue(self.by_id["mp3"].payload["mp3"])
        self.assertEqual(self.by_id["audio-best"].ext, "m4a")

    def test_subtitle_languages_are_validated(self):
        ids = [o.id for o in self.opts if o.kind == "subtitle"]
        self.assertEqual(ids, ["sub-en", "sub-id"])  # live_chat and a path-traversal-looking key are dropped

    def test_public_form_hides_engine_private_fields(self):
        pub = self.by_id["mp4-best"].to_public()
        self.assertNotIn("selector", pub)
        self.assertNotIn("payload", pub)

    def test_no_format_list_still_offers_mp4_and_mp3(self):
        ids = [o.id for o in ytdlp_engine.build_options({"title": "x"})]
        self.assertEqual(ids, ["mp4-best", "mp3"])

    def test_selectors(self):
        self.assertTrue(ytdlp_engine.mp4_selector().startswith("bv*[vcodec^=avc1]+ba[acodec^=mp4a]"))
        self.assertIn("[height<=720]", ytdlp_engine.mp4_selector(720))
        self.assertTrue(ytdlp_engine.mp4_selector().endswith("/b"))
        self.assertEqual(ytdlp_engine.stream_selector(None, "mp3"), "ba/b")
        self.assertIn("[height<=480]", ytdlp_engine.stream_selector(480))

    def test_playlist_media_info_is_bounded_and_filtered(self):
        info = {"_type": "playlist", "title": "PL", "entries": [{"title": "a", "url": "https://x.example.com/1"}, None, {"title": "b", "url": "javascript:x"}] + [{"title": str(i), "url": f"https://x.example.com/{i}"} for i in range(200)]}
        m = ytdlp_engine.build_media_info("https://x.example.com/pl", info)
        self.assertEqual(m.kind, "playlist")
        self.assertLessEqual(len(m.entries), settings.max_playlist_entries)
        self.assertTrue(all(e["url"].startswith("https://") for e in m.entries))

    def test_non_http_thumbnail_is_dropped(self):
        m = ytdlp_engine.build_media_info("https://x.example.com/v", {"title": "t", "thumbnail": "file:///etc/passwd"})
        self.assertIsNone(m.thumbnail)


class DownloadOptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = JobContext(workdir=Path(self.tmp.name))
        self.hooks = {"progress": lambda d: None, "post": lambda d: None}

    def test_mp4_options(self):
        o = ytdlp_engine.build_download_opts(self.ctx, ytdlp_engine.mp4_selector(), False, None, self.hooks)
        self.assertEqual(o["merge_output_format"], "mp4")
        self.assertEqual(o["concurrent_fragment_downloads"], settings.fragment_concurrency)
        self.assertEqual(o["max_filesize"], settings.max_download_bytes)
        self.assertTrue(o["ignoreconfig"])
        self.assertFalse(o["noplaylist"] is False)
        self.assertEqual(o["paths"]["home"], self.tmp.name)
        self.assertNotIn("cookiefile", o)
        self.assertNotIn("cookiesfrombrowser", o)
        self.assertNotIn("username", o)
        self.assertFalse(o["geo_bypass"])
        self.assertTrue(o["hls_prefer_native"])  # no external ffmpeg/aria2 fetching: it would bypass the egress guard
        self.assertEqual(o["external_downloader"], {"default": "native"})
        self.assertEqual(o["retries"], settings.max_retries)

    def test_mp3_options(self):
        o = ytdlp_engine.build_download_opts(self.ctx, "ba/b", True, None, self.hooks)
        self.assertEqual(o["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertNotIn("merge_output_format", o)

    def test_subtitle_options(self):
        o = ytdlp_engine.build_download_opts(self.ctx, "", False, "en", self.hooks)
        self.assertTrue(o["skip_download"])
        self.assertEqual(o["subtitleslangs"], ["en"])

    def test_backoff_is_exponential_and_capped(self):
        with patch.object(settings, "retry_backoff", 1.0):
            self.assertEqual([ytdlp_engine._backoff(n) for n in (1, 2, 3, 4)], [1.0, 2.0, 4.0, 8.0])
            self.assertEqual(ytdlp_engine._backoff(30), 20.0)

    def test_pick_output_prefers_final_file_over_artifacts(self):
        d = Path(self.tmp.name)
        for n, size in (("a.mp4", 100), ("a.f137.mp4.part", 5000), ("a.jpg", 9000), ("a.info.json", 50)):
            (d / n).write_bytes(b"x" * size)
        self.assertEqual(ytdlp_engine.pick_output(d, False, False).name, "a.mp4")
        (d / "a.mp3").write_bytes(b"x")
        self.assertEqual(ytdlp_engine.pick_output(d, True, False).name, "a.mp3")
        self.assertIsNone(ytdlp_engine.pick_output(Path(tempfile.mkdtemp()), False, False))


def install_fake_yt_dlp(behaviour):
    """Install a minimal fake `yt_dlp` so run_ytdlp_download can be exercised without the real package."""
    mod = types.ModuleType("yt_dlp")
    utils = types.ModuleType("yt_dlp.utils")

    class DownloadCancelled(Exception):
        pass

    utils.DownloadCancelled = DownloadCancelled

    class YDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            behaviour(self.opts, urls)

    mod.YoutubeDL = YDL
    mod.utils = utils
    mod.version = types.SimpleNamespace(__version__="fake")
    return patch.dict(sys.modules, {"yt_dlp": mod, "yt_dlp.utils": utils})


class RunDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = JobContext(workdir=Path(self.tmp.name))

    def test_success_returns_output_and_updates_progress(self):
        def beh(opts, urls):
            Path(opts["paths"]["home"], "v.mp4").write_bytes(b"x" * 1000)
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000, "filename": "v.f1.mp4", "speed": 100.0, "eta": 5})
            opts["progress_hooks"][0]({"status": "finished", "downloaded_bytes": 1000, "total_bytes": 1000, "filename": "v.f1.mp4"})
        with install_fake_yt_dlp(beh):
            out = ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertEqual(out.name, "v.mp4")
        self.assertEqual(self.ctx.progress.stage, "done")
        self.assertEqual(self.ctx.progress.total, 1000)

    def test_cancel_inside_hook_aborts_with_cancelled(self):
        def beh(opts, urls):
            self.ctx.cancel.set()
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "filename": "a"})
        with install_fake_yt_dlp(beh):
            with self.assertRaises(Cancelled):
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)

    def test_progress_over_size_limit_aborts_with_storage_limit(self):
        def beh(opts, urls):
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": settings.max_download_bytes + 1, "filename": "a"})
        with install_fake_yt_dlp(beh):
            with self.assertRaises(AppError) as cm:
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)

    def test_estimated_total_over_limit_aborts_early(self):
        def beh(opts, urls):
            opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 10, "total_bytes_estimate": settings.max_download_bytes * 2, "filename": "a"})
        with install_fake_yt_dlp(beh):
            with self.assertRaises(AppError) as cm:
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertEqual(cm.exception.code, ErrorCode.STORAGE_LIMIT)

    def test_engine_exceptions_are_classified_not_leaked(self):
        def beh(opts, urls):
            raise RuntimeError("ERROR: Sign in to confirm you're not a bot /srv/secret")
        with install_fake_yt_dlp(beh):
            with self.assertRaises(AppError) as cm:
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertEqual(cm.exception.code, ErrorCode.SOURCE_BLOCKED)
        self.assertNotIn("/srv/secret", cm.exception.message)

    def test_ffmpeg_failure_is_conversion_error(self):
        def beh(opts, urls):
            raise RuntimeError("Postprocessing: ffmpeg exited with code 1")
        with install_fake_yt_dlp(beh):
            with self.assertRaises(AppError) as cm:
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx, audio_mp3=True)
        self.assertEqual(cm.exception.code, ErrorCode.CONVERSION_FAILED)

    def test_no_output_file_is_an_error(self):
        with install_fake_yt_dlp(lambda o, u: None):
            with self.assertRaises(AppError) as cm:
                ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertEqual(cm.exception.code, ErrorCode.DOWNLOAD_FAILED)

    def test_runs_under_egress_guard(self):
        from savehack import security
        seen = {}
        def beh(opts, urls):
            seen["depth"] = security.guard_depth()
            Path(opts["paths"]["home"], "v.mp4").write_bytes(b"x")
        with install_fake_yt_dlp(beh):
            ytdlp_engine.run_ytdlp_download("https://x.example.com/v", "b", self.ctx)
        self.assertGreater(seen["depth"], 0)
        self.assertEqual(security.guard_depth(), 0)


class ExtractorMatchingTests(unittest.TestCase):
    def test_registry_is_used_and_generic_is_ignored(self):
        class IE:
            def __init__(self, key, host):
                self._k, self._h = key, host
            def ie_key(self):
                return self._k
            def suitable(self, url):
                return self._h in url
        classes = [IE("Generic", ""), IE("Vimeo", "vimeo.com"), IE("Broken", None)]
        classes[2].suitable = lambda url: 1 / 0
        ytdlp_engine._extractor_classes.cache_clear()
        with patch.object(ytdlp_engine, "_extractor_classes", return_value=[c for c in classes if c.ie_key() != "Generic"]):
            self.assertEqual(ytdlp_engine.match_extractor("https://vimeo.com/1"), "Vimeo")
            self.assertIsNone(ytdlp_engine.match_extractor("https://random.example.com/page"))


if __name__ == "__main__":
    unittest.main()
