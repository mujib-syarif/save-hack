"""Structured error categories.

User-facing messages are written for normal users and never contain stack
traces or server details. Technical detail stays in `AppError.detail`, which
is only ever logged on the backend.
"""
import socket
import ssl
from enum import Enum


class ErrorCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    EXTRACTION_TIMEOUT = "EXTRACTION_TIMEOUT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    STORAGE_LIMIT = "STORAGE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"


HTTP_STATUS = {
    ErrorCode.INVALID_URL: 400,
    ErrorCode.UNSUPPORTED_SOURCE: 422,
    ErrorCode.MEDIA_NOT_FOUND: 404,
    ErrorCode.FORMAT_UNAVAILABLE: 422,
    ErrorCode.SOURCE_BLOCKED: 422,
    ErrorCode.AUTHENTICATION_REQUIRED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.NETWORK_ERROR: 502,
    ErrorCode.EXTRACTION_TIMEOUT: 504,
    ErrorCode.DOWNLOAD_FAILED: 502,
    ErrorCode.CONVERSION_FAILED: 500,
    ErrorCode.STORAGE_LIMIT: 413,
    ErrorCode.SERVER_ERROR: 500,
}

DEFAULT_MESSAGES = {
    ErrorCode.INVALID_URL: "URL tidak valid atau alamatnya tidak diizinkan.",
    ErrorCode.UNSUPPORTED_SOURCE: "Sumber ini belum didukung atau tidak ada media yang bisa diunduh di halaman tersebut.",
    ErrorCode.MEDIA_NOT_FOUND: "Media tidak ditemukan. Link mungkin sudah dihapus atau tidak publik.",
    ErrorCode.FORMAT_UNAVAILABLE: "Format yang dipilih tidak tersedia untuk media ini. Pilih format lain.",
    ErrorCode.SOURCE_BLOCKED: "Sumber memblokir akses dari server ini. Ini pembatasan dari sumber, bukan kesalahan link.",
    ErrorCode.AUTHENTICATION_REQUIRED: "Media ini membutuhkan login, akun privat, atau verifikasi. Layanan ini hanya mengunduh media publik.",
    ErrorCode.RATE_LIMITED: "Terlalu banyak permintaan. Tunggu sebentar lalu coba lagi.",
    ErrorCode.NETWORK_ERROR: "Gagal terhubung ke sumber. Coba lagi beberapa saat lagi.",
    ErrorCode.EXTRACTION_TIMEOUT: "Sumber terlalu lama merespons. Coba lagi nanti.",
    ErrorCode.DOWNLOAD_FAILED: "Unduhan gagal. Coba lagi.",
    ErrorCode.CONVERSION_FAILED: "Konversi media gagal. Coba format lain.",
    ErrorCode.STORAGE_LIMIT: "File melebihi batas ukuran atau kapasitas penyimpanan server.",
    ErrorCode.SERVER_ERROR: "Terjadi kesalahan di server. Coba lagi nanti.",
}


class AppError(Exception):
    """An error that is safe to show to the user (`message`) with private `detail`."""

    def __init__(self, code: ErrorCode, message: str = None, detail: str = None, status: int = None, retryable: bool = None):
        self.code = code
        self.message = message or DEFAULT_MESSAGES[code]
        self.detail = detail or ""
        self.status = status or HTTP_STATUS[code]
        self.retryable = retryable if retryable is not None else code in {
            ErrorCode.NETWORK_ERROR, ErrorCode.EXTRACTION_TIMEOUT, ErrorCode.DOWNLOAD_FAILED, ErrorCode.RATE_LIMITED, ErrorCode.SERVER_ERROR,
        }
        super().__init__(f"{code.value}: {self.message}")

    def to_public(self) -> dict:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


class Cancelled(Exception):
    """Raised inside workers when the user cancels a job."""


# Ordered (substring, code) rules applied to lower-cased technical messages.
_RULES = [
    (("sign in to confirm", "not a bot", "captcha", "verify you are human", "cf-chl", "just a moment"), ErrorCode.SOURCE_BLOCKED),
    (("drm", "widevine", "playready", "fairplay"), ErrorCode.SOURCE_BLOCKED),
    (("private video", "login required", "log in", "sign in", "members-only", "members only", "requires authentication", "cookies", "this video is private", "age-restricted", "confirm your age", "premium", "subscribe to"), ErrorCode.AUTHENTICATION_REQUIRED),
    (("unsupported url",), ErrorCode.UNSUPPORTED_SOURCE),
    (("requested format is not available", "no video formats found", "format is not available"), ErrorCode.FORMAT_UNAVAILABLE),
    (("http error 404", "video unavailable", "has been removed", "does not exist", "not found", "no longer available", "http error 410", "this video has been"), ErrorCode.MEDIA_NOT_FOUND),
    (("http error 429", "too many requests", "rate limit"), ErrorCode.RATE_LIMITED),
    (("http error 403", "http error 401", "forbidden", "access denied", "geo restrict", "geo-restrict", "geoblock", "not available in your country", "blocked"), ErrorCode.SOURCE_BLOCKED),
    (("timed out", "timeout"), ErrorCode.EXTRACTION_TIMEOUT),
    (("ffmpeg", "postprocess", "conversion", "merger"), ErrorCode.CONVERSION_FAILED),
    (("no space left", "file is larger than max-filesize", "larger than", "exceeds"), ErrorCode.STORAGE_LIMIT),
    (("unable to download", "connection", "temporary failure", "name or service not known", "network is unreachable", "ssl", "remote end closed", "incomplete read", "http error 5"), ErrorCode.NETWORK_ERROR),
]


def classify_message(text: str, fallback: ErrorCode = ErrorCode.DOWNLOAD_FAILED) -> ErrorCode:
    low = (text or "").lower()
    for needles, code in _RULES:
        if any(n in low for n in needles):
            return code
    return fallback


def classify_exception(exc: BaseException, fallback: ErrorCode = ErrorCode.DOWNLOAD_FAILED) -> AppError:
    """Convert any exception into an AppError with a safe message."""
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return AppError(ErrorCode.EXTRACTION_TIMEOUT, detail=repr(exc))
    if isinstance(exc, (socket.gaierror, ssl.SSLError, ConnectionError)):
        return AppError(ErrorCode.NETWORK_ERROR, detail=repr(exc))
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return AppError(ErrorCode.STORAGE_LIMIT, detail=repr(exc))
    code = classify_message(str(exc), fallback)
    return AppError(code, detail=f"{type(exc).__name__}: {exc}")
