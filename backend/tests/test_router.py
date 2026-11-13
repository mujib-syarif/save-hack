import threading
import time
import unittest
from unittest.mock import patch

from savehack.config import settings
from savehack.engines import ytdlp_engine
from savehack.engines.direct import DirectEngine
from savehack.engines.html_parser import HtmlEngine
from savehack.engines.streams import StreamEngine
from savehack.errors import AppError, ErrorCode
from savehack.models import FormatOption, MediaInfo
from savehack.router import Router

from tests.helpers import NetCase


class FakeYtDlp:
    """Stands in for the yt-dlp engine: claims only hosts listed in `claims`."""
    name = "ytdlp"

    def __init__(self, claims=(), error=None):
        self.claims, self.error, self.calls = claims, error, []

    def analyze(self, url):
        self.calls.append(url)
        if not any(c in url for c in self.claims):
            return None
        if self.error:
            raise self.error
        return MediaInfo(url=url, engine="ytdlp", site="Fake", title="from ytdlp", formats=[FormatOption(id="mp4-best", label="x", kind="video", payload={"downloader": "ytdlp"})])


def make_router(ytdlp=None):
    direct = DirectEngine()
    streams = StreamEngine()
    return Router(direct=direct, ytdlp=ytdlp or FakeYtDlp(), streams=streams, html=HtmlEngine(direct, streams))


