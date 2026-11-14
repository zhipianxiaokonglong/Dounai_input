from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from dounai_input.errors import ProviderError
from dounai_input.providers.base import TranscriptionRequest
from dounai_input.providers.deepseek import DeepSeekASRProvider
from tests.helpers import synthetic_voice


def _request(tmp_path: Path) -> TranscriptionRequest:
    path = tmp_path / "voice.wav"
    path.write_bytes(synthetic_voice(seed=4))
    return TranscriptionRequest(path=path, mime="audio/wav", language="zh")


def test_deepseek_asr_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    provider = DeepSeekASRProvider({})
    available, reason = provider.availability()
    assert available is False
    assert "DEEPSEEK_API_KEY" in (reason or "")


def test_deepseek_asr_uploads_wav_and_reads_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"request_id": "r1", "text": "你好，豆奶。", "duration_ms": 1200},
        )

    provider = DeepSeekASRProvider(
        {"base_url": "https://api.deepseek.com"},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.transcribe(_request(tmp_path)))

    assert result.text == "你好，豆奶。"
    assert result.provider_id == "deepseek"
    assert result.model == "deepseek-asr"
    assert seen["url"] == "https://api.deepseek.com/v1/tools/asr"
    assert seen["authorization"] == "Bearer sk-test"
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'name="file"' in body
    assert b"audio.wav" in body


def test_deepseek_asr_accepts_streaming_wav_header(tmp_path: Path) -> None:
    """TTS vendors often emit WAV with an unknown/0x7FFFFFFF nframes header."""

    from dounai_input.providers.deepseek import _wav_duration

    data = bytearray(synthetic_voice(seed=5))
    # RIFF data-chunk size field lives at offset 40 for canonical PCM WAV.
    data[40:44] = (0x7FFFFFFF).to_bytes(4, "little")
    duration = _wav_duration(bytes(data))
    assert duration is not None
    assert 0 < duration < 10


def test_deepseek_asr_rate_limit_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "busy"}})

    provider = DeepSeekASRProvider({}, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider.transcribe(_request(tmp_path)))
    assert raised.value.retryable is True
