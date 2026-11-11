"""Dropbox shared links: `dl=1` is Dropbox's documented direct-download switch."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..errors import AppError, ErrorCode
from .base import SiteExtractor


class Dropbox(SiteExtractor):
    name = "dropbox"
    label = "Dropbox"
    hosts = ("dropbox.com", "dropboxusercontent.com")

    def extract(self, url, direct):
        parts = urlsplit(url)
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        if segs[0] == "sh" or segs[:2] == ["scl", "fo"]:
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Folder Dropbox belum didukung. Tempel link file tunggal.", detail="dropbox folder")
        if segs[0] not in ("s", "scl") and not parts.hostname.endswith("dropboxusercontent.com"):
            return None
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("dl", "raw")]
        query.append(("dl", "1"))
        direct_url = urlunsplit(("https", parts.hostname, parts.path, urlencode(query), ""))
        return self.from_direct_url(url, direct_url, direct)
