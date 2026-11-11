"""Engine B: yt-dlp.

Uses yt-dlp's own extractor registry to decide whether a URL is supported (no
hard-coded host list), discovers formats, and downloads with MP4/MP3 selection,
concurrent fragments, retries with backoff, subtitle support and cooperative
cancellation. yt-dlp is imported lazily so the rest of the application (and its
tests) work even where yt-dlp is not installed.
"""
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..config import settings
from ..errors import AppError, Cancelled, ErrorCode, classify_exception
from ..models import FormatOption, MediaInfo, format_resolution
from ..security import egress_guard
from .base import Engine, JobContext

logger = logging.getLogger("save_hack.ytdlp")

LANG_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
SKIP_SUFFIXES = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp", ".temp", ".tmp"}


def _import_yt_dlp():
    import yt_dlp  # noqa: WPS433 (lazy on purpose)
    return yt_dlp


def available() -> bool:
    try:
        _import_yt_dlp()
        return True
    except ImportError:
        return False


def version() -> str:
    try:
        return getattr(_import_yt_dlp().version, "__version__", "unknown")
    except ImportError:
        return "not installed"


@lru_cache(maxsize=1)
def _extractor_classes():
    from yt_dlp.extractor import gen_extractor_classes
    return [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]


def match_extractor(url: str) -> Optional[str]:
    """Return the key of the yt-dlp extractor that claims this URL (never the generic one)."""
    try:
        classes = _extractor_classes()
    except ImportError:
        return None
    for ie in classes:
        try:
            if ie.suitable(url):
                return ie.ie_key()
        except Exception:  # a broken extractor regex must not break routing
            continue
    return None


# ---- format selectors ------------------------------------------------------

def mp4_selector(height: Optional[int] = None) -> str:
    """Prefer H.264/AAC (plays everywhere), then any mp4, then best merged, then best single file."""
    h = f"[height<={int(height)}]" if height else ""
    parts = [
        f"bv*{h}[vcodec^=avc1]+ba[acodec^=mp4a]",
        f"bv*{h}[ext=mp4]+ba[ext=m4a]",
        f"bv*{h}+ba",
        f"b{h}[ext=mp4]",
        f"b{h}",
    ]
    if not height:
        parts.append("b")
    return "/".join(parts)


def stream_selector(height: Optional[int], mode: str = "video") -> str:
    if mode == "mp3":
        return "ba/b"
    h = f"[height<={int(height)}]" if height else ""
    return f"bv*{h}+ba/b{h}" + ("" if height else "/b")


def audio_selector(kind: str) -> str:
    return "ba[ext=m4a]/ba/b" if kind == "audio-best" else "ba/b"


# ---- analysis --------------------------------------------------------------

def _short_codec(c: str) -> str:
    if not c or c == "none":
        return ""
    return c.split(".")[0]


def _size(f: dict) -> Optional[int]:
    return f.get("filesize") or f.get("filesize_approx") or None


