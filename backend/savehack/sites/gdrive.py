"""Google Drive: publicly shared files only (no sign-in, no quota bypass)."""
import re
from urllib.parse import parse_qs, urlsplit

from ..errors import AppError, ErrorCode
from .base import SiteExtractor

ID_PATH_RE = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


class GoogleDrive(SiteExtractor):
    name = "gdrive"
    label = "Google Drive"
    hosts = ("drive.google.com", "drive.usercontent.google.com")

    def extract(self, url, direct):
        parts = urlsplit(url)
        if "/folders/" in parts.path or "/drive/folders" in parts.path:
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Folder Google Drive belum didukung. Tempel link file tunggal.", detail="gdrive folder")
        m = ID_PATH_RE.search(parts.path)
        file_id = m.group(1) if m else (parse_qs(parts.query).get("id") or [""])[0]
        if not ID_RE.match(file_id):
            return None
        direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        return self.from_direct_url(url, direct_url, direct)
