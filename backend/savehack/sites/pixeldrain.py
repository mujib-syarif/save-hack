"""Pixeldrain: documented file API (https://pixeldrain.com/api)."""
import re
from urllib.parse import urlsplit

from ..errors import AppError, ErrorCode
from .base import SiteExtractor

FILE_RE = re.compile(r"^/(?:u|api/file)/([A-Za-z0-9]{3,32})/?$")


class Pixeldrain(SiteExtractor):
    name = "pixeldrain"
    label = "Pixeldrain"
    hosts = ("pixeldrain.com", "pixeldrain.net", "pixeldra.in")

    def extract(self, url, direct):
        path = urlsplit(url).path
        if path.startswith("/l/"):
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Daftar (list) Pixeldrain belum didukung. Buka salah satu file lalu tempel link file-nya.", detail="pixeldrain list")
        m = FILE_RE.match(path)
        if not m:
            return None
        return self.from_direct_url(url, f"https://pixeldrain.com/api/file/{m.group(1)}?download", direct)
