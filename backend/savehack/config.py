"""Runtime configuration read from environment variables.

Every value has a conservative default that fits a small Railway container
(ephemeral disk, limited RAM and CPU).
"""
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _origins(raw):
    return [o.strip() for o in (raw or "*").split(",") if o.strip()] or ["*"]


@dataclass
class Settings:
    download_dir: Path = field(default_factory=lambda: Path(os.getenv("DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "downloads"))))
    allowed_origins: list = field(default_factory=lambda: _origins(os.getenv("ALLOWED_ORIGINS")))
    # Size and storage limits (MB). Railway disk is ephemeral and small.
    max_download_mb: int = field(default_factory=lambda: _int("MAX_DOWNLOAD_MB", 300, 1))
    max_storage_mb: int = field(default_factory=lambda: _int("MAX_STORAGE_MB", 1024, 1))
    # Concurrency
    max_concurrent_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_JOBS", 2, 1))
    max_queued_jobs: int = field(default_factory=lambda: _int("MAX_QUEUED_JOBS", 6, 0))
    max_active_jobs_per_ip: int = field(default_factory=lambda: _int("MAX_ACTIVE_JOBS_PER_IP", 2, 1))
    # Timeouts (seconds)
    analyze_timeout: int = field(default_factory=lambda: _int("ANALYZE_TIMEOUT", 45, 5))
    http_timeout: int = field(default_factory=lambda: _int("HTTP_TIMEOUT", 15, 3))
    job_timeout: int = field(default_factory=lambda: _int("JOB_TIMEOUT", 900, 30))
    # Retries / performance
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 3, 0))
    retry_backoff: float = 1.0
    fragment_concurrency: int = field(default_factory=lambda: min(_int("FRAGMENT_CONCURRENCY", 4, 1), 16))
    chunk_size: int = 64 * 1024  # small reads: a dropped connection only loses the in-flight chunk, so resume keeps most progress
    # Housekeeping
    job_ttl: int = field(default_factory=lambda: _int("JOB_TTL_SECONDS", 900, 30))
    analysis_cache_ttl: int = field(default_factory=lambda: _int("ANALYSIS_CACHE_SECONDS", 300, 0))
    sweep_interval: int = 60
    # Rate limiting (requests per minute per client)
    rate_analyze_per_min: int = field(default_factory=lambda: _int("RATE_ANALYZE_PER_MIN", 20, 1))
    rate_download_per_min: int = field(default_factory=lambda: _int("RATE_DOWNLOAD_PER_MIN", 8, 1))
    # Number of trusted reverse proxies in front of the app (Railway edge = 1).
    trusted_proxy_hops: int = field(default_factory=lambda: _int("TRUSTED_PROXY_HOPS", 1, 0))
    enable_docs: bool = field(default_factory=lambda: os.getenv("ENABLE_DOCS", "").lower() in ("1", "true", "yes"))
    legacy_download_waiters: int = field(default_factory=lambda: _int("LEGACY_DOWNLOAD_WAITERS", 4, 1))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    max_playlist_entries: int = 50
    max_html_bytes: int = 2 * 1024 * 1024
    max_manifest_bytes: int = 2 * 1024 * 1024
    max_redirects: int = 5

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    @property
    def max_storage_bytes(self) -> int:
        return self.max_storage_mb * 1024 * 1024


settings = Settings()
