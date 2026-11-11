"""OneDrive personal share links via the documented shares API.

https://learn.microsoft.com/onedrive/developer/rest-api/api/shares_get :
base64url-encode the sharing URL, prefix it with `u!`, and request
`/shares/{token}/root/content`.
"""
import base64

from .base import SiteExtractor


def encode_share_url(url: str) -> str:
    return "u!" + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


class OneDrive(SiteExtractor):
    name = "onedrive"
    label = "OneDrive"
    hosts = ("1drv.ms", "onedrive.live.com")

    def extract(self, url, direct):
        direct_url = f"https://api.onedrive.com/v1.0/shares/{encode_share_url(url)}/root/content"
        return self.from_direct_url(url, direct_url, direct)
