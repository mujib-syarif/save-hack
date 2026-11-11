"""MediaFire public file pages: read the page's own download button link."""
import base64
import re
from urllib.parse import urlsplit

from ..config import settings
from ..errors import AppError, ErrorCode
from ..net import fetch_text
from .base import SiteExtractor

SCRAMBLED_RE = re.compile(r'data-scrambled-url="([^"]+)"', re.I)
HREF_RES = (
    re.compile(r'<a[^>]+(?:id="downloadButton"|aria-label="Download file")[^>]*\shref="(https?://[^"]+)"', re.I),
    re.compile(r'<a[^>]+\shref="(https?://download[^"]*\.mediafire\.com/[^"]+)"', re.I),
)


def find_download_link(html: str):
    m = SCRAMBLED_RE.search(html)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode("utf-8")
        except Exception:
            pass
    for rx in HREF_RES:
        m = rx.search(html)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


class MediaFire(SiteExtractor):
    name = "mediafire"
    label = "MediaFire"
    hosts = ("mediafire.com",)

    def extract(self, url, direct):
        path = urlsplit(url).path
        if not (path.startswith("/file/") or path.startswith("/view/") or path.startswith("/download/")):
            if path.startswith(("/folder/", "/?")):
                raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Folder MediaFire belum didukung. Tempel link file tunggal.", detail="mediafire folder")
            return None
        html, final, _ = fetch_text(url, settings.max_html_bytes, headers={"Accept": "text/html"})
        link = find_download_link(html)
        host = (urlsplit(link).hostname or "") if link else ""
        if not link or not (host == "mediafire.com" or host.endswith(".mediafire.com")):
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, "Tombol unduh MediaFire tidak ditemukan. File mungkin dihapus, privat, atau halaman memakai verifikasi.", detail="mediafire: no download link")
        return self.from_direct_url(url, link, direct)
