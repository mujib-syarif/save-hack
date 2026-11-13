"""Engine D: HLS (.m3u8) and MPEG-DASH (.mpd) handler.

Manifests are parsed here to discover qualities, codecs and audio tracks. The
actual segment download and merge is delegated to yt-dlp, whose native HLS/DASH
downloaders run inside the Python egress guard (an external `ffmpeg -i <url>`
would resolve names on its own and bypass the SSRF guard) and are followed by a
local FFmpeg merge. Encrypted-with-DRM streams are refused, never bypassed.
"""
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

from ..config import settings
from ..errors import AppError, ErrorCode
from ..models import FormatOption, MediaInfo, format_resolution
from ..net import fetch_text
from .base import Engine, JobContext
from .html_parser import parse_iso_duration

logger = logging.getLogger("save_hack.streams")

DRM_SCHEMES = ("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed", "9a04f079-9840-4286-ab92-e65be0885f95", "94ce86fb-07ff-4f43-adb8-93d2fa968ca2",
               "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b", "urn:mpeg:dash:mp4protection", "widevine", "playready", "fairplay")
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line: str) -> dict:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(line.split(":", 1)[1] if ":" in line else line)}


def split_codecs(codecs: str):
    """('avc1.64001f,mp4a.40.2') -> (video_codec, audio_codec)."""
    v = a = ""
    for c in [c.strip() for c in (codecs or "").split(",") if c.strip()]:
        base = c.split(".")[0].lower()
        if base in ("mp4a", "ac-3", "ec-3", "opus", "vorbis", "flac", "mp3"):
            a = a or c
        else:
            v = v or c
    return v, a


