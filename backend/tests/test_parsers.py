import unittest

from savehack.engines.html_parser import HtmlEngine, NotHtml, parse_html, parse_iso_duration
from savehack.engines.streams import StreamEngine, build_formats, parse_dash, parse_hls, split_codecs
from savehack.errors import AppError, ErrorCode

from tests.helpers import DASH_MPD, HLS_MASTER, NetCase

PAGE = """<!doctype html><html><head><title>Fallback title</title>
<meta property="og:title" content="OG Title"><meta property="og:site_name" content="Demo Site">
<meta property="og:image" content="/thumb.jpg"><meta property="og:video" content="/video.mp4">
<meta name="twitter:player" content="https://player.example.com/embed/1">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"VideoObject","name":"LD Name","duration":"PT1H2M3S","thumbnailUrl":"https://cdn.example.com/t.png","contentUrl":"https://cdn.example.com/ld.mp4"}</script>
</head><body>
<video poster="/poster.jpg"><source src="/video.mp4" type="video/mp4"><source src="/live/master.m3u8" type="application/x-mpegURL"><track src="/en.vtt" srclang="en"></video>
<audio src="/a.mp3"></audio>
<img src="/pic.png" alt="pic"><img src="/sprite-icon.png"><img src="/tiny.png" width="16" height="16">
<a href="/docs/manual.pdf">Manual</a><a href="/about">About</a><a href="mailto:x@y.z">mail</a>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<script>var cfg={"file":"https:\\/\\/cdn.example.com\\/inline.webm"};</script>
</body></html>"""


class HtmlParseTests(unittest.TestCase):
    def setUp(self):
        self.page = parse_html(PAGE, "https://demo.example.com/watch/1")
        self.urls = {c.url: c for c in self.page.candidates}

    def test_metadata(self):
        self.assertEqual(self.page.title, "OG Title")
        self.assertEqual(self.page.site_name, "Demo Site")
        self.assertEqual(self.page.duration, 3723.0)
        self.assertTrue(self.page.thumbnail.startswith("https://"))

    def test_collects_media_from_all_sources(self):
        for u in ("https://demo.example.com/video.mp4", "https://cdn.example.com/ld.mp4", "https://demo.example.com/a.mp3",
                  "https://demo.example.com/live/master.m3u8", "https://cdn.example.com/inline.webm", "https://demo.example.com/docs/manual.pdf",
                  "https://demo.example.com/pic.png"):
            self.assertIn(u, self.urls, u)

    def test_stream_candidate_is_classified(self):
        self.assertEqual(self.urls["https://demo.example.com/live/master.m3u8"].kind, "stream")

    def test_ignores_non_media_links_icons_and_tiny_images(self):
        joined = " ".join(self.urls)
        for bad in ("/about", "mailto:", "sprite-icon", "tiny.png"):
            self.assertNotIn(bad, joined)

    def test_og_image_is_thumbnail_only_not_a_download(self):
        self.assertNotIn("https://demo.example.com/thumb.jpg", self.urls)

    def test_embeds_and_subtitles(self):
        self.assertIn("https://www.youtube.com/embed/abc", self.page.embeds)
        self.assertIn("https://player.example.com/embed/1", self.page.embeds)
        self.assertEqual(self.page.subtitles, [("en", "https://demo.example.com/en.vtt")])

    def test_candidates_are_deduplicated(self):
        self.assertEqual(len([c for c in self.page.candidates if c.url.endswith("/video.mp4")]), 1)

    def test_malformed_and_hostile_markup_does_not_crash(self):
        for html in ("", "<video><source", "<script type='application/ld+json'>{bad json</script>", "<a href='http://[::1'>x</a>", "\x00" * 100):
            parse_html(html, "https://x.example.com/")

    def test_javascript_and_data_urls_are_ignored(self):
        page = parse_html('<video src="javascript:alert(1)"></video><img src="data:image/png;base64,AAAA"><a href="file:///etc/passwd">x</a>', "https://x.example.com/")
        self.assertEqual(page.candidates, [])

    def test_iso_duration(self):
        self.assertEqual(parse_iso_duration("PT90S"), 90.0)
        self.assertEqual(parse_iso_duration("PT1M30S"), 90.0)
        self.assertIsNone(parse_iso_duration("garbage"))
        self.assertEqual(parse_iso_duration(12), 12.0)


