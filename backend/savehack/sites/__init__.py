"""Website-specific extractor registry (Engine E).

Adding a site = adding one module with a `SiteExtractor` subclass and listing it
below. Extractors are independent of each other and of the router.
"""
from typing import Optional

from .base import SiteExtractor
from .dropbox import Dropbox
from .gdrive import GoogleDrive
from .mediafire import MediaFire
from .onedrive import OneDrive
from .pixeldrain import Pixeldrain

REGISTRY = [Pixeldrain(), Dropbox(), GoogleDrive(), OneDrive(), MediaFire()]


def find_site_extractor(url: str) -> Optional[SiteExtractor]:
    for ex in REGISTRY:
        if ex.matches(url):
            return ex
    return None


def site_names() -> list:
    return [ex.name for ex in REGISTRY]
