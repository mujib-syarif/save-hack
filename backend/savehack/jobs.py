"""Job lifecycle: analysis results, queued downloads, cancellation, cleanup.

Each job owns a private directory below `download_dir`; removing that directory
is the only cleanup needed. Files stay available for a short TTL after
completion (so an interrupted browser download can resume) and are then swept.
"""
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .config import settings
from .engines.base import JobContext
from .errors import AppError, Cancelled, ErrorCode, classify_exception
from .models import MediaInfo
from .security import safe_filename

logger = logging.getLogger("save_hack.jobs")

ACTIVE = {"queued", "running"}
TERMINAL = {"done", "failed", "cancelled"}


class Job:
    def __init__(self, info: MediaInfo, owner: str, base: Path):
        self.id = uuid.uuid4().hex
        self.info = info
        self.owner = owner
        self.status = "analyzed"
        self.format_id: Optional[str] = None
        self.created = time.time()
        self.updated = self.created
        self.workdir = base / self.id
        self.ctx: Optional[JobContext] = None
        self.error: Optional[AppError] = None
        self.result_path: Optional[Path] = None
        self.result_name: str = ""
        self.result_size: int = 0
        self.runs = 0
        self.finished = threading.Event()
        self.state: dict = {}  # engine resume validators, survives retries

    def touch(self) -> None:
        self.updated = time.time()

    def to_public(self) -> dict:
        prog = self.ctx.progress.to_public() if self.ctx else {"stage": self.status, "downloaded": 0, "total": None, "percent": None, "speed": None, "eta": None}
        out = {"job_id": self.id, "status": self.status, "format_id": self.format_id, "progress": prog, "engine": self.info.engine,
               "error": self.error.to_public() if self.error else None, "attempt": self.runs}
        if self.status == "done":
            out["file"] = {"name": self.result_name, "size": self.result_size}
        return out