def build_options(info: dict) -> list:
    """Normalise yt-dlp's format list into a small set of user-facing options."""
    formats = info.get("formats") or []
    video_fmts = [f for f in formats if (f.get("vcodec") or "none") != "none" and f.get("height")]
    audio_fmts = [f for f in formats if (f.get("acodec") or "none") != "none" and (f.get("vcodec") or "none") == "none"]
    options = []
    if not formats:  # extractor gave a single URL without a format list
        options.append(FormatOption(id="mp4-best", label="Terbaik · MP4", kind="video", ext="mp4", container="mp4", payload={"downloader": "ytdlp", "selector": mp4_selector()}))
        options.append(FormatOption(id="mp3", label="Audio · MP3 192 kbps", kind="audio", ext="mp3", container="mp3", acodec="mp3", payload={"downloader": "ytdlp", "selector": audio_selector("mp3"), "mp3": True}))
        return options
    def _abr(f):
        return f.get("abr") or f.get("tbr") or 0
    # The MP4 and "original audio" selectors prefer AAC/m4a (plays everywhere), so size estimates and labels must describe that track.
    m4a = [f for f in audio_fmts if f.get("ext") == "m4a" or (f.get("acodec") or "").startswith("mp4a")]
    best_audio = max(m4a or audio_fmts, key=_abr, default=None)
    heights = sorted({int(f["height"]) for f in video_fmts}, reverse=True)
    if video_fmts:
        top = max(video_fmts, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        has_audio_stream = (top.get("acodec") or "none") != "none"
        total = (_size(top) or 0) + (0 if has_audio_stream else (_size(best_audio) if best_audio else 0) or 0) or None
        options.append(FormatOption(id="mp4-best", label=f"Terbaik ({top['height']}p) · MP4", kind="video", ext="mp4", container="mp4",
                                    resolution=format_resolution(top.get("width"), top.get("height")), height=top.get("height"), width=top.get("width"),
                                    vcodec=_short_codec(top.get("vcodec")), acodec=_short_codec(top.get("acodec") if has_audio_stream else (best_audio or {}).get("acodec")),
                                    filesize=total, bitrate=int(top["tbr"] * 1000) if top.get("tbr") else None,
                                    note="Kualitas tertinggi yang tersedia", payload={"downloader": "ytdlp", "selector": mp4_selector()}))
        for h in heights:
            if h < 144:
                continue
            at_h = [f for f in video_fmts if int(f["height"]) == h]
            pick = max(at_h, key=lambda f: ((f.get("vcodec") or "").startswith("avc1"), f.get("tbr") or 0))
            has_a = (pick.get("acodec") or "none") != "none"
            total = (_size(pick) or 0) + (0 if has_a else (_size(best_audio) if best_audio else 0) or 0) or None
            options.append(FormatOption(id=f"mp4-{h}", label=f"{h}p · MP4", kind="video", ext="mp4", container="mp4",
                                        resolution=format_resolution(pick.get("width"), h), height=h, width=pick.get("width"),
                                        vcodec=_short_codec(pick.get("vcodec")), acodec=_short_codec(pick.get("acodec") if has_a else (best_audio or {}).get("acodec")),
                                        filesize=total, bitrate=int(pick["tbr"] * 1000) if pick.get("tbr") else None,
                                        payload={"downloader": "ytdlp", "selector": mp4_selector(h)}))
    if video_fmts or audio_fmts:
        options.append(FormatOption(id="mp3", label="Audio · MP3 192 kbps", kind="audio", ext="mp3", container="mp3", acodec="mp3",
                                    filesize=None, bitrate=192000, payload={"downloader": "ytdlp", "selector": audio_selector("mp3"), "mp3": True}))
    if best_audio:
        options.append(FormatOption(id="audio-best", label=f"Audio asli · {best_audio.get('ext', 'm4a').upper()}", kind="audio", ext=best_audio.get("ext", "m4a"),
                                    container=best_audio.get("ext", "m4a"), acodec=_short_codec(best_audio.get("acodec")), filesize=_size(best_audio),
                                    bitrate=int(best_audio["abr"] * 1000) if best_audio.get("abr") else None, note="Tanpa konversi",
                                    payload={"downloader": "ytdlp", "selector": audio_selector("audio-best")}))
    for lang in list((info.get("subtitles") or {}).keys())[:12]:
        if LANG_RE.match(lang) and lang != "live_chat":
            options.append(FormatOption(id=f"sub-{lang}", label=f"Subtitle · {lang} (SRT)", kind="subtitle", ext="srt", payload={"downloader": "ytdlp", "subtitle": lang}))
    return options


def build_media_info(url: str, info: dict, engine: str = "ytdlp") -> MediaInfo:
    site = info.get("extractor_key") or info.get("extractor") or ""
    thumb = info.get("thumbnail")
    thumb = thumb if isinstance(thumb, str) and thumb.startswith(("http://", "https://")) else None
    if info.get("_type") == "playlist":
        entries = []
        for e in (info.get("entries") or [])[:settings.max_playlist_entries]:
            if not e:
                continue
            link = e.get("webpage_url") or e.get("url")
            if isinstance(link, str) and link.startswith(("http://", "https://")):
                entries.append({"title": e.get("title") or link, "url": link, "duration": e.get("duration")})
        return MediaInfo(url=url, engine=engine, site=site, title=info.get("title") or "Playlist", kind="playlist", uploader=info.get("uploader") or info.get("channel") or "",
                         thumbnail=thumb, entries=entries, meta={"extractor": site, "playlist_count": len(entries)}, payload={"downloader": "ytdlp"})
    formats = build_options(info)
    has_video = any(f.kind == "video" for f in formats)
    subs = [f.id[4:] for f in formats if f.kind == "subtitle"]
    return MediaInfo(url=url, engine=engine, site=site, title=info.get("title") or "Media tanpa judul", kind="video" if has_video else "audio",
                     uploader=info.get("uploader") or info.get("channel") or "", duration=info.get("duration"), thumbnail=thumb, formats=formats,
                     filesize=next((f.filesize for f in formats if f.id == "mp4-best" and f.filesize), None),
                     meta={"extractor": site, "subtitle_langs": subs, "is_live": bool(info.get("is_live"))},
                     payload={"downloader": "ytdlp", "webpage_url": info.get("webpage_url") or url})


def extract_info(url: str, playlist_ok: bool = True) -> dict:
    yt_dlp = _import_yt_dlp()
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "ignoreconfig": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": settings.http_timeout, "retries": 1, "extractor_retries": 1,
        "geo_bypass": False, "playlistend": settings.max_playlist_entries, "extract_flat": "in_playlist",
    }
    with egress_guard(), yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


