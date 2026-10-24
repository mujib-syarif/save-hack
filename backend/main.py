from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("save_hack")

app = FastAPI(title="Save Hack Media API", version="0.2.0")

DEFAULT_ORIGINS = (
    "https://mujib-syarif.github.io,"
    "http://localhost:8080,"
    "http://127.0.0.1:8080"
)
ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

OUT = Path(os.getenv("DOWNLOAD_DIR", "/tmp/downloads"))
OUT.mkdir(parents=True, exist_ok=True)
MAX_MB = max(1, min(int(os.getenv("MAX_DOWNLOAD_MB", "300")), 2048))
MAX_CONCURRENT_JOBS = max(1, min(int(os.getenv("MAX_CONCURRENT_JOBS", "1")), 4))
JOB_TTL_SECONDS = max(600, int(os.getenv("JOB_TTL_SECONDS", "1800")))
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
ANALYZE_SLOTS = threading.BoundedSemaphore(2)
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# This allowlist is a first layer, not a substitute for outbound network controls.
SUPPORTED_HOSTS = {
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "fb.watch", "x.com", "twitter.com", "reddit.com",
    "v.redd.it", "vimeo.com", "dailymotion.com",
}
DIRECT_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".aac", ".wav", ".ogg"}
QUALITIES = {"best", "480", "720", "1080", "1440", "2160"}


class MediaRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    rights_confirmed: bool


class DownloadRequest(MediaRequest):
    format: Literal["mp4", "mp3"]
    quality: Literal["best", "480", "720", "1080", "1440", "2160"] = "1080"


