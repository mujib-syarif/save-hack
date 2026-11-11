"""Engine A: direct file URLs.

Streams to disk in chunks (never buffers a whole file in memory), follows
validated redirects, detects the file name from Content-Disposition / URL /
Content-Type, and resumes interrupted transfers with HTTP Range + If-Range.
"""
import logging
import mimetypes
import re
import shutil
import time
from email.message import Message
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests

from ..config import settings
from ..errors import AppError, Cancelled, ErrorCode
from ..models import FormatOption, MediaInfo
from ..net import open_stream, raise_for_status
from ..security import filename_from_url, safe_filename
from .base import Engine, JobContext

logger = logging.getLogger("save_hack.direct")

FILE_EXTENSIONS = {
    ".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".flv", ".3gp", ".ts",
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg",
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".epub", ".apk", ".iso",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv",
}
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".flv", ".3gp", ".ts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg"}
MIME_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
    "application/pdf": ".pdf", "application/zip": ".zip", "application/x-zip-compressed": ".zip",
    "application/vnd.rar": ".rar", "application/x-rar-compressed": ".rar", "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz", "application/x-tar": ".tar",
}
HTML_TYPES = ("text/html", "application/xhtml+xml")


def kind_for(ext: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ext in VIDEO_EXT or ct.startswith("video/"):
        return "video"
    if ext in AUDIO_EXT or ct.startswith("audio/"):
        return "audio"
    if ext in IMAGE_EXT or ct.startswith("image/"):
        return "image"
    return "file"


def parse_content_disposition(value: str) -> str:
    """Extract a filename (RFC 6266 / 2231, including `filename*=`) or ''."""
    if not value:
        return ""
    try:
        msg = Message()
        msg["content-disposition"] = value
        name = msg.get_filename()
    except Exception:
        name = None
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
        name = m.group(1) if m else ""
    return name or ""


def extension_for(filename: str, content_type: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext and re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    return MIME_EXT.get(ct) or (mimetypes.guess_extension(ct) if ct and ct != "application/octet-stream" else "") or ""


def parse_total(resp) -> Optional[int]:
    """Total size from Content-Range (206) or Content-Length (200)."""
    cr = resp.headers.get("Content-Range", "")
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr)
    if m and m.group(3) != "*":
        return int(m.group(3))
    if resp.status_code == 200:
        cl = resp.headers.get("Content-Length", "")
        if cl.isdigit():
            return int(cl)
    return None


class DirectEngine(Engine):
    name = "direct"

    # ---- analysis -------------------------------------------------------
    def probe(self, url: str) -> dict:
        """Cheap request (Range 0-0) that reveals type, size, name and range support."""
        resp, final = open_stream(url, headers={"Range": "bytes=0-0"})
        try:
            raise_for_status(resp)
            ctype = resp.headers.get("Content-Type", "")
            disp = resp.headers.get("Content-Disposition", "")
            total = parse_total(resp)
            filename = parse_content_disposition(disp) or filename_from_url(final)
            return {
                "final_url": final, "content_type": ctype, "disposition": disp, "filesize": total,
                "accept_ranges": resp.status_code == 206 or "bytes" in resp.headers.get("Accept-Ranges", "").lower(),
                "etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified"),
                "filename": filename, "status": resp.status_code,
            }
        finally:
            resp.close()

    @staticmethod
    def looks_like_file_url(url: str) -> bool:
        return Path(urlsplit(url).path.lower()).suffix in FILE_EXTENSIONS

    def build_info(self, url: str, probe: dict, site: str = None, engine: str = None, title: str = None) -> Optional[MediaInfo]:
        ctype = probe["content_type"].split(";")[0].strip().lower()
        attachment = "attachment" in probe["disposition"].lower()
        if ctype in HTML_TYPES and not attachment:
            return None  # a web page, not a file
        ext = extension_for(probe["filename"], ctype)
        name = safe_filename(probe["filename"] or "", default="download", ext=ext)
        if ext and not Path(name).suffix:
            name = f"{name}{ext}"
        kind = kind_for(ext, ctype)
        size = probe["filesize"]
        label = (ext.lstrip(".").upper() or "FILE") + " (file asli)"
        fmt = FormatOption(id="file", label=label, kind=kind if kind in ("video", "audio", "image") else "file", ext=ext.lstrip("."),
                           container=ext.lstrip("."), filesize=size, note="Unduh langsung" + ("; mendukung resume" if probe["accept_ranges"] else ""),
                           payload={"filename": name})
        host = urlsplit(probe["final_url"]).hostname or ""
        preview = probe["final_url"] if kind in ("video", "audio", "image") and probe["final_url"].startswith("https://") else None
        return MediaInfo(url=url, engine=engine or self.name, site=site or host, title=title or Path(name).stem, kind=kind,
                         filesize=size, formats=[fmt], preview_url=preview,
                         payload={"direct_url": probe["final_url"], "downloader": "direct", "etag": probe["etag"],
                                  "last_modified": probe["last_modified"], "accept_ranges": probe["accept_ranges"]})

    def analyze(self, url: str) -> Optional[MediaInfo]:
        probe = self.probe(url)
        return self.build_info(url, probe)

    # ---- download -------------------------------------------------------
    def download(self, info: MediaInfo, fmt: FormatOption, ctx: JobContext) -> Path:
        # Options built by other engines (HTML parser, site plugins) carry their own resolved URL.
        url = fmt.payload.get("direct_url") or info.payload.get("direct_url") or info.url
        filename = safe_filename(fmt.payload.get("filename") or "download", default="download")
        dest = ctx.workdir / filename
        part = ctx.workdir / (filename + ".part")
        state = ctx.state
        state.setdefault("etag", fmt.payload.get("etag") or info.payload.get("etag"))
        state.setdefault("last_modified", fmt.payload.get("last_modified") or info.payload.get("last_modified"))
        attempt = 0
        while True:
            ctx.check()
            try:
                self._fetch(url, part, ctx, state)
                break
            except Cancelled:
                raise
            except AppError as exc:
                if not exc.retryable or attempt >= settings.max_retries:
                    raise
                err = exc
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as exc:
                if attempt >= settings.max_retries:
                    raise AppError(ErrorCode.NETWORK_ERROR, detail=repr(exc))
                err = exc
            attempt += 1
            delay = settings.retry_backoff * (2 ** (attempt - 1))
            logger.info("direct download retry %s/%s in %.1fs after %s", attempt, settings.max_retries, delay, type(err).__name__)
            if ctx.cancel.wait(delay):
                raise Cancelled()
        part.replace(dest)
        return dest

    def _fetch(self, url: str, part: Path, ctx: JobContext, state: dict) -> None:
        offset = part.stat().st_size if part.exists() else 0
        headers = {}
        validator = state.get("etag") or state.get("last_modified")
        if offset and validator:
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = validator
        elif offset:
            part.unlink(missing_ok=True)  # cannot prove the remote file is unchanged: start over
            offset = 0
        resp, _final = open_stream(url, headers=headers)
        try:
            if resp.status_code == 416:
                # Range not satisfiable: our partial file is already complete or stale.
                total = None
                m = re.match(r"bytes\s+\*/(\d+)", resp.headers.get("Content-Range", ""))
                if m:
                    total = int(m.group(1))
                if total is not None and total == offset:
                    return
                part.unlink(missing_ok=True)
                raise AppError(ErrorCode.NETWORK_ERROR, detail="stale partial file, restarting", retryable=True)
            raise_for_status(resp)
            resumed = resp.status_code == 206 and offset > 0
            if resumed:
                m = re.match(r"bytes\s+(\d+)-", resp.headers.get("Content-Range", ""))
                if not m or int(m.group(1)) != offset:
                    part.unlink(missing_ok=True)
                    raise AppError(ErrorCode.NETWORK_ERROR, detail="server returned unexpected range", retryable=True)
            else:
                offset = 0  # server ignored Range (200) or fresh start
            state["etag"] = resp.headers.get("ETag") or state.get("etag")
            state["last_modified"] = resp.headers.get("Last-Modified") or state.get("last_modified")
            total = parse_total(resp)
            if total is not None and total > settings.max_download_bytes:
                raise AppError(ErrorCode.STORAGE_LIMIT, f"File {total // (1024 * 1024)} MB melebihi batas {settings.max_download_mb} MB.", detail="content-length over limit")
            free = shutil.disk_usage(part.parent).free
            if total is not None and total - offset > free - 32 * 1024 * 1024:
                raise AppError(ErrorCode.STORAGE_LIMIT, detail="not enough free disk space")
            mode = "ab" if resumed else "wb"
            written = offset
            ctx.update(stage="downloading", downloaded=written, total=total)
            last = time.monotonic()
            last_bytes = written
            with open(part, mode) as fh:
                for chunk in resp.iter_content(chunk_size=settings.chunk_size):
                    ctx.check()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if written > settings.max_download_bytes:
                        raise AppError(ErrorCode.STORAGE_LIMIT, f"File melebihi batas {settings.max_download_mb} MB.", detail="stream over limit")
                    now = time.monotonic()
                    if now - last >= 0.4:
                        speed = (written - last_bytes) / (now - last)
                        eta = ((total - written) / speed) if total and speed > 0 else None
                        ctx.update(downloaded=written, speed=speed, eta=eta)
                        last, last_bytes = now, written
            ctx.update(downloaded=written, speed=None, eta=None)
            if total is not None and written != total:
                raise AppError(ErrorCode.NETWORK_ERROR, detail=f"incomplete transfer {written}/{total}", retryable=True)
        finally:
            resp.close()