class YtDlpEngine(Engine):
    name = "ytdlp"

    def analyze(self, url: str) -> Optional[MediaInfo]:
        key = match_extractor(url)
        if key is None:
            return None  # not claimed by any specific extractor: let the generic engines try
        try:
            info = extract_info(url)
        except Exception as exc:
            raise classify_exception(exc, ErrorCode.MEDIA_NOT_FOUND)
        if not info:
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, detail="yt-dlp returned no info")
        if info.get("is_live"):
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Siaran langsung (live) belum bisa diunduh.", detail="live stream")
        result = build_media_info(url, info)
        result.meta["extractor_key"] = key
        return result

    def download(self, info: MediaInfo, fmt: FormatOption, ctx: JobContext) -> Path:
        url = info.payload.get("webpage_url") or info.url
        if fmt.kind == "subtitle":
            return run_ytdlp_download(url, "", ctx, subtitle_lang=fmt.payload.get("subtitle"))
        return run_ytdlp_download(url, fmt.payload.get("selector") or mp4_selector(), ctx, audio_mp3=bool(fmt.payload.get("mp3")))


# ---- download --------------------------------------------------------------

def _backoff(n: int) -> float:
    return min(settings.retry_backoff * (2 ** max(n - 1, 0)), 20.0)


def build_download_opts(ctx: JobContext, selector: str, audio_mp3: bool, subtitle_lang: Optional[str], hooks: dict) -> dict:
    outtmpl = str(ctx.workdir / "%(title).100B.%(ext)s")
    opts = {
        "outtmpl": outtmpl, "paths": {"home": str(ctx.workdir), "temp": str(ctx.workdir)},
        "quiet": True, "no_warnings": True, "noprogress": True, "ignoreconfig": True, "noplaylist": True,
        "windowsfilenames": True, "restrictfilenames": False, "trim_file_name": 150,
        "socket_timeout": settings.http_timeout, "retries": settings.max_retries, "fragment_retries": settings.max_retries,
        "file_access_retries": 2, "extractor_retries": 1,
        "retry_sleep_functions": {"http": _backoff, "fragment": _backoff, "extractor": _backoff},
        "concurrent_fragment_downloads": settings.fragment_concurrency, "http_chunk_size": 10 * 1024 * 1024,
        "max_filesize": settings.max_download_bytes, "continuedl": True, "geo_bypass": False,
        # Segment fetching must stay inside this process (egress guard). An external `ffmpeg -i <url>` or aria2
        # would resolve DNS on its own and bypass the SSRF guard, so force yt-dlp's native downloaders.
        "hls_prefer_native": True, "external_downloader": {"default": "native"},
        "progress_hooks": [hooks["progress"]], "postprocessor_hooks": [hooks["post"]],
    }
    if subtitle_lang:
        opts.update({"skip_download": True, "writesubtitles": True, "writeautomaticsub": False, "subtitleslangs": [subtitle_lang], "subtitlesformat": "srt/vtt/best",
                     "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]})
        return opts
    opts["format"] = selector
    if audio_mp3:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessor_args"] = {"Merger": ["-movflags", "+faststart"]}
    return opts


def pick_output(workdir: Path, audio_mp3: bool, subtitle: bool) -> Optional[Path]:
    files = [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() not in SKIP_SUFFIXES]
    if subtitle:
        files = [p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in (".srt", ".vtt")]
    if audio_mp3:
        mp3 = [p for p in files if p.suffix.lower() == ".mp3"]
        files = mp3 or files
    if not files:
        return None
    return max(files, key=lambda p: (p.suffix.lower() in (".mp4", ".mp3", ".srt"), p.stat().st_size))


def run_ytdlp_download(url: str, selector: str, ctx: JobContext, audio_mp3: bool = False, subtitle_lang: Optional[str] = None) -> Path:
    yt_dlp = _import_yt_dlp()
    from yt_dlp.utils import DownloadCancelled
    abort = {}
    done = {"bytes": 0, "last_file": None, "last_bytes": 0}
    started = time.monotonic()

    def progress_hook(d):
        try:
            ctx.check()
        except (Cancelled, AppError) as exc:
            abort["error"] = exc
            raise DownloadCancelled()
        st = d.get("status")
        fname = d.get("filename")
        cur = int(d.get("downloaded_bytes") or 0)
        if done["last_file"] != fname:
            done["bytes"] += done["last_bytes"]
            done["last_file"], done["last_bytes"] = fname, 0
        done["last_bytes"] = cur
        downloaded = done["bytes"] + cur
        if downloaded > settings.max_download_bytes:
            abort["error"] = AppError(ErrorCode.STORAGE_LIMIT, f"File melebihi batas {settings.max_download_mb} MB.", detail="progress over limit")
            raise DownloadCancelled()
        info = d.get("info_dict") or {}
        req = info.get("requested_formats") or []
        sizes = [_size(f) for f in req]
        total = sum(sizes) if req and all(sizes) else (d.get("total_bytes") or d.get("total_bytes_estimate"))
        if total and total > settings.max_download_bytes * 1.05:
            abort["error"] = AppError(ErrorCode.STORAGE_LIMIT, f"File {int(total) // (1024 * 1024)} MB melebihi batas {settings.max_download_mb} MB.", detail="estimated size over limit")
            raise DownloadCancelled()
        ctx.update(stage="downloading", downloaded=downloaded, total=int(total) if total else None, speed=d.get("speed"), eta=d.get("eta"))
        if st == "finished":
            ctx.update(speed=None, eta=None)

    def post_hook(d):
        if d.get("status") == "started":
            ctx.update(stage="converting", speed=None, eta=None)
        try:
            ctx.check()
        except (Cancelled, AppError) as exc:
            abort["error"] = exc
            raise DownloadCancelled()

    opts = build_download_opts(ctx, selector, audio_mp3, subtitle_lang, {"progress": progress_hook, "post": post_hook})
    try:
        with egress_guard(), yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadCancelled:
        raise abort.get("error") or Cancelled()
    except (Cancelled, AppError):
        raise
    except Exception as exc:
        if abort.get("error"):
            raise abort["error"]
        err = classify_exception(exc, ErrorCode.DOWNLOAD_FAILED)
        if "ffmpeg" in str(exc).lower() or "postprocess" in str(exc).lower():
            err = AppError(ErrorCode.CONVERSION_FAILED, detail=err.detail)
        raise err
    if abort.get("error"):
        raise abort["error"]
    out = pick_output(ctx.workdir, audio_mp3, bool(subtitle_lang))
    if out is None:
        raise AppError(ErrorCode.DOWNLOAD_FAILED, "Engine tidak menghasilkan file.", detail=f"no output after {time.monotonic() - started:.1f}s")
    if out.stat().st_size > settings.max_download_bytes:
        out.unlink(missing_ok=True)
        raise AppError(ErrorCode.STORAGE_LIMIT, f"File melebihi batas {settings.max_download_mb} MB.", detail="final file over limit")
    ctx.update(stage="done", downloaded=out.stat().st_size, total=out.stat().st_size)
    return out