class SlidingWindowLimiter:
    """Process-local abuse guard. Use a shared limiter and egress controls for public scale."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, action: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            hits = self._hits[(key, action)]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


LIMITER = SlidingWindowLimiter()


def client_key(request: Request) -> str:
    # Do not trust X-Forwarded-For here; proxy trust must be configured explicitly.
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request, action: str, limit: int, window: int) -> None:
    if not LIMITER.allow(client_key(request), action, limit, window):
        raise HTTPException(429, "Terlalu banyak permintaan. Tunggu sebentar lalu coba lagi.")


def _is_supported_host(host: str) -> bool:
    return any(host == base or host.endswith("." + base) for base in SUPPORTED_HOSTS)


def validate_url(raw: str) -> str:
    """Validate user-supplied URL and reject local/private destinations before extraction."""
    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or not host:
            raise ValueError("https required")
        if parsed.username or parsed.password:
            raise ValueError("credentials in URL are not allowed")
        if parsed.port not in (None, 443):
            raise ValueError("non-standard port")
        # Reject IP literals, including public IPs, to reduce alternate access paths.
        try:
            ipaddress.ip_address(host)
            raise ValueError("IP literals are not supported")
        except ValueError as error:
            if str(error) == "IP literals are not supported":
                raise
        direct_file = Path(parsed.path.lower()).suffix in DIRECT_EXTENSIONS
        if not _is_supported_host(host) and not direct_file:
            raise ValueError("unsupported host")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("host did not resolve")
        for info in addresses:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise ValueError("private or non-public address")
        return raw.strip()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            400,
            "URL tidak valid, alamat privat, atau domain belum didukung. Gunakan tautan HTTPS yang dapat diakses publik.",
        )


def cleanup(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove temporary file %s", path.name)


def cleanup_expired_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    expired: list[tuple[str, Path | None]] = []
    with JOBS_LOCK:
        for job_id, job in list(JOBS.items()):
            if job["status"] in {"completed", "failed"} and job["updated_at"] < cutoff:
                expired.append((job_id, job.get("path")))
                del JOBS[job_id]
    for _, path in expired:
        cleanup(path)


def make_ytdlp_options(job_id: str, fmt: str, quality: str) -> dict:
    output = str(OUT / (job_id + ".%(ext)s"))
    options = {
        "outtmpl": output,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "max_filesize": MAX_MB * 1024 * 1024,
        "progress_hooks": [lambda data: update_progress(job_id, data)],
        "postprocessor_hooks": [lambda data: update_postprocessing(job_id, data)],
    }
    if fmt == "mp3":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
        return options

    if quality == "best":
        selector = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]"
    else:
        selector = (
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]"
            f"/b[height<={quality}][ext=mp4]"
        )
    options.update({"format": selector, "merge_output_format": "mp4"})
    return options


def update_progress(job_id: str, data: dict) -> None:
    state = data.get("status")
    now = time.time()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["updated_at"] = now
        if state == "downloading":
            job["status"] = "downloading"
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            job["downloaded_bytes"] = downloaded
            job["total_bytes"] = total
            job["progress"] = min(99, round(downloaded * 100 / total)) if total > 0 else None
        elif state == "finished":
            job["status"] = "finalizing"
            job["progress"] = 99


def update_postprocessing(job_id: str, _data: dict) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["status"] = "finalizing"
            job["progress"] = 99
            job["updated_at"] = time.time()


def run_download(job_id: str, url: str, fmt: str, quality: str) -> None:
    try:
        with yt_dlp.YoutubeDL(make_ytdlp_options(job_id, fmt, quality)) as downloader:
            downloader.download([url])
        found = [
            path for path in OUT.glob(job_id + ".*")
            if path.is_file() and path.suffix not in {".part", ".ytdl"}
        ]
        if not found:
            raise RuntimeError("The media engine did not produce an output file.")
        path = max(found, key=lambda item: item.stat().st_mtime)
        size = path.stat().st_size
        if size <= 0:
            cleanup(path)
            raise RuntimeError("The media engine produced an empty file.")
        if size > MAX_MB * 1024 * 1024:
            cleanup(path)
            raise RuntimeError(f"File exceeds the configured {MAX_MB} MB limit.")
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job.update({
                    "status": "completed",
                    "progress": 100,
                    "downloaded_bytes": size,
                    "total_bytes": size,
                    "path": path,
                    "filename": "save-hack" + (".mp3" if fmt == "mp3" else ".mp4"),
                    "updated_at": time.time(),
                })
    except Exception:
        logger.exception("Download job %s failed", job_id)
        for path in OUT.glob(job_id + ".*"):
            cleanup(path)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job.update({
                    "status": "failed",
                    "progress": None,
                    "error": "Download gagal. Sumber mungkin tidak mendukung format ini atau proses dibatasi. Coba tautan lain.",
                    "updated_at": time.time(),
                })
    finally:
        DOWNLOAD_SLOTS.release()


def job_snapshot(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job.get("progress"),
        "downloaded_bytes": job.get("downloaded_bytes", 0),
        "total_bytes": job.get("total_bytes", 0),
        "filename": job.get("filename"),
        "file_ready": job["status"] == "completed",
        "error": job.get("error"),
    }


def finish_file_response(job_id: str, path: Path) -> None:
    cleanup(path)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)


@app.get("/health")
def health():
    return {"ok": True, "service": "Save Hack Media API", "version": "0.2.0"}


@app.post("/analyze")
def analyze(req: MediaRequest, request: Request):
    enforce_rate_limit(request, "analyze", 12, 60)
    if not req.rights_confirmed:
        raise HTTPException(400, "Konfirmasi hak unduh wajib diisi.")
    url = validate_url(req.url)
    if not ANALYZE_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Server sedang sibuk menganalisis tautan. Coba lagi sebentar.")
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 12,
            "skip_download": True,
        }) as downloader:
            info = downloader.extract_info(url, download=False)
        heights = sorted({
            int(item["height"])
            for item in (info.get("formats") or [])
            if item.get("height") and item.get("vcodec") != "none"
        })
        return {
            "title": info.get("title") or "Media tanpa judul",
            "uploader": info.get("uploader") or info.get("channel") or "Tidak diketahui",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "formats": ["mp4", "mp3"],
            "available_heights": heights,
            "max_height": max(heights, default=None),
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Media analysis failed")
        raise HTTPException(
            422,
            "URL gagal dianalisis. Sumber mungkin tidak didukung, sedang membatasi akses, atau media tidak tersedia.",
        )
    finally:
        ANALYZE_SLOTS.release()


@app.post("/download", status_code=202)
def download(req: DownloadRequest, request: Request):
    enforce_rate_limit(request, "download", 4, 600)
    if not req.rights_confirmed:
        raise HTTPException(400, "Konfirmasi hak unduh wajib diisi.")
    url = validate_url(req.url)
    cleanup_expired_jobs()
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Server sibuk. Tunggu hingga unduhan lain selesai lalu coba lagi.")

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "path": None,
        "filename": None,
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    try:
        EXECUTOR.submit(run_download, job_id, url, req.format, req.quality)
    except Exception:
        DOWNLOAD_SLOTS.release()
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        logger.exception("Could not schedule download job")
        raise HTTPException(503, "Server tidak dapat memulai unduhan. Coba lagi nanti.")
    return {"job_id": job_id, "status": "queued", "poll_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    cleanup_expired_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job tidak ditemukan atau sudah kedaluwarsa.")
        return job_snapshot(job)


@app.get("/jobs/{job_id}/file")
def get_job_file(job_id: str):
    cleanup_expired_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job tidak ditemukan atau sudah kedaluwarsa.")
        if job["status"] != "completed" or not job.get("path"):
            raise HTTPException(409, "File belum siap diunduh.")
        path = job["path"]
        filename = job["filename"]
        media_type = "audio/mpeg" if filename.endswith(".mp3") else "video/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(finish_file_response, job_id, path),
    )
