"""Guarded HTTP client shared by the direct, HTML and manifest engines.

* One pooled `requests.Session` (connection pooling / keep-alive).
* Environment proxies are ignored, so traffic cannot be rerouted around the guard.
* Redirects are followed manually; every hop is re-validated and resolved under
  `egress_guard`, so a redirect to localhost, a private range or a cloud
  metadata endpoint is refused.
"""
import logging
import threading
import time
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

from .config import settings
from .errors import AppError, ErrorCode
from .security import egress_guard, validate_url

logger = logging.getLogger("save_hack.net")

USER_AGENT = "Mozilla/5.0 (compatible; SaveHack/1.0; +https://github.com/mujib-syarif/save-hack)"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

_session = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.trust_env = False  # never inherit HTTP(S)_PROXY / netrc
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers["User-Agent"] = USER_AGENT
        _session = s
    return _session


def raise_for_status(resp) -> None:
    """Map an HTTP error status onto the structured error categories."""
    code = resp.status_code
    if code < 400:
        return
    detail = f"HTTP {code} from {resp.url}"
    if code == 401:
        raise AppError(ErrorCode.AUTHENTICATION_REQUIRED, detail=detail)
    if code in (402, 407):
        raise AppError(ErrorCode.AUTHENTICATION_REQUIRED, detail=detail)
    if code == 403:
        raise AppError(ErrorCode.SOURCE_BLOCKED, detail=detail)
    if code in (404, 410):
        raise AppError(ErrorCode.MEDIA_NOT_FOUND, detail=detail)
    if code == 429:
        raise AppError(ErrorCode.RATE_LIMITED, detail=detail)
    if code == 416:
        raise AppError(ErrorCode.DOWNLOAD_FAILED, detail=detail, retryable=False)
    if code >= 500:
        raise AppError(ErrorCode.NETWORK_ERROR, detail=detail)
    raise AppError(ErrorCode.DOWNLOAD_FAILED, detail=detail, retryable=False)


def open_stream(url: str, headers: dict = None, method: str = "GET", timeout: float = None):
    """Open `url` with validated manual redirects. Returns (response, final_url).

    The caller must close the response. Does not raise on HTTP error statuses;
    call `raise_for_status` if needed.
    """
    timeout = timeout or settings.http_timeout
    current = url
    hdrs = {"Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    for _ in range(settings.max_redirects + 1):
        current = validate_url(current)
        try:
            with egress_guard():
                resp = get_session().request(method, current, headers=hdrs, stream=True, timeout=(timeout, timeout), allow_redirects=False)
        except requests.exceptions.Timeout as exc:
            raise AppError(ErrorCode.EXTRACTION_TIMEOUT, detail=repr(exc))
        except requests.exceptions.SSLError as exc:
            raise AppError(ErrorCode.NETWORK_ERROR, detail=f"tls: {exc!r}")
        except requests.exceptions.RequestException as exc:
            raise AppError(ErrorCode.NETWORK_ERROR, detail=repr(exc))
        if resp.status_code in REDIRECT_STATUSES and resp.headers.get("Location"):
            nxt = urljoin(current, resp.headers["Location"])
            resp.close()
            logger.debug("redirect %s -> %s", current, nxt)
            current = nxt
            if resp.status_code == 303:
                method = "GET"
            continue
        return resp, current
    raise AppError(ErrorCode.NETWORK_ERROR, "Terlalu banyak pengalihan (redirect) dari sumber.", detail="redirect limit")


def _iter_available(resp):
    """Yield body bytes as they arrive (read1) so a trickling server cannot hold a 64 KB chunk read open."""
    raw = getattr(resp, "raw", None)
    if raw is not None and hasattr(raw, "read1"):
        while True:
            chunk = raw.read1(64 * 1024, decode_content=True)
            if not chunk:
                return
            yield chunk
    else:
        yield from resp.iter_content(chunk_size=64 * 1024)


def read_limited(resp, limit: int, deadline: float = None) -> bytes:
    """Read at most `limit` bytes of a streamed response (never loads huge bodies).

    The per-read socket timeout does not bound a server that trickles one byte every few seconds (a chunk read
    blocks until it is full), so the whole read also gets a wall-clock deadline (default: the analysis timeout).
    A watchdog closes the connection at the deadline, which unblocks the pending read.
    """
    deadline = deadline if deadline is not None else time.monotonic() + settings.analyze_timeout
    watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), resp.close)
    watchdog.daemon = True
    watchdog.start()
    chunks, size = [], 0
    try:
        for chunk in _iter_available(resp):
            if time.monotonic() > deadline:
                raise AppError(ErrorCode.EXTRACTION_TIMEOUT, detail="response read exceeded its deadline")
            if not chunk:
                continue
            size += len(chunk)
            chunks.append(chunk)
            if size >= limit:
                break
    except AppError:
        raise
    except Exception as exc:  # the watchdog closing the socket surfaces as assorted I/O errors
        if time.monotonic() >= deadline:
            raise AppError(ErrorCode.EXTRACTION_TIMEOUT, detail=f"response read exceeded its deadline ({exc.__class__.__name__})")
        raise
    finally:
        watchdog.cancel()
    return b"".join(chunks)[:limit]


def fetch_text(url: str, limit: int, headers: dict = None, timeout: float = None):
    """GET a small text resource. Returns (text, final_url, content_type)."""
    resp, final = open_stream(url, headers=headers, timeout=timeout)
    try:
        raise_for_status(resp)
        data = read_limited(resp, limit)
        ctype = resp.headers.get("Content-Type", "")
        enc = resp.encoding or "utf-8"
        return data.decode(enc, errors="replace"), final, ctype
    finally:
        resp.close()
