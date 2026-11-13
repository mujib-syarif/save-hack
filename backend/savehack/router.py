"""Smart engine router.

Pipeline for an analysis request:

 1. validate + normalise the URL (SSRF checks)
 2. direct stream manifest (.m3u8/.mpd) or direct file URL (known extension)
 3. website-specific extractor plugin
 4. yt-dlp, but only when yt-dlp's own registry has a dedicated extractor
 5. generic HTML media parser (also detects non-HTML responses as direct files,
    and follows one level of embedded players through steps 3-4)
 6. structured error with per-engine diagnostics

Safety rules: a URL claimed by a specific extractor (plugin or yt-dlp) that then
fails is reported with that failure, and is NOT handed to the HTML parser, so a
poster image or unrelated link can never be returned in place of the real media.
Concurrent requests for the same URL share one extraction, and results are
cached briefly.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Optional

from .config import settings
from .engines import DirectEngine, HtmlEngine, StreamEngine, YtDlpEngine
from .engines import ytdlp_engine
from .engines.base import JobContext
from .engines.html_parser import NotHtml
from .errors import AppError, ErrorCode, classify_exception
from .models import FormatOption, MediaInfo
from .security import validate_url
from .sites import find_site_extractor

logger = logging.getLogger("save_hack.router")

_PRIORITY = [ErrorCode.AUTHENTICATION_REQUIRED, ErrorCode.SOURCE_BLOCKED, ErrorCode.RATE_LIMITED, ErrorCode.EXTRACTION_TIMEOUT,
             ErrorCode.FORMAT_UNAVAILABLE, ErrorCode.MEDIA_NOT_FOUND, ErrorCode.NETWORK_ERROR]
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="analyze")


class Router:
    def __init__(self, direct=None, ytdlp=None, streams=None, html=None):
        self.direct = direct or DirectEngine()
        self.ytdlp = ytdlp or YtDlpEngine()
        self.streams = streams or StreamEngine()
        self.html = html or HtmlEngine(self.direct, self.streams)
        self._cache = {}  # url -> (expires, MediaInfo)
        self._inflight = {}  # url -> {"event", "result", "error"}
        self._lock = threading.Lock()

    # ---- public API ------------------------------------------------------
    def analyze(self, raw_url: str) -> MediaInfo:
        url = validate_url(raw_url)
        with self._lock:
            hit = self._cache.get(url)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            slot = self._inflight.get(url)
            leader = slot is None
            if leader:
                slot = {"event": threading.Event(), "result": None, "error": None}
                self._inflight[url] = slot
        if not leader:  # identical extraction already running: wait for it instead of repeating it
            if not slot["event"].wait(settings.analyze_timeout + 5):
                raise AppError(ErrorCode.EXTRACTION_TIMEOUT)
            if slot["error"]:
                raise slot["error"]
            return slot["result"]
        try:
            fut = _pool.submit(self._pipeline, url)
            try:
                result = fut.result(timeout=settings.analyze_timeout)
            except FutureTimeout:
                fut.cancel()
                raise AppError(ErrorCode.EXTRACTION_TIMEOUT, detail=f"analysis exceeded {settings.analyze_timeout}s")
            slot["result"] = result
            if settings.analysis_cache_ttl > 0:
                with self._lock:
                    self._cache[url] = (time.monotonic() + settings.analysis_cache_ttl, result)
                    if len(self._cache) > 256:
                        now = time.monotonic()
                        for k in [k for k, v in self._cache.items() if v[0] < now]:
                            self._cache.pop(k, None)
            return result
        except BaseException as exc:
            slot["error"] = exc if isinstance(exc, AppError) else classify_exception(exc, ErrorCode.SERVER_ERROR)
            if not isinstance(exc, AppError):
                logger.exception("analysis crashed for %s", url)
            raise slot["error"]
        finally:
            slot["event"].set()
            with self._lock:
                self._inflight.pop(url, None)

    def download(self, info: MediaInfo, fmt: FormatOption, ctx: JobContext):
        """Dispatch to whichever engine produced the option."""
        which = fmt.payload.get("downloader") or info.payload.get("downloader") or info.engine
        if which == "direct":
            return self.direct.download(info, fmt, ctx)
        if which == "ytdlp":
            return self.ytdlp.download(info, fmt, ctx)
        if which == "stream":
            return self.streams.download(info, fmt, ctx)
        raise AppError(ErrorCode.SERVER_ERROR, detail=f"no downloader for {which!r}")

    def engines(self) -> dict:
        from .sites import site_names
        return {"direct": True, "html": True, "stream": True, "ytdlp": ytdlp_engine.available(), "sites": site_names()}

    # ---- pipeline --------------------------------------------------------
    def _attempt(self, name: str, fn, attempts: list, errors: list, retry_network: bool = True) -> Optional[MediaInfo]:
        """Run one engine; record diagnostics; retry once on transient network errors."""
        tries = 2 if retry_network else 1
        for n in range(tries):
            try:
                result = fn()
                if result is None:
                    attempts.append({"engine": name, "outcome": "not_applicable"})
                    return None
                attempts.append({"engine": name, "outcome": "success"})
                return result
            except NotHtml:
                attempts.append({"engine": name, "outcome": "not_html"})
                raise
            except Exception as exc:
                err = classify_exception(exc, ErrorCode.MEDIA_NOT_FOUND)
                if err.code == ErrorCode.NETWORK_ERROR and n + 1 < tries:
                    time.sleep(min(settings.retry_backoff, 2.0))
                    continue
                logger.info("engine %s failed: %s (%s)", name, err.code.value, err.detail)
                attempts.append({"engine": name, "outcome": "failed", "code": err.code.value})
                errors.append((name, err))
                return None
        return None

    def _pipeline(self, url: str) -> MediaInfo:
        attempts, errors = [], []
        claimed = False  # a specific extractor (plugin / yt-dlp) owns this URL

        def done(info: Optional[MediaInfo]):
            if info is None:
                return None
            info.attempts = attempts
            return info

        # 2. manifests and direct file URLs by extension
        if self.streams.looks_like_stream_url(url):
            info = self._attempt("stream", lambda: self.streams.analyze(url), attempts, errors)
            if info:
                return done(info)
        elif self.direct.looks_like_file_url(url):
            info = self._attempt("direct", lambda: self.direct.analyze(url), attempts, errors)
            if info:
                return done(info)

        # 3. website-specific plugin
        site = find_site_extractor(url)
        if site is not None:
            info = self._attempt(f"site:{site.name}", lambda: site.extract(url, self.direct), attempts, errors)
            if info:
                return done(info)
            claimed = claimed or any(n == f"site:{site.name}" for n, _ in errors)

        # 4. yt-dlp, only for URLs its registry recognises
        if not claimed and ytdlp_engine.available():
            info = self._attempt("ytdlp", lambda: self.ytdlp.analyze(url), attempts, errors, retry_network=False)
            if info:
                return done(info)
            claimed = claimed or any(n == "ytdlp" for n, _ in errors)

        # 5. generic HTML parsing (never for a URL a specific extractor claimed and failed on)
        if not claimed:
            try:
                info = self._attempt("html", lambda: self.html.analyze(url), attempts, errors)
            except NotHtml:
                info = self._attempt("direct", lambda: self.direct.analyze(url), attempts, errors)
                if info:
                    return done(info)
            else:
                if info and info.formats:
                    return done(info)
                if info and info.payload.get("embeds"):
                    embedded = self._follow_embeds(info.payload["embeds"], attempts, errors)
                    if embedded:
                        embedded.url = url
                        return done(embedded)

        # Last-resort yt-dlp Generic extractor can recognise public media
        # pages whose player metadata is generated by JavaScript. It runs only
        # after the dedicated extractors and bounded HTML parser found nothing.
        if not claimed and ytdlp_engine.available():
            generic = getattr(self.ytdlp, "analyze_generic", None)
            if callable(generic):
                info = self._attempt("ytdlp_generic", lambda: generic(url), attempts, errors, retry_network=False)
                if info:
                    return done(info)

        raise self._final_error(attempts, errors)

    def _follow_embeds(self, embeds: list, attempts: list, errors: list) -> Optional[MediaInfo]:
        """One level only: embedded players are routed through plugins / yt-dlp / manifests, never re-parsed as HTML."""
        for emb in embeds[:3]:
            try:
                emb_url = validate_url(emb)
            except AppError:
                continue
            site = find_site_extractor(emb_url)
            if site is not None:
                info = self._attempt(f"embed>site:{site.name}", lambda s=site, u=emb_url: s.extract(u, self.direct), attempts, errors)
                if info:
                    return info
            if self.streams.looks_like_stream_url(emb_url):
                info = self._attempt("embed>stream", lambda u=emb_url: self.streams.analyze(u), attempts, errors)
                if info:
                    return info
            if ytdlp_engine.available():
                info = self._attempt("embed>ytdlp", lambda u=emb_url: self.ytdlp.analyze(u), attempts, errors, retry_network=False)
                if info:
                    return info
        return None

    @staticmethod
    def _final_error(attempts: list, errors: list) -> AppError:
        # The specific extractor's reason beats generic "nothing found" noise.
        specific = [e for n, e in errors if (n.startswith("site:") or n in ("ytdlp", "ytdlp_generic")) and e.code != ErrorCode.UNSUPPORTED_SOURCE]
        pool = specific or [e for _, e in errors]
        for code in _PRIORITY:
            for e in pool:
                if e.code == code:
                    return AppError(e.code, e.message, detail=e.detail)
        if pool and pool[0].code != ErrorCode.UNSUPPORTED_SOURCE:
            return AppError(pool[0].code, pool[0].message, detail=pool[0].detail)
        return AppError(ErrorCode.UNSUPPORTED_SOURCE, detail="; ".join(f"{a['engine']}={a['outcome']}" for a in attempts))
