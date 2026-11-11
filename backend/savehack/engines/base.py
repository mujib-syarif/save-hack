"""Engine contract and per-job execution context."""
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..errors import Cancelled
from ..models import FormatOption, MediaInfo, Progress


@dataclass
class JobContext:
    """Everything an engine needs while downloading, independent of the job store."""
    workdir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Progress = field(default_factory=Progress)
    state: dict = field(default_factory=dict)  # engine scratch space that survives retries (resume validators)
    deadline: Optional[float] = None  # monotonic time

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if self.deadline is not None and time.monotonic() > self.deadline:
            from ..errors import AppError, ErrorCode
            raise AppError(ErrorCode.EXTRACTION_TIMEOUT, "Unduhan melewati batas waktu server.", detail="job deadline exceeded")

    def update(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.progress, k, v)


class Engine:
    """An extraction + download backend.

    `analyze` returns a MediaInfo, or None when the engine is simply not
    applicable to the URL. It raises AppError when the engine IS applicable but
    definitively failed (for example authentication required), so the router can
    report the real reason instead of a vague "unsupported".
    """
    name = "base"

    def analyze(self, url: str) -> Optional[MediaInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    def download(self, info: MediaInfo, fmt: FormatOption, ctx: JobContext) -> Path:  # pragma: no cover - interface
        raise NotImplementedError
