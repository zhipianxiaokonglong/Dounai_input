"""Test doubles and generated media helpers.

These are test-only stubs.  They are not part of the installed package and are
never used by the server at runtime.
"""

from __future__ import annotations

import asyncio
import io
import struct
import wave
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from dounai_input.errors import ProviderError
from dounai_input.providers.base import (
    BaseProvider,
    ProviderManifest,
    TranscriptionRequest,
    TranscriptionResult,
)

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00"
    b"\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def pcm_wav(samples: np.ndarray, sample_rate: int = 16_000) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def synthetic_voice(
    *,
    f0: float = 180.0,
    seconds: float = 2.5,
    sample_rate: int = 16_000,
    noise: float = 0.006,
    seed: int = 7,
) -> bytes:
    """Create deterministic speech-like harmonic audio for speaker tests."""
    rng = np.random.default_rng(seed)
    time = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    envelope = 0.65 + 0.35 * np.sin(2 * np.pi * 2.3 * time)
    signal = np.zeros_like(time)
    for harmonic, amplitude in enumerate([1.0, 0.65, 0.38, 0.2, 0.11], start=1):
        signal += amplitude * np.sin(2 * np.pi * f0 * harmonic * time + harmonic * 0.17)
    signal *= envelope
    signal = signal / np.max(np.abs(signal)) * 0.42
    signal += rng.normal(0.0, noise, size=time.size).astype(np.float32)
    return pcm_wav(signal.astype(np.float32), sample_rate)


def silence_wav(seconds: float = 2.0, sample_rate: int = 16_000) -> bytes:
    return pcm_wav(np.zeros(int(seconds * sample_rate), dtype=np.float32), sample_rate)


def wav_header_only() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"")
    return buffer.getvalue()


def make_png(width: int = 1, height: int = 1, padding: int = 0) -> bytes:
    """A tiny structurally plausible PNG; media validation checks magic bytes."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return header + ihdr + (b"\x00" * padding)


@dataclass
class StubProvider(BaseProvider):
    provider_id: str = "stub"
    text: str = "stub transcript"
    delay: float = 0.0
    fail: str | None = None
    available: bool = True
    available_reason: str | None = None
    privacy: str = "local"
    capabilities: tuple[str, ...] = ("audio",)
    can_transcribe: bool = True

    def __post_init__(self) -> None:
        self.manifest = ProviderManifest(
            id=self.provider_id,
            name=f"Stub {self.provider_id}",
            version="1.0.0",
            description="Test-only provider",
            capabilities=self.capabilities,
            privacy=self.privacy,
            can_transcribe=self.can_transcribe,
        )
        self.requests: list[TranscriptionRequest] = []

    def availability(self) -> tuple[bool, str | None]:
        return self.available, self.available_reason

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderError(self.fail, provider_id=self.provider_id, retryable=True)
        return TranscriptionResult(
            text=self.text,
            provider_id=self.provider_id,
            model="stub-model",
            language=request.language,
        )


class ExplodingInfoProvider(StubProvider):
    def info(self):  # type: ignore[override]
        raise RuntimeError("info exploded")


def register_all(registry, providers: Sequence[BaseProvider]) -> None:
    for provider in providers:
        registry.register(provider)
