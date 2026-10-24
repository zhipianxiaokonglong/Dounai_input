"""Adapter for a user-owned HTTP inference service.

This is the escape hatch for a team's own on-prem model or a product/device
gateway that exposes a simple multipart transcription endpoint.  It deliberately
implements one tiny contract instead of pretending to support every proprietary
API:

    POST <endpoint>  multipart/form-data  file=<audio>, language=<optional>
    -> 200 JSON with a configurable text field (default ``{"text": "..."}``)

Authentication is optional and read from an environment variable at call time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..errors import ProviderError
from .base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class CustomHttpProvider(BaseProvider):
    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)
        self.endpoint = str(self.config.get("endpoint", "")).strip()
        self.token_env = str(self.config.get("token_env", "DOUNAI_CUSTOM_HTTP_TOKEN"))
        self.timeout_seconds = float(self.config.get("timeout_seconds", 30))
        self.text_field = str(self.config.get("text_field", "text"))
        self.model_name = str(self.config.get("model", "self-hosted"))
        capabilities = tuple(self.config.get("capabilities", ["audio"]))
        self.manifest = ProviderManifest(
            id="custom-http",
            name=str(self.config.get("name", "自有 HTTP 模型服务")),
            version="1.0.0",
            description=(
                "把音频转发到你自己的模型服务。适用于自建 ASR、私有云或设备侧网关；"
                "端点与鉴权均由你控制。"
            ),
            capabilities=capabilities,
            privacy=str(self.config.get("privacy", "remote")),
            requires_api_key=False,
            can_transcribe=True,
        )

    def _token(self) -> str | None:
        value = os.getenv(self.token_env, "").strip()
        return value or None

    def availability(self) -> tuple[bool, str | None]:
        if not self.endpoint:
            return False, "Set providers.custom-http.endpoint in dounai.toml."
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "custom-http endpoint must be an absolute http(s) URL."
        return True, None

    def info(self) -> dict[str, Any]:
        info = super().info()
        info["model"] = self.model_name
        return info

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "Custom HTTP provider is not configured.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        audio = await asyncio.to_thread(request.path.read_bytes)
        form: dict[str, str] = {}
        if request.language:
            form["language"] = request.language
        prompt = request.safe_context()
        if prompt:
            form["prompt"] = prompt
        headers: dict[str, str] = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = min(request.timeout_seconds, self.timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    data=form,
                    files={"file": (request.path.name, audio, request.mime)},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "The custom endpoint timed out.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("custom-http network error: %s", type(exc).__name__)
            raise ProviderError(
                "Could not reach the custom endpoint.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise ProviderError(
                f"Custom endpoint returned HTTP {response.status_code}.",
                provider_id=self.manifest.id,
                retryable=retryable,
            )
        try:
            payload: Any = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "Custom endpoint returned non-JSON data.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        text = _read_field(payload, self.text_field)
        if not isinstance(text, str):
            raise ProviderError(
                f"Custom endpoint response is missing string field {self.text_field!r}.",
                provider_id=self.manifest.id,
                retryable=True,
            )
        return TranscriptionResult(
            text=text.strip(),
            provider_id=self.manifest.id,
            model=self.model_name,
            language=request.language,
        )


def _read_field(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def provider_factory(config: Mapping[str, Any], *, models_dir: Path) -> CustomHttpProvider:
    del models_dir
    return CustomHttpProvider(config)
