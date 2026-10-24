import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def public_dns(_host, _port, type=None):
    return [(2, 1, 6, "", ("8.8.8.8", 443))]


def private_dns(_host, _port, type=None):
    return [(2, 1, 6, "", ("127.0.0.1", 443))]


def test_validate_url_accepts_public_https_supported_host(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", public_dns)
    assert main.validate_url("https://www.youtube.com/watch?v=example") == "https://www.youtube.com/watch?v=example"


def test_validate_url_accepts_public_direct_media_url(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", public_dns)
    assert main.validate_url("https://cdn.example.test/video.mp4") == "https://cdn.example.test/video.mp4"


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=example",
    "https://user:password@youtube.com/watch?v=example",
    "https://youtube.com:8443/watch?v=example",
    "https://127.0.0.1/video.mp4",
    "https://unsupported.example.test/page",
])
def test_validate_url_rejects_unsafe_or_unsupported_urls(monkeypatch, url):
    monkeypatch.setattr(main.socket, "getaddrinfo", public_dns)
    with pytest.raises(main.HTTPException) as error:
        main.validate_url(url)
    assert error.value.status_code == 400


def test_validate_url_rejects_private_dns_results(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", private_dns)
    with pytest.raises(main.HTTPException) as error:
        main.validate_url("https://www.youtube.com/watch?v=example")
    assert error.value.status_code == 400


@pytest.mark.parametrize("quality", ["480", "720", "1080", "1440", "2160"])
def test_mp4_quality_selector_never_exceeds_requested_height(quality):
    options = main.make_ytdlp_options("test-job", "mp4", quality)
    assert "height<=" + quality in options["format"]
    assert options["merge_output_format"] == "mp4"


def test_best_quality_selector_uses_best_available_mp4_first():
    options = main.make_ytdlp_options("test-job", "mp4", "best")
    assert options["format"].startswith("bv*[ext=mp4]+ba[ext=m4a]")
    assert options["merge_output_format"] == "mp4"


def test_mp3_uses_audio_extraction_options():
    options = main.make_ytdlp_options("test-job", "mp3", "best")
    assert options["format"] == "bestaudio/best"
    assert options["postprocessors"][0]["preferredcodec"] == "mp3"


def test_health_endpoint():
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_download_rejects_unknown_quality_before_scheduling():
    with TestClient(main.app) as client:
        response = client.post("/download", json={
            "url": "https://www.youtube.com/watch?v=example",
            "format": "mp4",
            "quality": "4k",
            "rights_confirmed": True,
        })
    assert response.status_code == 422
