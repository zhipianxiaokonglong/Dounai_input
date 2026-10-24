"""Adapters for OpenAI-compatible speech APIs.

Two protocols are supported because "OpenAI compatible" is not one protocol:

* ``audio_transcriptions``: multipart ``/audio/transcriptions`` (OpenAI).
* ``chat_audio``: one base64 audio message to ``/chat/completions``
  (Alibaba Cloud Model Studio's Qwen3-ASR-Flash uses this shape).

The vendor difference stays inside this adapter; callers see only
``TranscriptionResult``.
"""

from __future__ import annotations

import asyncio
import base64
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

_PROFILES: dict[str, dict[str, Any]] = {
    "dashscope": {
        "name": "通义千问 Qwen3-ASR（阿里云百炼）",
        "description": (
            "推荐第三方方案。Qwen3-ASR-Flash 对中文、方言与中英混说表现稳定；"
            "同一账号还可使用 Qwen3.8-Omni-Flash 做后续全模态理解。BYOK，音频会发送到厂商。"
        ),
        "capabilities": ("text", "image", "audio"),
        "protocol": "chat_audio",
        "default_api_key_env": "DASHSCOPE_API_KEY",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3-asr-flash",
        "homepage": "https://bailian.console.aliyun.com/",
    },
    "openai": {
        "name": "OpenAI Speech-to-Text",
        "description": (
            "OpenAI 官方转写接口，多语言覆盖好。BYOK，音频会发送到 OpenAI；"
            "需要可访问 OpenAI API 的网络环境。"
        ),
        "capabilities": ("text", "image", "audio"),
        "protocol": "audio_transcriptions",
        "default_api_key_env": "OPENAI_API_KEY",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-transcribe",
        "homepage": "https://platform.openai.com/api-keys",
    },
}


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        provider_id: str,
        name: str | None = None,
        description: str | None = None,
        protocol: str = "audio_transcriptions",
        default_api_key_env: str,
        default_base_url: str,
        default_model: str,
        capabilities: tuple[str, ...] = ("text", "image", "audio"),
        homepage: str | None = None,
    ) -> None:
        super().__init__(config)
        self.manifest = ProviderManifest(
            id=provider_id,
            name=name or provider_id,
            version="1.0.0",
            description=description or "",
            capabilities=capabilities,
            privacy="remote",
            requires_api_key=True,
            can_transcribe=True,
            homepage=homepage,
        )
        self.base_url = str(self.config.get("base_url") or default_base_url).rstrip("/")
        self.api_key_env = str(self.config.get("api_key_env") or default_api_key_env)
        self.asr_model = str(self.config.get("asr_model") or default_model)
        self.omni_model = self.config.get("omni_model")
        self.protocol = str(self.config.get("protocol") or protocol)
        self.language = self.config.get("language") or "zh"
        self.timeout_seconds = float(self.config.get("timeout_seconds", 60))
        self._protocol = self.protocol
        if self._protocol not in {"audio_transcriptions", "chat_audio"}:
            raise ValueError(f"Unsupported OpenAI-compatible protocol: {self._protocol}")

    def api_key(self) -> str | None:
        value = os.getenv(self.api_key_env, "").strip()
        return value or None

    def availability(self) -> tuple[bool, str | None]:
        if not self.api_key():
            return False, f"Set {self.api_key_env} to use this provider."
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "base_url must be an absolute http(s) URL."
        if not self.asr_model:
            return False, "asr_model is not configured."
        return True, None

    def info(self) -> dict[str, Any]:
        info = super().info()
        info["model"] = self.asr_model
        info["base_url"] = self.base_url
        return info

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "Provider is not configured.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        api_key = self.api_key()
        if api_key is None:
            raise ProviderError(
                f"Set {self.api_key_env} to use this provider.",
                provider_id=self.manifest.id,
                retryable=False,
            )
        timeout = min(request.timeout_seconds, self.timeout_seconds)
        try:
            if self._protocol == "audio_transcriptions":
                text = await self._transcribe_multipart(request, api_key, timeout)
            else:
                text = await self._transcribe_chat_audio(request, api_key, timeout)
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

    async def _transcribe_multipart(
        self, request: TranscriptionRequest, api_key: str, timeout: float
    ) -> str:
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
                headers={"Authorization": f"Bearer {api_key}"},
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

    async def _transcribe_chat_audio(
        self, request: TranscriptionRequest, api_key: str, timeout: float
    ) -> str:
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
                            "input_audio": {"data": f"data:{request.mime};base64,{encoded}"},
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
                    "Authorization": f"Bearer {api_key}",
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

    def _json_or_error(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status >= 400:
            retryable = status in {408, 409, 425, 429} or status >= 500
            if status in {401, 403}:
                message = f"Authentication failed. Check {self.api_key_env}."
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


def build_provider(
    provider_id: str,
    config: Mapping[str, Any],
    *,
    models_dir: Path,
) -> BaseProvider:
    if provider_id not in _PROFILES:
        raise ValueError(f"Unknown OpenAI-compatible profile: {provider_id}")
    profile = _PROFILES[provider_id]
    return OpenAICompatibleProvider(
        config,
        provider_id=provider_id,
        name=str(profile["name"]),
        description=str(profile["description"]),
        protocol=str(profile["protocol"]),
        default_api_key_env=str(profile["default_api_key_env"]),
        default_base_url=str(profile["default_base_url"]),
        default_model=str(profile["default_model"]),
        capabilities=tuple(profile["capabilities"]),
        homepage=profile.get("homepage"),
    )