def parse_hls(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("not an HLS playlist")
    out = {"type": "media", "variants": [], "audio": [], "subtitles": [], "duration": None, "drm": False, "encrypted": False, "live": False}
    total, pending = 0.0, None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF"):
            out["type"] = "master"
            pending = _attrs(line)
        elif pending is not None and not line.startswith("#"):
            res = pending.get("RESOLUTION", "")
            w, h = (int(x) for x in res.split("x")) if re.fullmatch(r"\d+x\d+", res) else (None, None)
            vc, ac = split_codecs(pending.get("CODECS", ""))
            out["variants"].append({"bandwidth": int(pending.get("AVERAGE-BANDWIDTH") or pending.get("BANDWIDTH") or 0) or None,
                                    "width": w, "height": h, "codecs": pending.get("CODECS", ""), "vcodec": vc, "acodec": ac,
                                    "fps": pending.get("FRAME-RATE"), "uri": line, "audio_group": pending.get("AUDIO")})
            pending = None
        elif line.startswith("#EXT-X-MEDIA"):
            a = _attrs(line)
            item = {"name": a.get("NAME", ""), "lang": a.get("LANGUAGE", ""), "group": a.get("GROUP-ID", ""), "uri": a.get("URI"), "default": a.get("DEFAULT") == "YES"}
            if a.get("TYPE") == "AUDIO":
                out["audio"].append(item)
            elif a.get("TYPE") == "SUBTITLES":
                out["subtitles"].append(item)
        elif line.startswith("#EXTINF"):
            m = re.match(r"#EXTINF:([\d.]+)", line)
            if m:
                total += float(m.group(1))
        elif line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")):
            a = _attrs(line)
            method = a.get("METHOD", "NONE").upper()
            fmt = (a.get("KEYFORMAT", "") + a.get("URI", "")).lower()
            if method not in ("NONE", "AES-128") or any(s in fmt for s in ("fairplay", "widevine", "playready", "skd://")):
                out["drm"] = True
            elif method == "AES-128":
                out["encrypted"] = True
    if out["type"] == "media":
        out["duration"] = total or None
        # A media playlist without ENDLIST that is not declared VOD is still being written: a live stream.
        out["live"] = "#EXT-X-ENDLIST" not in text and "#EXT-X-PLAYLIST-TYPE:VOD" not in text
    return out


def hls_key_flags(text: str) -> tuple:
    """(drm, aes128) for the #EXT-X-KEY / #EXT-X-SESSION-KEY lines of one playlist."""
    drm = enc = False
    for line in text.splitlines():
        if line.startswith(("#EXT-X-KEY", "#EXT-X-SESSION-KEY")):
            a = _attrs(line)
            method = a.get("METHOD", "NONE").upper()
            fmt = (a.get("KEYFORMAT", "") + a.get("URI", "")).lower()
            if method not in ("NONE", "AES-128") or any(s in fmt for s in ("fairplay", "widevine", "playready", "skd://")):
                drm = True
            elif method == "AES-128":
                enc = True
    return drm, enc


def parse_dash(text: str) -> dict:
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:  # anywhere: padding must not smuggle a DTD past the check
        raise ValueError("DTD/entities are not allowed in manifests")
    try:
        root = ET.fromstring(text)  # noqa: S314 - DTDs/entities are rejected above, so expansion attacks cannot occur
    except ET.ParseError as exc:
        raise ValueError(f"invalid MPD: {exc}")
    if not root.tag.lower().endswith("mpd"):
        raise ValueError("not an MPD")
    out = {"type": "dash", "variants": [], "audio": [], "subtitles": [], "duration": parse_iso_duration(root.get("mediaPresentationDuration")), "drm": False,
           "live": (root.get("type") or "static").lower() == "dynamic"}

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    # ContentProtection may sit on the MPD, Period, AdaptationSet or Representation: look at every element.
    for el in root.iter():
        if local(el.tag) == "ContentProtection":
            scheme = (el.get("schemeIdUri") or "").lower()
            if any(s in scheme for s in DRM_SCHEMES):
                out["drm"] = True
    for aset in root.iter():
        if local(aset.tag) != "AdaptationSet":
            continue
        for rep in aset:
            if local(rep.tag) != "Representation":
                continue
            mime = rep.get("mimeType") or aset.get("mimeType") or ""
            ctype = (aset.get("contentType") or mime.split("/")[0]).lower()
            codecs = rep.get("codecs") or aset.get("codecs") or ""
            bw = int(rep.get("bandwidth") or 0) or None
            if ctype == "video":
                vc, _ = split_codecs(codecs)
                w = int(rep.get("width") or aset.get("width") or 0) or None
                h = int(rep.get("height") or aset.get("height") or 0) or None
                out["variants"].append({"bandwidth": bw, "width": w, "height": h, "codecs": codecs, "vcodec": vc or codecs, "acodec": "", "fps": rep.get("frameRate"), "uri": rep.get("id", ""), "audio_group": None})
            elif ctype == "audio":
                _, ac = split_codecs(codecs)
                out["audio"].append({"name": aset.get("label") or rep.get("id", ""), "lang": aset.get("lang", ""), "group": "", "uri": None, "default": False, "codecs": ac or codecs, "bandwidth": bw})
            elif ctype in ("text", "application"):
                out["subtitles"].append({"name": aset.get("label") or "", "lang": aset.get("lang", ""), "group": "", "uri": None, "default": False})
    return out


def build_formats(parsed: dict, manifest_url: str, stream_type: str) -> list:
    """Turn a parsed manifest into user-facing options (best, one per height, audio-only)."""
    best_per_height = {}
    for v in parsed["variants"]:
        h = v["height"] or 0
        cur = best_per_height.get(h)
        if cur is None or (v["bandwidth"] or 0) > (cur["bandwidth"] or 0):
            best_per_height[h] = v
    base = {"downloader": "stream", "manifest_url": manifest_url, "stream_type": stream_type}
    formats = []
    video_variants = sorted((v for h, v in best_per_height.items() if h), key=lambda v: v["height"], reverse=True)
    tracks = ", ".join(dict.fromkeys(a["name"] or a["lang"] for a in parsed["audio"] if (a["name"] or a["lang"])))
    if video_variants:
        top = video_variants[0]
        formats.append(FormatOption(id="best", label=f"Terbaik ({top['height']}p) · MP4", kind="video", ext="mp4", container="mp4",
                                    resolution=format_resolution(top["width"], top["height"]), height=top["height"], width=top["width"],
                                    vcodec=top["vcodec"], acodec=top["acodec"], bitrate=top["bandwidth"],
                                    note=(f"Audio: {tracks}" if tracks else "Gabungan video+audio"), payload={**base, "height": None, "mode": "video"}))
        for v in video_variants:
            formats.append(FormatOption(id=f"h{v['height']}", label=f"{v['height']}p · MP4", kind="video", ext="mp4", container="mp4",
                                        resolution=format_resolution(v["width"], v["height"]), height=v["height"], width=v["width"],
                                        vcodec=v["vcodec"], acodec=v["acodec"], bitrate=v["bandwidth"],
                                        note=(f"{round(v['bandwidth'] / 1_000_000, 1)} Mbps" if v["bandwidth"] else ""), payload={**base, "height": v["height"], "mode": "video"}))
    elif parsed["variants"]:  # variants without resolution (e.g. a single unlabeled rendition)
        v = parsed["variants"][0]
        formats.append(FormatOption(id="best", label="Terbaik · MP4", kind="video", ext="mp4", container="mp4", vcodec=v["vcodec"], acodec=v["acodec"],
                                    bitrate=v["bandwidth"], payload={**base, "height": None, "mode": "video"}))
    elif parsed["type"] == "media":
        formats.append(FormatOption(id="best", label="Stream tunggal · MP4", kind="video", ext="mp4", container="mp4", payload={**base, "height": None, "mode": "video"}))
    formats.append(FormatOption(id="mp3", label="Audio saja · MP3", kind="audio", ext="mp3", container="mp3", acodec="mp3", note=(f"Trek: {tracks}" if tracks else ""),
                                payload={**base, "height": None, "mode": "mp3"}))
    return formats


class StreamEngine(Engine):
    name = "stream"

    @staticmethod
    def looks_like_stream_url(url: str) -> bool:
        return Path(urlsplit(url).path.lower()).suffix in (".m3u8", ".mpd")

    def analyze(self, url: str) -> Optional[MediaInfo]:
        text, final, ctype = fetch_text(url, settings.max_manifest_bytes, headers={"Accept": "*/*"})
        stripped = text.lstrip("﻿ \r\n\t")
        try:
            if stripped.startswith("#EXTM3U"):
                parsed, stream_type = parse_hls(stripped), "hls"
            elif stripped.startswith("<") and "<mpd" in stripped[:2048].lower():
                parsed, stream_type = parse_dash(stripped), "dash"
            else:
                return None
        except ValueError as exc:
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Manifest stream tidak valid atau tidak didukung.", detail=str(exc))
        if stream_type == "hls" and parsed["type"] == "master" and not parsed["drm"]:
            self._inspect_variant(parsed, final)
        if parsed.get("live"):
            raise AppError(ErrorCode.UNSUPPORTED_SOURCE, "Siaran langsung (live) belum bisa diunduh.", detail="live manifest")
        if parsed["drm"]:
            raise AppError(ErrorCode.SOURCE_BLOCKED, "Stream ini dilindungi DRM. Layanan ini tidak mengunduh konten ber-DRM.", detail="DRM markers in manifest")
        formats = build_formats(parsed, final, stream_type)
        if not formats:
            raise AppError(ErrorCode.MEDIA_NOT_FOUND, "Tidak ada track video atau audio di stream ini.", detail="empty manifest")
        name = Path(urlsplit(final).path).stem or urlsplit(final).hostname or "stream"
        tracks = [a["name"] or a["lang"] for a in parsed["audio"] if (a["name"] or a["lang"])]
        codecs = sorted({c for v in parsed["variants"] for c in (v["vcodec"], v["acodec"]) if c})
        info = MediaInfo(url=url, engine=self.name, site=urlsplit(final).hostname or "", title=name, kind="video" if parsed["variants"] or parsed["type"] == "media" else "audio",
                         duration=parsed["duration"], formats=formats,
                         meta={"stream_type": stream_type, "playlist_type": parsed["type"], "audio_tracks": tracks, "codecs": codecs,
                               "encrypted_aes128": bool(parsed.get("encrypted")), "subtitle_tracks": [s["name"] or s["lang"] for s in parsed["subtitles"]]},
                         payload={"downloader": "stream", "manifest_url": final})
        return info

    @staticmethod
    def _inspect_variant(parsed: dict, base_url: str) -> None:
        """A master playlist carries no #EXT-X-KEY of its own: DRM and liveness live in the media playlists.

        Fetch the first video variant (and first audio rendition) through the guarded client and check them.
        A fetch failure is not fatal here; the download itself still runs inside the egress guard.
        """
        uris = [v["uri"] for v in parsed["variants"][:1]] + [a["uri"] for a in parsed["audio"][:1] if a.get("uri")]
        for uri in uris:
            try:
                text, _final, _ctype = fetch_text(urljoin(base_url, uri), settings.max_manifest_bytes, headers={"Accept": "*/*"})
            except AppError as exc:
                if exc.code in (ErrorCode.AUTHENTICATION_REQUIRED, ErrorCode.SOURCE_BLOCKED):
                    raise
                logger.info("variant playlist not inspected: %s", exc.code)
                continue
            if not text.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"):
                continue
            drm, enc = hls_key_flags(text)
            parsed["drm"] = parsed["drm"] or drm
            parsed["encrypted"] = parsed.get("encrypted") or enc
            if "#EXT-X-ENDLIST" not in text and "#EXT-X-PLAYLIST-TYPE:VOD" not in text:
                parsed["live"] = True

    def download(self, info: MediaInfo, fmt: FormatOption, ctx: JobContext) -> Path:
        from .ytdlp_engine import run_ytdlp_download, stream_selector
        manifest = fmt.payload.get("manifest_url") or info.payload.get("manifest_url") or info.url
        mode = fmt.payload.get("mode", "video")
        selector = stream_selector(fmt.payload.get("height"), mode)
        return run_ytdlp_download(manifest, selector, ctx, audio_mp3=(mode == "mp3"), subtitle_lang=None)
