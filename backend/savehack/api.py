"""FastAPI application: thin HTTP layer over router + job manager."""
import logging
import mimetypes
import shutil
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .engines import ytdlp_engine
from .errors import AppError, ErrorCode
from .jobs import JobManager
from .ratelimit import RateLimiter
from .router import Router

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("save_hack")

ROUTER = Router()
MANAGER = JobManager(ROUTER)
ANALYZE_LIMIT = RateLimiter(settings.rate_analyze_per_min)
DOWNLOAD_LIMIT = RateLimiter(settings.rate_download_per_min)
STARTED = time.time()

# Types a browser could execute or render inline are always served as opaque downloads.
UNSAFE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/javascript", "application/javascript", "text/xml", "application/xml"}


@asynccontextmanager
async def lifespan(_app):
    MANAGER.start_background()
    logger.info("Save Hack %s started (yt-dlp %s, ffmpeg=%s)", __version__, ytdlp_engine.version(), bool(shutil.which("ffmpeg")))
    try:
        yield
    finally:
        MANAGER.shutdown()


app = FastAPI(title="Save Hack Universal Downloader API", version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"], expose_headers=["Content-Disposition", "Content-Length"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/") and request.method != "GET" or request.url.path.startswith(("/api/jobs", "/api/formats")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---- errors ------------------------------------------------------------------

def error_response(err: AppError, headers: dict = None) -> JSONResponse:
    # `detail` duplicates the message so older clients that read FastAPI-style errors keep working.
    return JSONResponse(status_code=err.status, content={"ok": False, "error": err.to_public(), "detail": err.message}, headers=headers)


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError):
    if exc.detail:
        logger.info("request failed: %s | %s", exc.code.value, exc.detail)
    return error_response(exc)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, _exc: RequestValidationError):
    return error_response(AppError(ErrorCode.INVALID_URL, "Permintaan tidak lengkap atau formatnya salah.", status=400, retryable=False))


@app.exception_handler(Exception)
async def unhandled_handler(_request: Request, exc: Exception):
    logger.exception("unhandled error")
    return error_response(AppError(ErrorCode.SERVER_ERROR, detail=repr(exc)))


# ---- helpers -----------------------------------------------------------------

def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return peer
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return parts[-hops] if len(parts) >= hops else peer


def limit(limiter: RateLimiter, request: Request) -> str:
    ip = client_ip(request)
    if not limiter.allow(ip):
        raise AppError(ErrorCode.RATE_LIMITED, detail=f"rate limit for {ip}")
    return ip


def require_rights(flag: bool) -> None:
    if not flag:
        raise AppError(ErrorCode.INVALID_URL, "Konfirmasi hak unduh wajib dicentang.", status=400, retryable=False)


def media_payload(job) -> dict:
    return {"ok": True, "job_id": job.id, "media": job.info.to_public(), "formats": [f.to_public() for f in job.info.formats],
            "default_format": job.info.formats[0].id if job.info.formats else None}


class AnalyzeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    rights_confirmed: bool


class DownloadRequest(BaseModel):
    job_id: str = Field(min_length=8, max_length=64)
    format_id: str = Field(min_length=1, max_length=64)


class LegacyDownloadRequest(AnalyzeRequest):
    format: str = Field(pattern="^(mp4|mp3)$")


# ---- endpoints ---------------------------------------------------------------

@app.get("/health")
def health():
    base = settings.download_dir
    try:
        free_mb = shutil.disk_usage(base).free // (1024 * 1024)
    except OSError:
        free_mb = None
    return {"ok": True, "service": "Save Hack Universal Downloader API", "version": __version__, "yt_dlp_version": ytdlp_engine.version(),
            "ffmpeg": bool(shutil.which("ffmpeg")), "engines": ROUTER.engines(), "jobs": MANAGER.counts(),
            "storage": {"used_mb": MANAGER.storage_used() // (1024 * 1024), "limit_mb": settings.max_storage_mb, "disk_free_mb": free_mb},
            "limits": {"max_download_mb": settings.max_download_mb, "max_concurrent_jobs": settings.max_concurrent_jobs},
            "uptime_seconds": int(time.time() - STARTED)}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest, request: Request):
    ip = limit(ANALYZE_LIMIT, request)
    require_rights(req.rights_confirmed)
    info = ROUTER.analyze(req.url)
    job = MANAGER.create(info, ip)
    logger.info("analyzed host=%s engine=%s formats=%d", urlsplit(info.url).hostname, info.engine, len(info.formats))
    return media_payload(job)


@app.get("/api/formats/{job_id}")
def formats(job_id: str):
    job = MANAGER.get(job_id)
    return {"ok": True, "job_id": job.id, "formats": [f.to_public() for f in job.info.formats]}


@app.post("/api/download", status_code=202)
def start_download(req: DownloadRequest, request: Request):
    ip = limit(DOWNLOAD_LIMIT, request)
    job = MANAGER.start(req.job_id, req.format_id, ip)
    return {"ok": True, **job.to_public()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return {"ok": True, **MANAGER.get(job_id).to_public()}


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    MANAGER.delete(job_id)
    return {"ok": True, "job_id": job_id, "status": "deleted"}


@app.get("/api/download/{job_id}")
def fetch_file(job_id: str):
    job = MANAGER.get(job_id)
    if job.status != "done" or not job.result_path or not job.result_path.is_file():
        raise AppError(ErrorCode.MEDIA_NOT_FOUND, "File belum siap atau sudah dihapus dari server.", detail=f"job {job.status}", status=409, retryable=False)
    job.touch()  # keep the file while the browser is still downloading / may resume
    ctype = mimetypes.guess_type(job.result_name)[0] or "application/octet-stream"
    if ctype in UNSAFE_TYPES:
        ctype = "application/octet-stream"
    return FileResponse(job.result_path, media_type=ctype, filename=job.result_name)


# ---- legacy endpoints (the previous frontend: /analyze and a synchronous /download) ------------

@app.post("/analyze", include_in_schema=False)
def legacy_analyze(req: AnalyzeRequest, request: Request):
    limit(ANALYZE_LIMIT, request)
    require_rights(req.rights_confirmed)
    info = ROUTER.analyze(req.url)
    return {"title": info.title, "uploader": info.uploader or "Tidak diketahui", "duration": info.duration, "thumbnail": info.thumbnail,
            "extractor": info.site or info.engine, "formats": ["mp4", "mp3"]}


@app.post("/download", include_in_schema=False)
def legacy_download(req: LegacyDownloadRequest, request: Request):
    ip = limit(DOWNLOAD_LIMIT, request)
    require_rights(req.rights_confirmed)
    info = ROUTER.analyze(req.url)
    wanted = [f for f in info.formats if (f.kind in ("video", "image", "file") if req.format == "mp4" else f.kind == "audio")]
    if not wanted:
        raise AppError(ErrorCode.FORMAT_UNAVAILABLE)
    job = MANAGER.create(info, ip)
    try:
        MANAGER.start(job.id, wanted[0].id, ip)
        job.finished.wait(settings.job_timeout)
        if job.status != "done":
            raise job.error or AppError(ErrorCode.DOWNLOAD_FAILED)
        return fetch_file(job.id)
    except BaseException:
        MANAGER.delete(job.id)
        raise