class JobManager:
    def __init__(self, router, base_dir: Path = None):
        self.router = router
        self.base = Path(base_dir or settings.download_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self._jobs: dict = {}
        self._lock = threading.Lock()
        self._pool = self._new_pool()
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None

    # ---- lifecycle -------------------------------------------------------
    @staticmethod
    def _new_pool() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs, thread_name_prefix="job")

    def start_background(self) -> None:
        """Start (or restart after `shutdown`) the sweeper and worker pool."""
        self._stop = threading.Event()
        if self._pool is None:
            self._pool = self._new_pool()
        self.purge_leftovers()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="job-sweeper", daemon=True)
        self._sweeper.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.ctx:
                job.ctx.cancel.set()
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        self.purge_leftovers()

    def purge_leftovers(self) -> None:
        """Remove job directories left by a previous process (ephemeral storage hygiene)."""
        for p in self.base.iterdir() if self.base.exists() else []:
            if p.is_dir() and len(p.name) == 32:
                shutil.rmtree(p, ignore_errors=True)

    def _sweep_loop(self) -> None:
        while not self._stop.wait(settings.sweep_interval):
            try:
                self.sweep()
            except Exception:
                logger.exception("sweep failed")

    # ---- creation / lookup ----------------------------------------------
    def create(self, info: MediaInfo, owner: str) -> Job:
        job = Job(info, owner, self.base)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, "Job tidak ditemukan atau sudah kedaluwarsa. Analisis ulang URL-nya.", detail=f"unknown job {job_id[:8]}")
        return job

    def counts(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
        return {"total": len(jobs), "active": sum(j.status in ACTIVE for j in jobs), "running": sum(j.status == "running" for j in jobs)}

    def storage_used(self) -> int:
        total = 0
        for p in self.base.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    # ---- start / cancel / delete ----------------------------------------
    def start(self, job_id: str, format_id: str, owner: str) -> Job:
        job = self.get(job_id)
        fmt = job.info.find_format(format_id)
        if fmt is None:
            raise AppError(ErrorCode.FORMAT_UNAVAILABLE, detail=f"format {format_id!r} not offered")
        with self._lock:
            if job.status in ACTIVE:
                raise AppError(ErrorCode.RATE_LIMITED, "Unduhan untuk job ini sedang berjalan.", detail="already active")
            if job.runs > 0 and not job.finished.is_set():
                raise AppError(ErrorCode.RATE_LIMITED, "Unduhan sebelumnya masih dihentikan. Coba lagi sebentar.", detail="previous run still stopping")
            active = [j for j in self._jobs.values() if j.status in ACTIVE]
            if len(active) >= settings.max_concurrent_jobs + settings.max_queued_jobs:
                raise AppError(ErrorCode.RATE_LIMITED, "Server sedang sibuk. Coba lagi sebentar.", detail="queue full")
            if sum(1 for j in active if j.owner == owner) >= settings.max_active_jobs_per_ip:
                raise AppError(ErrorCode.RATE_LIMITED, "Terlalu banyak unduhan aktif dari koneksi Anda. Tunggu hingga selesai.", detail="per-client job limit")
            if fmt.filesize and fmt.filesize > settings.max_download_bytes:
                raise AppError(ErrorCode.STORAGE_LIMIT, f"File sekitar {fmt.filesize // (1024 * 1024)} MB melebihi batas {settings.max_download_mb} MB.", detail="declared size over limit")
            if self.storage_used() + (fmt.filesize or 0) > settings.max_storage_bytes:
                raise AppError(ErrorCode.STORAGE_LIMIT, "Penyimpanan sementara server penuh. Coba lagi beberapa menit lagi.", detail="storage quota")
            if self._pool is None:
                self._pool = self._new_pool()
            if job.format_id != format_id:  # a different format must not resume the old partial file
                shutil.rmtree(job.workdir, ignore_errors=True)
                job.state = {}
            job.format_id = format_id
            job.status = "queued"
            job.error = None
            job.result_path = None
            job.runs += 1
            job.finished = threading.Event()
            job.workdir.mkdir(parents=True, exist_ok=True)
            job.ctx = JobContext(workdir=job.workdir, state=job.state)
            job.touch()
        self._pool.submit(self._run, job, fmt)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.ctx:
            job.ctx.cancel.set()
        if job.status in ACTIVE:
            job.status = "cancelled"
            job.touch()
        return job

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.ctx:
            job.ctx.cancel.set()
        with self._lock:
            self._jobs.pop(job_id, None)
        self._remove_dir(job)

    # ---- worker ----------------------------------------------------------
    def _run(self, job: Job, fmt) -> None:
        ctx = job.ctx
        try:
            if ctx.cancel.is_set():
                raise Cancelled()
            job.status = "running"
            ctx.update(stage="downloading")
            ctx.deadline = time.monotonic() + settings.job_timeout
            path = self.router.download(job.info, fmt, ctx)
            size = path.stat().st_size
            if size > settings.max_download_bytes:
                path.unlink(missing_ok=True)
                raise AppError(ErrorCode.STORAGE_LIMIT, f"File melebihi batas {settings.max_download_mb} MB.", detail="final size over limit")
            if ctx.cancel.is_set():
                raise Cancelled()
            job.result_path, job.result_size = path, size
            job.result_name = safe_filename(path.name, default="save-hack")
            ctx.update(stage="done", downloaded=size, total=size)
            job.status = "done"
        except Cancelled:
            job.status = "cancelled"
            self._remove_dir(job)
            logger.info("job %s cancelled", job.id[:8])
        except BaseException as exc:  # noqa: BLE001 - worker must never die silently
            if ctx.cancel.is_set():
                # Cancel/delete raced with the worker (e.g. the directory vanished under it): that is a
                # cancellation, not a failure. Remove anything the worker managed to write meanwhile.
                job.status = "cancelled"
                self._remove_dir(job)
                logger.info("job %s cancelled (%s)", job.id[:8], type(exc).__name__)
                return
            err = classify_exception(exc, ErrorCode.DOWNLOAD_FAILED)
            if err.code in (ErrorCode.STORAGE_LIMIT, ErrorCode.CONVERSION_FAILED, ErrorCode.FORMAT_UNAVAILABLE):
                self._remove_dir(job)  # nothing worth resuming
            logger.error("job %s failed: %s | %s", job.id[:8], err.code.value, err.detail or exc, exc_info=not isinstance(exc, AppError))
            job.error = err
            job.status = "failed"
        finally:
            job.touch()
            job.finished.set()

    def _remove_dir(self, job: Job) -> None:
        shutil.rmtree(job.workdir, ignore_errors=True)

    # ---- housekeeping ----------------------------------------------------
    def sweep(self, now: float = None) -> int:
        now = time.time() if now is None else now
        expired = []
        with self._lock:
            for j in self._jobs.values():
                if j.status in ACTIVE:
                    continue
                ttl = settings.job_ttl if j.status in TERMINAL else settings.job_ttl * 2
                if now - j.updated > ttl:
                    expired.append(j)
            for j in expired:
                self._jobs.pop(j.id, None)
        for j in expired:
            self._remove_dir(j)
        if expired:
            logger.info("swept %d expired jobs", len(expired))
        return len(expired)