class HtmlEngineTests(NetCase):
    def page(self, html):
        self.server.httpd.page = html
        return HtmlEngine(stream_engine=StreamEngine())

    def test_only_probed_media_is_offered(self):
        eng = self.page('<title>T</title><video src="/video.mp4"></video><a href="/fake.mp4">fake</a><a href="/about">x</a>')
        info = eng.analyze(self.server.url("/page.html"))
        self.assertEqual(info.engine, "html")
        self.assertEqual(info.kind, "video")
        self.assertEqual(len(info.formats), 1)
        self.assertEqual(info.formats[0].payload["direct_url"], self.server.url("/video.mp4"))
        self.assertEqual(info.formats[0].filesize, 300000)

    def test_page_without_media_yields_none(self):
        self.assertIsNone(self.page("<title>Nothing</title><p>text</p><a href='/about'>x</a>").analyze(self.server.url("/page.html")))

    def test_stream_candidate_expands_into_quality_options(self):
        info = self.page('<video><source src="/master.m3u8" type="application/x-mpegURL"></video>').analyze(self.server.url("/page.html"))
        labels = [f.label for f in info.formats]
        self.assertTrue(any("1080p" in l for l in labels), labels)
        self.assertTrue(all(f.payload.get("downloader") == "stream" for f in info.formats))

    def test_candidates_pointing_at_private_hosts_are_dropped(self):
        eng = self.page('<video src="http://169.254.169.254/latest/meta-data/x.mp4"></video><video src="/video.mp4"></video>')
        info = eng.analyze(self.server.url("/page.html"))
        self.assertEqual([f.payload["direct_url"] for f in info.formats], [self.server.url("/video.mp4")])
        self.assertFalse(any("169.254" in p for p, _ in self.server.requests))

    def test_non_html_response_raises_nothtml(self):
        with self.assertRaises(NotHtml):
            HtmlEngine().analyze(self.server.url("/file.bin"))

    def test_http_error_is_categorised(self):
        with self.assertRaises(AppError) as cm:
            HtmlEngine().analyze(self.server.url("/error403"))
        self.assertEqual(cm.exception.code, ErrorCode.SOURCE_BLOCKED)


class HlsTests(unittest.TestCase):
    def test_master_playlist(self):
        p = parse_hls(HLS_MASTER)
        self.assertEqual(p["type"], "master")
        self.assertEqual([v["height"] for v in p["variants"]], [360, 720, 1080])
        self.assertEqual(p["variants"][1]["fps"], "30")
        self.assertEqual(p["variants"][2]["vcodec"], "avc1.640028")
        self.assertEqual(p["variants"][2]["acodec"], "mp4a.40.2")
        self.assertEqual([a["name"] for a in p["audio"]], ["English", "Indonesia"])
        self.assertFalse(p["drm"])

    def test_media_playlist_duration(self):
        p = parse_hls("#EXTM3U\n#EXTINF:4.5,\na.ts\n#EXTINF:5.5,\nb.ts\n#EXT-X-ENDLIST\n")
        self.assertEqual(p["type"], "media")
        self.assertEqual(p["duration"], 10.0)

    def test_aes128_is_allowed_but_drm_is_flagged(self):
        self.assertFalse(parse_hls('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXTINF:4,\na.ts\n')["drm"])
        self.assertTrue(parse_hls('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:4,\na.ts\n')["drm"])
        self.assertTrue(parse_hls('#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES-CTR,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"\n')["drm"])

    def test_live_playlist_detection(self):
        self.assertTrue(parse_hls("#EXTM3U\n#EXTINF:4,\na.ts\n")["live"])
        self.assertFalse(parse_hls("#EXTM3U\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n")["live"])
        self.assertFalse(parse_hls("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:4,\na.ts\n")["live"])

    def test_rejects_non_playlist(self):
        with self.assertRaises(ValueError):
            parse_hls("<html></html>")

    def test_codec_split(self):
        self.assertEqual(split_codecs("avc1.64001f,mp4a.40.2"), ("avc1.64001f", "mp4a.40.2"))
        self.assertEqual(split_codecs("mp4a.40.2"), ("", "mp4a.40.2"))
        self.assertEqual(split_codecs(""), ("", ""))

    def test_format_options_best_per_height_and_audio(self):
        fmts = build_formats(parse_hls(HLS_MASTER), "https://x.example.com/master.m3u8", "hls")
        ids = [f.id for f in fmts]
        self.assertEqual(ids, ["best", "h1080", "h720", "h360", "mp3"])
        self.assertEqual(fmts[0].height, 1080)
        self.assertIn("English", fmts[0].note)
        self.assertEqual(fmts[-1].kind, "audio")


