"""Engine C: generic HTML media parser.

Inspects a public HTML page for Open Graph / Twitter Card metadata, JSON-LD,
<video>/<audio>/<source>/<track>/<img> tags, download links, embeds and media
URLs inside inline scripts. Candidates are then *probed* (a Range 0-0 request)
so that only URLs that really serve media or files are offered; arbitrary HTML
links are never downloaded.
"""
import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

from ..config import settings
from ..errors import AppError
from ..models import FormatOption, MediaInfo
from ..net import open_stream, raise_for_status, read_limited
from ..security import normalize_url, safe_filename
from .base import Engine
from .direct import FILE_EXTENSIONS, HTML_TYPES, DirectEngine, kind_for

logger = logging.getLogger("save_hack.html")

STREAM_EXT = {".m3u8", ".mpd"}
MEDIA_URL_START_RE = re.compile(r"https?:\\?/\\?/", re.I)
MEDIA_URL_SUFFIX_RE = re.compile(r"\.(?:mp4|webm|m4v|mov|mp3|m4a|m3u8|mpd)(?:\?.*)?$", re.I)
MAX_MEDIA_URL_LENGTH = 2048
MAX_MEDIA_SCAN_CHARS = 400_000
ICON_HINTS = ("sprite", "favicon", "icon", "logo", "pixel", "tracking", "spacer", "1x1", "blank", "avatar", "emoji")
MAX_CANDIDATES = 30
MAX_PROBES = 10


def _extract_media_urls(text: str):
    """Extract bounded media URLs without backtracking over untrusted page text."""
    text = text[:MAX_MEDIA_SCAN_CHARS]
    for match in MEDIA_URL_START_RE.finditer(text):
        start = match.start()
        end = match.end()
        limit = min(len(text), start + MAX_MEDIA_URL_LENGTH)
        while end < limit:
            char = text[end]
            if ord(char) <= 32 or ord(char) in (34, 39, 60, 62, 92, 41):
                break
            end += 1
        raw = text[start:end]
        if end == limit and end < len(text) and ord(text[end]) > 32 and ord(text[end]) not in (34, 39, 60, 62, 92, 41):
            continue
        normalized = raw.replace("\\/", "/")
        if MEDIA_URL_SUFFIX_RE.search(normalized):
            yield normalized

class NotHtml(Exception):
    """The URL answered with something that is not an HTML page."""


class Candidate:
    __slots__ = ("url", "kind", "source", "mime", "label")

    def __init__(self, url, kind, source, mime="", label=""):
        self.url, self.kind, self.source, self.mime, self.label = url, kind, source, mime, label

    def __repr__(self):
        return f"Candidate({self.kind},{self.source},{self.url})"


class PageData:
    def __init__(self):
        self.title = ""
        self.site_name = ""
        self.thumbnail = None
        self.duration = None
        self.uploader = ""
        self.candidates = []  # media/file candidates
        self.embeds = []  # iframe embed page URLs
        self.subtitles = []  # (lang, url)
        self._seen = set()

    def add(self, url, kind, source, mime="", label=""):
        if not url or url in self._seen or len(self.candidates) >= MAX_CANDIDATES:
            return
        self._seen.add(url)
        self.candidates.append(Candidate(url, kind, source, mime, label))


