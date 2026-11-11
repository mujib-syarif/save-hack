"""Plugin contract for website-specific extractors.

A site extractor is deliberately tiny: it recognises its hosts, converts a
share/page URL into a *direct* public file URL using that service's documented
mechanism, and hands that URL to the direct engine. It never touches
authentication, cookies or access controls: if the service answers with a login
page, the extractor reports AUTHENTICATION_REQUIRED.
"""
from typing import Optional
from urllib.parse import urlsplit

from ..engines.direct import DirectEngine
from ..errors import AppError, ErrorCode
from ..models import MediaInfo


class SiteExtractor:
    name = ""  # short id, e.g. "pixeldrain"
    label = ""  # display name
    hosts = ()  # registrable domains; subdomains match too

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def extract(self, url: str, direct: DirectEngine) -> Optional[MediaInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    # helper shared by all plugins
    def from_direct_url(self, source_url: str, direct_url: str, direct: DirectEngine) -> MediaInfo:
        probe = direct.probe(direct_url)
        info = direct.build_info(source_url, probe, site=self.label, engine=f"site:{self.name}")
        if info is None:
            raise AppError(ErrorCode.AUTHENTICATION_REQUIRED,
                           f"{self.label} tidak memberikan file langsung. File mungkin privat, memerlukan login, atau kuota unduhan terlampaui.",
                           detail=f"{self.name}: direct url returned HTML")
        info.payload["downloader"] = "direct"
        return info