class RouterTests(NetCase):
    def setUp(self):
        super().setUp()
        p = patch.object(ytdlp_engine, "available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_direct_file_url_uses_direct_engine_first(self):
        yt = FakeYtDlp(claims=("127.0.0.1",))
        info = make_router(yt).analyze(self.server.url("/video.mp4"))
        self.assertEqual(info.engine, "direct")
        self.assertEqual(yt.calls, [])  # never reached
        self.assertEqual(info.attempts, [{"engine": "direct", "outcome": "success"}])

    def test_manifest_url_uses_stream_engine(self):
        self.assertEqual(make_router().analyze(self.server.url("/master.m3u8")).engine, "stream")

    def test_ytdlp_claimed_url_wins_over_html_parsing(self):
        yt = FakeYtDlp(claims=("127.0.0.1",))
        self.server.httpd.page = '<video src="/video.mp4"></video>'
        info = make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(info.engine, "ytdlp")

    def test_unclaimed_page_falls_through_to_html_parser(self):
        self.server.httpd.page = '<title>P</title><video src="/video.mp4"></video>'
        yt = FakeYtDlp(claims=())
        info = make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(info.engine, "html")
        self.assertEqual([a["engine"] for a in info.attempts], ["ytdlp", "html"])
        self.assertEqual(info.attempts[0]["outcome"], "not_applicable")

    def test_claimed_but_failed_url_never_falls_back_to_html(self):
        """A poster/og:image or unrelated link must not be returned when the real extractor says login is required."""
        self.server.httpd.page = '<video src="/video.mp4"></video>'
        yt = FakeYtDlp(claims=("127.0.0.1",), error=AppError(ErrorCode.AUTHENTICATION_REQUIRED, detail="private"))
        with self.assertRaises(AppError) as cm:
            make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(cm.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)
        self.assertEqual([p for p, _ in self.server.requests if p == "/video.mp4"], [])

    def test_non_html_url_without_extension_is_downloaded_as_direct(self):
        info = make_router().analyze(self.server.url("/cd"))
        self.assertEqual(info.engine, "direct")
        self.assertEqual(info.formats[0].ext, "pdf")

    def test_generic_ytdlp_fallback_runs_after_html_parser_finds_nothing(self):
        class GenericYt(FakeYtDlp):
            def __init__(self):
                super().__init__(claims=())
                self.generic_calls = []
            def analyze_generic(self, url):
                self.generic_calls.append(url)
                return MediaInfo(url=url, engine="ytdlp", site="Generic", title="generic media",
                                 formats=[FormatOption(id="mp4-best", label="video", kind="video",
                                                       payload={"downloader": "ytdlp"})])
        self.server.httpd.page = "<title>JS player</title><p>player bootstraps in script</p>"
        yt = GenericYt()
        info = make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(info.engine, "ytdlp")
        self.assertEqual(yt.generic_calls, [self.server.url("/page.html")])
        self.assertEqual([a["engine"] for a in info.attempts], ["ytdlp", "html", "ytdlp_generic"])

    def test_generic_fallback_is_not_used_after_dedicated_extractor_blocks(self):
        class GenericYt(FakeYtDlp):
            def __init__(self):
                super().__init__(claims=("127.0.0.1",),
                                 error=AppError(ErrorCode.SOURCE_BLOCKED, detail="HTTP 403"))
                self.generic_calls = 0
            def analyze_generic(self, url):
                self.generic_calls += 1
                return None
        yt = GenericYt()
        self.server.httpd.page = '<video src="/video.mp4"></video>'
        with self.assertRaises(AppError) as cm:
            make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(cm.exception.code, ErrorCode.SOURCE_BLOCKED)
        self.assertEqual(yt.generic_calls, 0)
        self.assertEqual([p for p, _ in self.server.requests if p == "/video.mp4"], [])

    def test_page_with_nothing_is_unsupported_with_diagnostics(self):
        self.server.httpd.page = "<title>x</title><p>nothing</p>"
        with self.assertRaises(AppError) as cm:
            make_router().analyze(self.server.url("/page.html"))
        self.assertEqual(cm.exception.code, ErrorCode.UNSUPPORTED_SOURCE)

    def test_http_403_is_reported_as_blocked_not_unsupported(self):
        with self.assertRaises(AppError) as cm:
            make_router().analyze(self.server.url("/error403"))
        self.assertEqual(cm.exception.code, ErrorCode.SOURCE_BLOCKED)

    def test_private_url_is_rejected_before_any_engine_runs(self):
        yt = FakeYtDlp(claims=("169.254",))
        with self.assertRaises(AppError) as cm:
            make_router(yt).analyze("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_URL)
        self.assertEqual(yt.calls, [])

    def test_embedded_player_pointing_at_private_address_is_not_followed(self):
        self.server.httpd.page = '<iframe src="http://169.254.169.254/embed/1"></iframe><iframe src="http://10.0.0.5/x"></iframe>'
        yt = FakeYtDlp(claims=("169.254", "10.0.0.5"))
        with self.assertRaises(AppError):
            make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual([c for c in yt.calls if "169.254" in c or "10.0.0.5" in c], [])

    def test_embedded_player_is_routed_through_ytdlp(self):
        self.server.httpd.page = f'<iframe src="{self.server.url("/embed/7")}"></iframe>'

        class EmbedYt(FakeYtDlp):
            def analyze(s, url):
                s.calls.append(url)
                return super().analyze(url) if url.endswith("/embed/7") else None

        yt = EmbedYt(claims=("/embed/7",))
        info = make_router(yt).analyze(self.server.url("/page.html"))
        self.assertEqual(info.engine, "ytdlp")
        self.assertEqual(info.url, self.server.url("/page.html"))

    def test_results_are_cached_and_identical_requests_share_one_extraction(self):
        r = make_router()
        a = r.analyze(self.server.url("/video.mp4"))
        n = len(self.server.requests)
        b = r.analyze(self.server.url("/video.mp4"))
        self.assertIs(a, b)
        self.assertEqual(len(self.server.requests), n)

    def test_concurrent_identical_requests_do_not_repeat_extraction(self):
        r = make_router()
        calls = []
        real = r._pipeline

        def slow(url):
            calls.append(url)
            time.sleep(0.3)
            return real(url)

        r._pipeline = slow
        with patch.object(settings, "analysis_cache_ttl", 0):
            out = []
            ts = [threading.Thread(target=lambda: out.append(r.analyze(self.server.url("/video.mp4")))) for _ in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(out), 4)

    def test_analysis_timeout(self):
        r = make_router()
        r._pipeline = lambda url: time.sleep(2)
        with patch.object(settings, "analyze_timeout", 0.2), patch.object(settings, "analysis_cache_ttl", 0):
            with self.assertRaises(AppError) as cm:
                r.analyze(self.server.url("/video.mp4"))
        self.assertEqual(cm.exception.code, ErrorCode.EXTRACTION_TIMEOUT)

    def test_unexpected_engine_crash_becomes_safe_error(self):
        r = make_router()
        r._pipeline = lambda url: (_ for _ in ()).throw(RuntimeError("secret internal path /srv/app"))
        with patch.object(settings, "analysis_cache_ttl", 0):
            with self.assertRaises(AppError) as cm:
                r.analyze(self.server.url("/video.mp4"))
        self.assertNotIn("secret", cm.exception.message)
        self.assertNotIn("secret", str(cm.exception.to_public()))

    def test_download_dispatches_by_option_downloader(self):
        r = make_router()
        seen = []
        r.direct.download = lambda i, f, c: seen.append("direct") or "d"
        r.ytdlp.download = lambda i, f, c: seen.append("ytdlp") or "y"
        r.streams.download = lambda i, f, c: seen.append("stream") or "s"
        info = MediaInfo(url="u", engine="x", site="s", title="t")
        for d in ("direct", "ytdlp", "stream"):
            r.download(info, FormatOption(id="a", label="a", kind="file", payload={"downloader": d}), None)
        self.assertEqual(seen, ["direct", "ytdlp", "stream"])
        with self.assertRaises(AppError):
            r.download(info, FormatOption(id="a", label="a", kind="file", payload={"downloader": "nope"}), None)


if __name__ == "__main__":
    unittest.main()