def parse_iso_duration(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _guess_kind(url: str, mime: str = "") -> str:
    ext = Path(urlsplit(url).path.lower()).suffix
    if ext in STREAM_EXT or "mpegurl" in mime or "dash+xml" in mime:
        return "stream"
    return kind_for(ext, mime)


class _Collector(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.meta = {}
        self.page = PageData()
        self.in_title = False
        self.in_jsonld = False
        self._jsonld_buf = []
        self._script_buf = []
        self.in_script = False
        self.jsonld_blocks = []
        self.script_text = []
        self.parent = []

    def _abs(self, value):
        if not value or value.startswith(("data:", "blob:", "javascript:", "#", "mailto:")):
            return None
        try:
            full = urljoin(self.base, value.strip())
            return full if full.startswith(("http://", "https://")) else None
        except ValueError:
            return None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        p = self.page
        if tag == "base" and a.get("href"):
            joined = self._abs(a["href"])
            if joined:
                self.base = joined
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"])
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self.in_jsonld = True
                self._jsonld_buf = []
            else:
                self.in_script = True
                self._script_buf = []
        elif tag in ("video", "audio"):
            self.parent.append(tag)
            src = self._abs(a.get("src"))
            if src:
                p.add(src, tag, f"{tag}-tag", a.get("type", ""))
            poster = self._abs(a.get("poster"))
            if poster and not p.thumbnail:
                p.thumbnail = poster
        elif tag == "source":
            src = self._abs(a.get("src"))
            if src:
                parent = self.parent[-1] if self.parent else ""
                kind = _guess_kind(src, a.get("type", ""))
                if kind in ("file", "stream") and parent in ("video", "audio"):
                    kind = kind if kind == "stream" else parent
                p.add(src, kind, "source-tag", a.get("type", ""), a.get("title", ""))
        elif tag == "track":
            src = self._abs(a.get("src"))
            if src:
                p.subtitles.append((a.get("srclang") or a.get("label") or "und", src))
        elif tag == "img":
            src = self._abs(a.get("src") or a.get("data-src"))
            if src and not any(h in src.lower() for h in ICON_HINTS) and _guess_kind(src) in ("image", "file"):
                w, h = a.get("width", ""), a.get("height", "")
                if (w.isdigit() and int(w) < 64) or (h.isdigit() and int(h) < 64):
                    return
                p.add(src, "image", "img-tag", "", a.get("alt", ""))
        elif tag == "a":
            href = self._abs(a.get("href"))
            if href and (Path(urlsplit(href).path.lower()).suffix in FILE_EXTENSIONS or "download" in a):
                kind = _guess_kind(href)
                if Path(urlsplit(href).path.lower()).suffix or kind != "file":
                    p.add(href, kind, "download-link", "", a.get("title", ""))
        elif tag in ("iframe", "embed"):
            src = self._abs(a.get("src"))
            if src:
                p.embeds.append(src)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in ("video", "audio") and self.parent:
            self.parent.pop()
        elif tag == "script":
            if self.in_jsonld:
                self.jsonld_blocks.append("".join(self._jsonld_buf))
                self.in_jsonld = False
            elif self.in_script:
                self.script_text.append("".join(self._script_buf)[:200_000])
                self.in_script = False

    def handle_data(self, data):
        if self.in_title and not self.page.title:
            self.page.title += data.strip()
        if self.in_jsonld:
            self._jsonld_buf.append(data)
        elif self.in_script:
            self._script_buf.append(data)


def _walk_jsonld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
    elif isinstance(node, dict):
        out.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_jsonld(v, out)


def _first(d, key):
    v = d.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl")
    return v if isinstance(v, str) else None


def parse_html(html: str, base_url: str) -> PageData:
    """Pure function: HTML text -> PageData. No network access."""
    c = _Collector(base_url)
    try:
        c.feed(html)
        c.close()
    except Exception as exc:  # malformed markup must never crash analysis
        logger.debug("html parse issue: %r", exc)
    page, meta = c.page, c.meta

    def m(*keys):
        for k in keys:
            if meta.get(k):
                return meta[k][0].strip()
        return ""

    base = c.base
    page.title = m("og:title", "twitter:title") or page.title
    page.site_name = m("og:site_name") or (urlsplit(base_url).hostname or "")
    page.uploader = m("author", "article:author", "og:site_name")
    thumb = m("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    if thumb and not page.thumbnail:
        page.thumbnail = urljoin(base, thumb)
    elif page.thumbnail:
        page.thumbnail = urljoin(base, page.thumbnail)

    def absu(v):
        return urljoin(base, v) if v else None

    for key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        for v in meta.get(key, []):
            u = absu(v)
            if u:
                page.add(u, _guess_kind(u, "video/") if _guess_kind(u) in ("file",) else _guess_kind(u), key)
    for key in ("og:audio", "og:audio:url", "og:audio:secure_url"):
        for v in meta.get(key, []):
            u = absu(v)
            if u:
                page.add(u, "stream" if _guess_kind(u) == "stream" else "audio", key)
    # og:image / twitter:image are preview thumbnails of the page, not the page's media.
    # They are shown as the thumbnail only and are never offered as a download, so a
    # video page whose video cannot be extracted does not silently return its poster.
    for v in meta.get("twitter:player", []):
        u = absu(v)
        if u:
            page.embeds.append(u)

    for block in c.jsonld_blocks:
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        nodes = []
        _walk_jsonld(data, nodes)
        for n in nodes:
            types = n.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(t in ("VideoObject", "AudioObject", "ImageObject", "MusicRecording") for t in types):
                continue
            if isinstance(n.get("name"), str) and not page.title:
                page.title = n["name"]
            if n.get("duration") and page.duration is None:
                page.duration = parse_iso_duration(n["duration"])
            thumb = _first(n, "thumbnailUrl") or _first(n, "thumbnail")
            if thumb and not page.thumbnail:
                page.thumbnail = absu(thumb)
            cu = _first(n, "contentUrl")
            if cu:
                u = absu(cu)
                kind = _guess_kind(u)
                if kind == "file":
                    kind = {"VideoObject": "video", "AudioObject": "audio", "ImageObject": "image"}.get(types[0], "file")
                page.add(u, kind, "jsonld")
            eu = _first(n, "embedUrl")
            if eu:
                page.embeds.append(absu(eu))

    for script in c.script_text:
        for u in _extract_media_urls(script):
            page.add(u, _guess_kind(u), "script-url")
    for u in _extract_media_urls(html):
        page.add(u, _guess_kind(u), "inline-url")
    page.embeds = list(dict.fromkeys(e for e in page.embeds if e))[:5]
    return page


class HtmlEngine(Engine):
    name = "html"

    def __init__(self, direct: DirectEngine = None, stream_engine=None):
        self.direct = direct or DirectEngine()
        self.stream_engine = stream_engine

    def fetch_page(self, url: str):
        resp, final = open_stream(url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Encoding": "gzip, deflate"})
        try:
            raise_for_status(resp)
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype and ctype not in HTML_TYPES and ctype not in ("text/plain", ""):
                raise NotHtml(ctype)
            raw = read_limited(resp, settings.max_html_bytes)
            enc = resp.encoding if resp.encoding and "html" in ctype else None
            return raw.decode(enc or "utf-8", errors="replace"), final
        finally:
            resp.close()

    def analyze(self, url: str) -> Optional[MediaInfo]:
        html, final = self.fetch_page(url)
        page = parse_html(html, final)
        return self.build_info(url, final, page)

    def build_info(self, url: str, final: str, page: PageData) -> Optional[MediaInfo]:
        formats, probed, kinds = [], 0, []
        host = urlsplit(final).hostname or ""
        for cand in page.candidates:
            if probed >= MAX_PROBES or len(formats) >= 12:
                break
            try:
                cand_url = normalize_url(cand.url)
            except AppError:
                continue
            idx = len(formats)
            if cand.kind == "stream" or Path(urlsplit(cand_url).path.lower()).suffix in STREAM_EXT:
                if self.stream_engine is None:
                    continue
                probed += 1
                try:
                    sinfo = self.stream_engine.analyze(cand_url)
                except AppError as exc:
                    logger.debug("stream candidate %s failed: %s", cand_url, exc.detail)
                    continue
                if not sinfo:
                    continue
                for f in sinfo.formats:
                    f.id = f"c{idx}-{f.id}"
                    f.note = (f.note + " · " if f.note else "") + "stream di halaman"
                    formats.append(f)
                kinds.append(sinfo.kind)
                continue
            probed += 1
            try:
                probe = self.direct.probe(cand_url)
            except AppError as exc:
                logger.debug("candidate %s rejected: %s", cand_url, exc.detail)
                continue
            dinfo = self.direct.build_info(cand_url, probe)
            if dinfo is None:  # served HTML
                continue
            if dinfo.kind == "file" and cand.source != "download-link":
                continue  # unknown blobs are only trusted when the page explicitly links them as downloads
            f = dinfo.formats[0]
            f.id = f"c{idx}"
            f.kind = dinfo.kind if dinfo.kind in ("video", "audio", "image") else "file"
            f.label = f"{f.label} · {cand.source}" if not cand.label else f"{cand.label[:40]} · {f.label}"
            f.payload.update({"downloader": "direct", "direct_url": dinfo.payload["direct_url"], "etag": dinfo.payload.get("etag"),
                              "last_modified": dinfo.payload.get("last_modified")})
            formats.append(f)
            kinds.append(dinfo.kind)
        if not formats:
            info = MediaInfo(url=url, engine=self.name, site=page.site_name or host, title=page.title or host, kind="file")
            info.payload["embeds"] = page.embeds
            return info if page.embeds else None
        kind = "video" if "video" in kinds else "audio" if "audio" in kinds else "image" if "image" in kinds else "file"
        # Prefer video/audio candidates first, then images, then other files.
        order = {"video": 0, "audio": 1, "image": 2, "file": 3, "subtitle": 4}
        formats.sort(key=lambda f: (order.get(f.kind, 5), -(f.height or 0), -(f.filesize or 0)))
        preview = next((f.payload.get("direct_url") for f in formats if f.kind in ("video", "audio", "image") and (f.payload.get("direct_url") or "").startswith("https://")), None)
        title = page.title or Path(formats[0].payload.get("filename", "")).stem or host
        info = MediaInfo(url=url, engine=self.name, site=page.site_name or host, title=title, kind=kind, uploader=page.uploader if page.uploader != page.site_name else "",
                         duration=page.duration, thumbnail=page.thumbnail, preview_url=preview, formats=formats)
        info.payload["embeds"] = page.embeds
        for lang, sub in page.subtitles[:8]:
            try:
                sub_url = normalize_url(sub)
            except AppError:
                continue
            info.formats.append(FormatOption(id=f"sub-{len(info.formats)}", label=f"Subtitle ({lang})", kind="subtitle", ext=Path(urlsplit(sub_url).path).suffix.lstrip(".") or "vtt",
                                             payload={"downloader": "direct", "direct_url": sub_url, "filename": safe_filename(f"{title}.{lang}.vtt")}))
        return info
