"""Data models shared by engines, router, jobs and the API."""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FormatOption:
    """One downloadable choice shown to the user.

    `selector` and `payload` are private instructions for the engine that
    produced the option. They are never sent to the client, so a client can only
    choose from options the server itself generated.
    """
    id: str
    label: str
    kind: str  # video | audio | file | image | subtitle
    ext: str = ""
    container: str = ""
    resolution: str = ""
    height: Optional[int] = None
    width: Optional[int] = None
    vcodec: str = ""
    acodec: str = ""
    filesize: Optional[int] = None
    bitrate: Optional[int] = None
    note: str = ""
    selector: str = ""
    payload: dict = field(default_factory=dict)

    def to_public(self) -> dict:
        return {
            "id": self.id, "label": self.label, "kind": self.kind, "ext": self.ext, "container": self.container,
            "resolution": self.resolution, "height": self.height, "width": self.width, "vcodec": self.vcodec,
            "acodec": self.acodec, "filesize": self.filesize, "bitrate": self.bitrate, "note": self.note,
        }


@dataclass
class MediaInfo:
    url: str
    engine: str
    site: str
    title: str
    kind: str = "video"  # video | audio | image | file | playlist
    uploader: str = ""
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    preview_url: Optional[str] = None
    filesize: Optional[int] = None
    formats: list = field(default_factory=list)
    entries: list = field(default_factory=list)  # playlist items: {title,url,duration}
    attempts: list = field(default_factory=list)  # diagnostics: [{engine, outcome, detail}]
    meta: dict = field(default_factory=dict)  # public extras (audio tracks, stream type, subtitle langs)
    payload: dict = field(default_factory=dict)  # private engine state (e.g. resolved direct URL)

    def find_format(self, format_id: str) -> Optional[FormatOption]:
        for f in self.formats:
            if f.id == format_id:
                return f
        return None

    def to_public(self) -> dict:
        return {
            "url": self.url, "engine": self.engine, "site": self.site, "title": self.title, "kind": self.kind,
            "uploader": self.uploader, "duration": self.duration, "thumbnail": self.thumbnail,
            "preview_url": self.preview_url, "filesize": self.filesize,
            "entries": self.entries, "attempts": self.attempts, "meta": self.meta,
        }


@dataclass
class Progress:
    stage: str = "queued"  # queued | downloading | converting | done
    downloaded: int = 0
    total: Optional[int] = None
    speed: Optional[float] = None
    eta: Optional[float] = None

    @property
    def percent(self) -> Optional[float]:
        if self.total and self.total > 0:
            return round(min(100.0, self.downloaded * 100.0 / self.total), 1)
        return None

    def to_public(self) -> dict:
        return {"stage": self.stage, "downloaded": self.downloaded, "total": self.total,
                "percent": self.percent, "speed": self.speed, "eta": self.eta}


def format_resolution(width: Any, height: Any) -> str:
    if width and height:
        return f"{int(width)}x{int(height)}"
    if height:
        return f"{int(height)}p"
    return ""
