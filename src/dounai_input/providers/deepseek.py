"""DeepSeek speech-to-text tool API adapter.

DeepSeek exposes ASR at ``POST /v1/tools/asr`` (multipart ``file``).  It accepts
16-bit PCM WAV or Ogg Opus, 8 MiB / 120 s per request, and auto-detects the
language.  The desktop records WebM, so this adapter normalizes every input to
16 kHz mono WAV locally before upload; audio never goes anywhere except the
selected DeepSeek endpoint.
"""

from __future__ import annotations

import asyncio
import io
import os
import wave
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..errors import ProviderError
from ..voiceprint import ensure_wav_bytes
from .base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0


class DeepSeekASRProvider(BaseProvider):
    manifest = ProviderManifest(
        id="deepseek",
        name="DeepSeek ASR",
        version="1.0.0",
        description="DeepSeek 语音识别工具 API（WAV / Ogg Opus，远程，按音频时长计费）",
        capabilities=("audio",),
        privacy="remote",
        requires_api_key=True,
        can_transcribe=True,
        homepage="https://api-docs.deepseek.com/zh-cn/guides/asr",
    )

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(config)
        self.base_url = str(self.config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.api_key_env = str(self.config.get("api_key_env") or "DEEPSEEK_API_KEY")
        self.timeout_seconds = float(self.config.get("timeout_seconds") or 60.0)
        self.max_bytes = int(self.config.get("max_bytes") or MAX_AUDIO_BYTES)
        self.max_seconds = float(self.config.get("max_seconds") or MAX_AUDIO_SECONDS)
        self._transport = transport

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/tools/asr"

    def _api_key(self) -> str | None:
        value = os.getenv(self.api_key_env, "").strip()
        return value or None

    def availability(self) -> tuple[bool, str | None]:
        if not bool(self.config.get("enabled", True)):
            return False, "DeepSeek ASR 已在配置中禁用。"
        if not self._api_key():
            return False, f"缺少 {self.api_key_env}。"
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "DeepSeek ASR base_url 不是合法的 http(s) 地址。"
        return True, None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        available, reason = self.availability()
        if not available:
            raise ProviderError(
                reason or "DeepSeek ASR 未就绪。",
                provider_id=self.manifest.id,
                retryable=False,
            )

        raw = await asyncio.to_thread(request.path.read_bytes)
        wav = await asyncio.to_thread(ensure_wav_bytes, raw, request.mime)
        if wav is None:
            raise ProviderError(
                "DeepSeek ASR 需要可解码的音频；当前文件无法转换为 16-bit PCM WAV。",
                provider_id=self.manifest.id,
                retryable=False,
            )
        if len(wav) > self.max_bytes:
            raise ProviderError(
                f"音频超过 DeepSeek ASR 的 {self.max_bytes // (1024 * 1024)} MiB 上限。",
                provider_id=self.manifest.id,
                retryable=False,
            )
        duration = await asyncio.to_thread(_wav_duration, wav)
        if duration is not None and duration > self.max_seconds:
            raise ProviderError(
                f"音频时长 {duration:.1f}s 超过 DeepSeek ASR 的 {self.max_seconds:.0f}s 上限。",
                provider_id=self.manifest.id,
                retryable=False,
            )

        headers = {"Authorization": f"Bearer {self._api_key()}"}
        timeout = min(request.timeout_seconds, self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    files={"file": ("audio.wav", wav, "audio/wav")},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "DeepSeek ASR 请求超时。",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "无法连接 DeepSeek ASR。",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise ProviderError(
                f"DeepSeek ASR 返回 HTTP {response.status_code}。",
                provider_id=self.manifest.id,
                retryable=retryable,
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderError(
                "DeepSeek ASR 返回了非 JSON 响应。",
                provider_id=self.manifest.id,
                retryable=True,
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ProviderError(
                "DeepSeek ASR 响应缺少字符串字段 text。",
                provider_id=self.manifest.id,
                retryable=True,
            )
        return TranscriptionResult(
            text=text.strip(),
            provider_id=self.manifest.id,
            model="deepseek-asr",
            language=request.language,
        )


def _wav_duration(wav_bytes: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            header_frames = handle.getnframes()
    except (wave.Error, EOFError):
        return None
    if rate <= 0 or channels <= 0 or sample_width <= 0:
        return None

    # DeepSeek TTS returns a streaming WAV whose header nframes can be
    # 0x7FFFFFFF even when the body is only a few seconds. Trust the actual
    # data chunk length instead of the header count.
    data_size = _wav_data_size(wav_bytes)
    frame_size = channels * sample_width
    actual_frames = data_size // frame_size if data_size else 0
    frames = actual_frames or header_frames
    if frames <= 0:
        return None
    return frames / float(rate)


def _wav_data_size(wav_bytes: bytes) -> int:
    """Return the byte length of the WAV ``data`` chunk, tolerant of streaming headers."""

    if len(wav_bytes) < 12 or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        return 0
    offset = 12
    while offset + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[offset : offset + 4]
        declared = int.from_bytes(wav_bytes[offset + 4 : offset + 8], "little")
        body = offset + 8
        available = max(0, len(wav_bytes) - body)
        if chunk_id == b"data":
            if declared in {0, 0xFFFFFFFF} or declared > available:
                return available
            return declared
        offset = body + declared + (declared & 1)
    return 0


def provider_factory(config: Mapping[str, Any]) -> DeepSeekASRProvider:
    return DeepSeekASRProvider(config)
