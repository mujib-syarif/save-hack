"""Runtime-compatibility tests against the REAL yt-dlp and FFmpeg.

Skipped when yt-dlp or ffmpeg is missing (they are installed in CI and in the Docker image).
A tiny HLS stream is generated with FFmpeg and served by a local server, so no external site is needed.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yt_dlp  # noqa: F401
    HAVE_YTDLP = True
except ImportError:
    HAVE_YTDLP = False

from savehack.engines import ytdlp_engine
from savehack.engines.base import JobContext
from savehack.engines.streams import StreamEngine

from tests.helpers import NetCase

HAVE_FFMPEG = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def probe(path: Path) -> str:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return out.stdout


@unittest.skipUnless(HAVE_YTDLP and HAVE_FFMPEG, "needs yt-dlp, ffmpeg and ffprobe")
class RealEngineTests(NetCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hls = tempfile.TemporaryDirectory()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=15", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", f"{cls.hls.name}/seg%d.ts", f"{cls.hls.name}/index.m3u8"], check=True)
        cls.server.httpd.hls_dir = cls.hls.name

    @classmethod
    def tearDownClass(cls):
        cls.hls.cleanup()
        super().tearDownClass()

    def test_registry_loads_and_generic_is_excluded(self):
        self.assertIsNotNone(ytdlp_engine.match_extractor("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertIsNone(ytdlp_engine.match_extractor("https://random-site.example.com/some/page"))

    def test_hls_mp4_download_through_streamengine(self):
        eng = StreamEngine()
        info = eng.analyze(self.server.url("/hls/index.m3u8"))
        self.assertEqual(info.meta["stream_type"], "hls")
        ctx = JobContext(workdir=self.workdir)
        out = eng.download(info, info.formats[0], ctx)
        self.assertEqual(out.suffix, ".mp4")
        streams = probe(out)
        self.assertIn("h264,video", streams)
        self.assertIn("aac,audio", streams)
        self.assertEqual(ctx.progress.stage, "done")

    def test_hls_mp3_conversion_through_ffmpeg(self):
        eng = StreamEngine()
        info = eng.analyze(self.server.url("/hls/index.m3u8"))
        mp3 = next(f for f in info.formats if f.id == "mp3")
        out = eng.download(info, mp3, JobContext(workdir=self.workdir))
        self.assertEqual(out.suffix, ".mp3")
        self.assertIn("mp3,audio", probe(out))

    def test_private_manifest_redirect_is_blocked_by_egress_guard(self):
        """yt-dlp must not be able to reach a non-public address even if a manifest points at one."""
        d = Path(self.hls.name)
        (d / "evil.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nhttp://169.254.169.254/seg0.ts\n#EXT-X-ENDLIST\n")
        eng = StreamEngine()
        info = eng.analyze(self.server.url("/hls/evil.m3u8"))
        from savehack.errors import AppError
        with self.assertRaises(AppError):
            eng.download(info, info.formats[0], JobContext(workdir=self.workdir))
        self.assertEqual(list(self.workdir.glob("*.mp4")), [])


if __name__ == "__main__":
    unittest.main()
