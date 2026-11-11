"""ASGI entrypoint (`uvicorn main:app`). All logic lives in the `savehack` package."""
from savehack.api import app  # noqa: F401
