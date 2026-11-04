"""Generic OpenAI-compatible API provider.

The adapter is intentionally *not* bound to vendor profiles or environment
variables: a connection supplies ``base_url``, ``model`` and an explicit API key
from the encrypted local store.  Two audio shapes are supported because
"OpenAI compatible" is not one protocol:

* ``audio_transcriptions``: multipart ``/audio/transcriptions`` (OpenAI, Azure,
  many gateways);
* ``chat_audio``: base64 audio in ``/chat/completions`` (Qwen3-ASR, some gateways).

Callers only see ``TranscriptionResult``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from ..errors import ProviderError
from .base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

TRANSCRIPTION_PROTOCOLS = {"audio_transcriptions", "chat_audio"}


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        protocol: str = "audio_transcriptions",
        capabilities: tuple[str, ...] = ("text", "audio"),
        description: str = "",
        timeout_seconds: float = 60.0,
        language: str = "zh",
        homepage: str | None = None,
    ) -> None:
        super().__init__({})
        if protocol not in TRANSCRIPTION_PROTOCOLS:
            raise ValueError(f"Unsupported OpenAI-compatible protocol: {protocol!r}")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.asr_model = model
        self.protocol = protocol
        self.language = language
        self.timeout_seconds = float(timeout_seconds)
        self.manifest = ProviderManifest(
            id=provider_id,
            name=name,
            version="1.0.0",
            description=description,
            capabilities=capabilities,
            privacy="remote",
            requires_api_key=True,
            can_transcribe=("audio" in capabilities),
            homepage=homepage,
        )

    def availability(self) -> tuple[bool, str | None]:
        if not self._api_key.strip():
            return False, "API key is missing for this connection."
        parsed = httpx.URL(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            return False, "base_url must be an absolute http(s) URL."
        if not self.asr_model:
            return False, "model id is not configured."
        if "audio" not in self.manifest.capabilities:
            return False, "this model was not verified for audio input."
        return True, None

    def info(self) -> dict[str, Any]:
        info = super().info()
        info["model"] = self.asr_model
        info["base_url"] = self.base_url
        info["transcription_protocol"] = self.protocol
        return info

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "Provider is not configured.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        timeout = min(request.timeout_seconds, self.timeout_seconds)
        try:
            if self.protocol == "audio_transcriptions":
                text = await self._transcribe_multipart(request, timeout)
            else:
                text = await self._transcribe_chat_audio(request, timeout)
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "The provider did not respond before the timeout.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("%s network error: %s", self.manifest.id, type(exc).__name__)
            raise ProviderError(
                "Could not reach the provider.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        return TranscriptionResult(
            text=text,
            provider_id=self.manifest.id,
            model=self.asr_model,
            language=request.language or self.language,
        )

    async def _transcribe_multipart(self, request: TranscriptionRequest, timeout: float) -> str:
        audio = await asyncio.to_thread(request.path.read_bytes)
        data: dict[str, str] = {
            "model": self.asr_model,
            "response_format": "json",
        }
        language = request.language or self.language
        if language:
            data["language"] = str(language)
        prompt = request.safe_context()
        if prompt:
            data["prompt"] = prompt
        files = {"file": (request.path.name, audio, request.mime)}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files=files,
            )
        payload = self._json_or_error(response)
        text = payload.get("text")
        if not isinstance(text, str):
            raise ProviderError(
                "The provider returned an unexpected transcription response.",
                provider_id=self.manifest.id,
                retryable=True,
            )
        return text.strip()

    async def _transcribe_chat_audio(self, request: TranscriptionRequest, timeout: float) -> str:
        audio = await asyncio.to_thread(request.path.read_bytes)
        encoded = base64.b64encode(audio).decode("ascii")
        language = request.language or self.language
        body: dict[str, Any] = {
            "model": self.asr_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{request.mime};base64,{encoded}",
                                "format": self._audio_format(request.mime),
                            },
                        }
                    ],
                }
            ],
            "stream": False,
        }
        asr_options: dict[str, Any] = {"enable_itn": False}
        if language:
            asr_options["language"] = str(language)
        body["asr_options"] = asr_options
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        payload = self._json_or_error(response)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "The provider returned an unexpected transcription response.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return "".join(parts).strip()
        raise ProviderError(
            "The provider returned an unexpected transcription response.",
            provider_id=self.manifest.id,
            retryable=True,
        )

    @staticmethod
    def _audio_format(mime: str) -> str:
        mapping = {
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mpeg": "mp3",
            "audio/mp4": "mp4",
            "audio/flac": "flac",
            "audio/ogg": "ogg",
            "audio/webm": "webm",
        }
        return mapping.get(mime, "wav")

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status >= 400:
            retryable = status in {408, 409, 425, 429} or status >= 500
            if status in {401, 403}:
                message = "Authentication failed. Check the connection API key."
            elif status == 404:
                message = "Provider endpoint or model was not found. Check base_url and model."
            elif status == 429:
                message = "The provider rate limit was reached."
            elif status >= 500:
                message = "The provider reported a server error."
            else:
                message = "The provider rejected the transcription request."
            logger.warning(
                "%s HTTP %s during transcription (retryable=%s)",
                self.manifest.id,
                status,
                retryable,
            )
            raise ProviderError(message, provider_id=self.manifest.id, retryable=retryable)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "The provider returned a non-JSON response.",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "The provider returned an unexpected JSON response.",
                provider_id=self.manifest.id,
                retryable=True,
            )
        return payload
