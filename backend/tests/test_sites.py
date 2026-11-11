import base64
import unittest
from unittest.mock import patch

from savehack.errors import AppError, ErrorCode
from savehack.sites import REGISTRY, find_site_extractor
from savehack.sites.base import SiteExtractor
from savehack.sites.mediafire import find_download_link
from savehack.sites.onedrive import encode_share_url


class Capture:
    """Replaces the network step so tests see which direct URL each plugin would fetch."""

    def __init__(self):
        self.urls = []

    def __call__(self, source_url, direct_url, direct):  # patched onto the class as a plain object: no `self` of the extractor
        self.urls.append(direct_url)
        return "INFO"


class SitePluginTests(unittest.TestCase):
    def run_plugin(self, url):
        cap = Capture()
        ex = find_site_extractor(url)
        self.assertIsNotNone(ex, url)
        with patch.object(SiteExtractor, "from_direct_url", cap):
            result = ex.extract(url, None)
        return result, cap.urls

    def test_registry_matches_hosts_and_subdomains_only(self):
        self.assertEqual(find_site_extractor("https://pixeldrain.com/u/abc").name, "pixeldrain")
        self.assertEqual(find_site_extractor("https://www.dropbox.com/s/x/y.mp4").name, "dropbox")
        self.assertEqual(find_site_extractor("https://drive.google.com/file/d/ABCDEFGHIJK/view").name, "gdrive")
        self.assertIsNone(find_site_extractor("https://notdropbox.com/s/x"))
        self.assertIsNone(find_site_extractor("https://evil.example.com/pixeldrain.com/u/abc"))
        self.assertEqual(len({e.name for e in REGISTRY}), len(REGISTRY))

    def test_pixeldrain(self):
        _, urls = self.run_plugin("https://pixeldrain.com/u/AbC123xy")
        self.assertEqual(urls, ["https://pixeldrain.com/api/file/AbC123xy?download"])
        r, urls = self.run_plugin("https://pixeldrain.com/")
        self.assertIsNone(r)
        with self.assertRaises(AppError) as cm:
            self.run_plugin("https://pixeldrain.com/l/listid")
        self.assertEqual(cm.exception.code, ErrorCode.UNSUPPORTED_SOURCE)

    def test_dropbox_forces_dl1_and_keeps_other_params(self):
        _, urls = self.run_plugin("https://www.dropbox.com/s/abc123/clip.mp4?dl=0&rlkey=KEY")
        self.assertEqual(urls, ["https://www.dropbox.com/s/abc123/clip.mp4?rlkey=KEY&dl=1"])
        _, urls = self.run_plugin("https://www.dropbox.com/scl/fi/xyz/clip.mp4?rlkey=K&raw=1")
        self.assertTrue(urls[0].endswith("&dl=1") and "raw" not in urls[0])
        with self.assertRaises(AppError):
            self.run_plugin("https://www.dropbox.com/scl/fo/folderid/h?rlkey=K")

    def test_gdrive(self):
        for u in ("https://drive.google.com/file/d/1AbCdEfGhIjK/view?usp=sharing", "https://drive.google.com/open?id=1AbCdEfGhIjK", "https://drive.google.com/uc?id=1AbCdEfGhIjK&export=download"):
            _, urls = self.run_plugin(u)
            self.assertEqual(urls, ["https://drive.usercontent.google.com/download?id=1AbCdEfGhIjK&export=download&confirm=t"], u)
        with self.assertRaises(AppError) as cm:
            self.run_plugin("https://drive.google.com/drive/folders/1AbCdEfGhIjK")
        self.assertEqual(cm.exception.code, ErrorCode.UNSUPPORTED_SOURCE)
        r, _ = self.run_plugin("https://drive.google.com/")
        self.assertIsNone(r)

    def test_gdrive_id_cannot_inject_extra_parameters(self):
        r, urls = self.run_plugin("https://drive.google.com/open?id=abc%26export%3Dx%26evil%3D1")
        self.assertIsNone(r)
        self.assertEqual(urls, [])

    def test_onedrive_share_token_matches_documented_encoding(self):
        share = "https://1drv.ms/v/s!AbCdEf"
        token = encode_share_url(share)
        self.assertTrue(token.startswith("u!"))
        self.assertNotIn("=", token)
        self.assertNotIn("/", token)
        padded = token[2:] + "=" * (-len(token[2:]) % 4)
        self.assertEqual(base64.urlsafe_b64decode(padded).decode(), share)
        _, urls = self.run_plugin(share)
        self.assertEqual(urls, [f"https://api.onedrive.com/v1.0/shares/{token}/root/content"])

    def test_mediafire_link_extraction(self):
        link = "https://download1234.mediafire.com/abc/file.zip"
        self.assertEqual(find_download_link(f'<a id="downloadButton" class="x" href="{link}">Download</a>'), link)
        scrambled = base64.b64encode(link.encode()).decode()
        self.assertEqual(find_download_link(f'<a data-scrambled-url="{scrambled}" id="downloadButton">'), link)
        self.assertIsNone(find_download_link("<html>no button</html>"))

    def test_mediafire_rejects_links_pointing_off_site_and_folders(self):
        ex = find_site_extractor("https://www.mediafire.com/file/abc/x.zip/file")
        evil = '<a id="downloadButton" href="http://169.254.169.254/latest/meta-data/">Download</a>'
        with patch("savehack.sites.mediafire.fetch_text", return_value=(evil, "u", "text/html")):
            with self.assertRaises(AppError) as cm:
                ex.extract("https://www.mediafire.com/file/abc/x.zip/file", None)
        self.assertEqual(cm.exception.code, ErrorCode.MEDIA_NOT_FOUND)
        with self.assertRaises(AppError) as cm:
            ex.extract("https://www.mediafire.com/folder/abc/x", None)
        self.assertEqual(cm.exception.code, ErrorCode.UNSUPPORTED_SOURCE)

    def test_login_page_instead_of_file_is_reported_as_authentication_required(self):
        from savehack.engines.direct import DirectEngine
        eng = DirectEngine()
        html_probe = {"final_url": "https://x.example.com/", "content_type": "text/html", "disposition": "", "filesize": None, "accept_ranges": False,
                      "etag": None, "last_modified": None, "filename": "", "status": 200}
        with patch.object(DirectEngine, "probe", return_value=html_probe):
            with self.assertRaises(AppError) as cm:
                find_site_extractor("https://pixeldrain.com/u/abc123").extract("https://pixeldrain.com/u/abc123", eng)
        self.assertEqual(cm.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)


if __name__ == "__main__":
    unittest.main()
