"""Application entry point for ASGI servers.

``uvicorn dounai_input.app:app`` works for a default, loopback-only deployment.
The CLI builds an app from an explicit ``dounai.toml`` instead.
"""

from __future__ import annotations

from .api import build_app
from .config import load_settings

app = build_app(load_settings())
