"""Example provider plugin for Dounai Input Gateway.

It calls a user-owned service:

    POST <endpoint>   multipart/form-data: file=<audio>, language=<optional>
    -> 200 {"text": "transcript"}

Install this package in the same environment as ``dounai-input``; the entry point
registers the adapter at server startup.  This package is optional and is not a
dependency of the core gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from dounai_input.errors import ProviderError
from dounai_input.providers.base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class ExampleHttpProvider(BaseProvider):
    manifest = ProviderManifest(
        id="example-http",
        name="Example self-hosted ASR",
        version="0.1.0",
        description="示例插件：把音频转发到自建 HTTP 转写服务。",
        capabilities=("audio",),
        privacy="remote",
        requires_api_key=True,
        can_transcribe=True,
        homepage="https://github.com/zhipianxiaokonglong/Dounai_input",
    )

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)
        self.endpoint = str(self.config.get("endpoint", "")).strip()
        self.token_env = str(self.config.get("token_env", "EXAMPLE_ASR_TOKEN"))
        self.model = str(self.config.get("model", "example-asr"))
        self.timeout = float(self.config.get("timeout_seconds", 30))

    def availability(self) -> tuple[bool, str | None]:
        if not self.endpoint:
            return False, "Set providers.example-http.endpoint in dounai.toml."
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "endpoint must be an absolute http(s) URL."
        if not os.getenv(self.token_env, "").strip():
            return False, f"Set {self.token_env} for the endpoint."
        return True, None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "Example provider is not configured.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        audio = await asyncio.to_thread(request.path.read_bytes)
        data = {}
        if request.language:
            data["language"] = request.language
        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout, request.timeout_seconds)
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {os.environ[self.token_env]}"},
                    data=data,
                    files={"file": (request.path.name, audio, request.mime)},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "Example endpoint timed out.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("example-http network error: %s", type(exc).__name__)
            raise ProviderError(
                "Could not reach the example endpoint.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"Example endpoint returned HTTP {response.status_code}.",
                provider_id=self.manifest.id,
                retryable=response.status_code >= 500,
            )
        try:
            text = response.json()["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                'Example endpoint must return JSON {"text": "..."}.',
                provider_id=self.manifest.id,
                retryable=False,
            ) from exc
        if not isinstance(text, str):
            raise ProviderError(
                "Example endpoint text field must be a string.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        return TranscriptionResult(
            text=text.strip(),
            provider_id=self.manifest.id,
            model=self.model,
            language=request.language,
        )