class DashTests(unittest.TestCase):
    def test_mpd(self):
        p = parse_dash(DASH_MPD)
        self.assertEqual([v["height"] for v in p["variants"]], [480, 1080])
        self.assertEqual(p["audio"][0]["lang"], "en")
        self.assertEqual(p["audio"][0]["codecs"], "mp4a.40.2")
        self.assertEqual(p["duration"], 90.0)
        self.assertFalse(p["drm"])

    def test_content_protection_means_drm(self):
        drm = DASH_MPD.replace('<Representation id="v1"', '<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/><Representation id="v1"')
        self.assertTrue(parse_dash(drm)["drm"])

    def test_entity_expansion_and_doctype_rejected(self):
        evil = '<?xml version="1.0"?><!DOCTYPE mpd [<!ENTITY a "aaaa">]><MPD xmlns="urn:mpeg:dash:schema:mpd:2011">&a;</MPD>'
        with self.assertRaises(ValueError):
            parse_dash(evil)

    def test_dynamic_mpd_is_live(self):
        self.assertTrue(parse_dash(DASH_MPD.replace("<MPD ", '<MPD type="dynamic" '))["live"])
        self.assertFalse(parse_dash(DASH_MPD)["live"])

    def test_dtd_hidden_behind_long_padding_is_still_rejected(self):
        evil = '<?xml version="1.0"?>' + "<!-- " + "x" * 6000 + " -->" + '<!DOCTYPE MPD [<!ENTITY a "b">]><MPD xmlns="urn:mpeg:dash:schema:mpd:2011">&a;</MPD>'
        with self.assertRaises(ValueError):
            parse_dash(evil)

    def test_rejects_non_mpd(self):
        with self.assertRaises(ValueError):
            parse_dash("<html/>")


class StreamEngineTests(NetCase):
    def test_hls_analysis(self):
        info = StreamEngine().analyze(self.server.url("/master.m3u8"))
        self.assertEqual(info.engine, "stream")
        self.assertEqual(info.meta["stream_type"], "hls")
        self.assertEqual(info.meta["audio_tracks"], ["English", "Indonesia"])
        self.assertIn("avc1.640028", info.meta["codecs"])
        self.assertEqual(info.formats[0].height, 1080)

    def test_dash_analysis(self):
        info = StreamEngine().analyze(self.server.url("/manifest.mpd"))
        self.assertEqual(info.meta["stream_type"], "dash")
        self.assertEqual(info.duration, 90.0)
        self.assertEqual([f.id for f in info.formats], ["best", "h1080", "h480", "mp3"])

    def test_non_manifest_content_is_not_applicable(self):
        self.assertIsNone(StreamEngine().analyze(self.server.url("/cd")))

    def test_drm_manifest_is_refused_not_bypassed(self):
        self.server.httpd.page = ""
        from tests import helpers
        orig = helpers.DASH_MPD
        try:
            helpers.DASH_MPD = orig.replace("<Representation id=\"v1\"", "<ContentProtection schemeIdUri=\"urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed\"/><Representation id=\"v1\"")
            with self.assertRaises(AppError) as cm:
                StreamEngine().analyze(self.server.url("/manifest.mpd"))
        finally:
            helpers.DASH_MPD = orig
        self.assertEqual(cm.exception.code, ErrorCode.SOURCE_BLOCKED)


if __name__ == "__main__":
    unittest.main()
