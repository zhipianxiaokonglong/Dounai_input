"""Provider registry, adapters and wire-protocol tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from dounai_input.providers.base import ProviderManifest, TranscriptionRequest
from dounai_input.providers.local_whisper import LocalWhisperProvider
from dounai_input.providers.offline import OfflineProvider
from dounai_input.providers.openai_compatible import OpenAICompatibleProvider
from dounai_input.providers.registry import ProviderRegistry
from dounai_input.store import Store
from tests.conftest import make_settings
from tests.helpers import StubProvider


def _request(tmp_path: Path) -> TranscriptionRequest:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 64)
    return TranscriptionRequest(
        path=path,
        mime="audio/wav",
        language="zh",
        context=("上一句",),
        timeout_seconds=5,
    )


def test_manifest_capabilities_are_ordered_and_validated() -> None:
    manifest = ProviderManifest(
        id="x",
        name="X",
        version="1",
        description="",
        capabilities=("audio", "text", "audio"),
        privacy="local",
    )
    assert manifest.capabilities == ("text", "audio")
    with pytest.raises(ValueError):
        ProviderManifest(
            id="x",
            name="X",
            version="1",
            description="",
            capabilities=("video",),
            privacy="local",
        )
    with pytest.raises(ValueError):
        ProviderManifest(
            id="x",
            name="X",
            version="1",
            description="",
            capabilities=("text",),
            privacy="unknown",
        )


def test_offline_provider_is_always_available_and_does_not_pretend_to_transcribe() -> None:
    provider = OfflineProvider({})
    assert provider.availability() == (True, None)
    assert provider.manifest.can_transcribe is False
    assert provider.manifest.capabilities == ("text", "image", "audio")


def test_local_whisper_reports_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    provider = LocalWhisperProvider({}, models_dir=Path("nonexistent"))
    available, reason = provider.availability()
    assert available is False
    assert "local-asr" in reason


def test_local_whisper_validates_model_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    models_dir = tmp_path / "models"
    model_dir = models_dir / "whisper-small"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    provider = LocalWhisperProvider({"model": "small"}, models_dir=models_dir)
    assert provider.availability() == (True, None)

    (model_dir / "model.bin").unlink()
    available, reason = provider.availability()
    assert available is False
    assert "incomplete" in reason

    bad = LocalWhisperProvider({"model": "not-a-catalog-model"}, models_dir=models_dir)
    available, reason = bad.availability()
    assert available is False
    assert "Unknown" in reason


def test_generic_openai_transcription_protocol(tmp_path: Path) -> None:
    captured: dict = {}
    transport = httpx.MockTransport(
        lambda request: (
            captured.update(
                url=str(request.url),
                auth=request.headers.get("authorization"),
                body=request.content,
            )
            or httpx.Response(200, json={"text": "hello"})
        )
    )
    provider = OpenAICompatibleProvider(
        provider_id="api-test-transcribe",
        name="Test transcribe",
        base_url="https://api.example.test/v1",
        model="gpt-4o-transcribe",
        api_key="test-key",
        protocol="audio_transcriptions",
        capabilities=("audio",),
    )

    async def run():
        # The provider owns its client, so patch the constructor for this one call.
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        import dounai_input.providers.openai_compatible as module

        module.httpx.AsyncClient = factory  # type: ignore[assignment]
        try:
            return await provider.transcribe(_request(tmp_path))
        finally:
            module.httpx.AsyncClient = original  # type: ignore[assignment]

    result = __import__("asyncio").run(run())
    assert result.text == "hello"
    assert result.provider_id == "api-test-transcribe"
    assert captured["url"].endswith("/audio/transcriptions")
    assert captured["auth"] == "Bearer test-key"
    assert b'name="model"' in captured["body"]
    assert b"gpt-4o-transcribe" in captured["body"]
    assert b'name="prompt"' in captured["body"]


def test_generic_chat_audio_protocol(tmp_path: Path) -> None:
    captured: dict = {}
    transport = httpx.MockTransport(
        lambda request: (
            captured.update(
                url=str(request.url),
                auth=request.headers.get("authorization"),
                json=json.loads(request.content),
            )
            or httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": [{"text": " 你好 "}]}}]
                },
            )
        )
    )
    provider = OpenAICompatibleProvider(
        provider_id="api-test-chat-audio",
        name="Test chat audio",
        base_url="https://api.example.test/v1",
        model="qwen3-asr-flash",
        api_key="test-key",
        protocol="chat_audio",
        capabilities=("audio",),
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    import dounai_input.providers.openai_compatible as module

    module.httpx.AsyncClient = factory  # type: ignore[assignment]
    try:
        result = __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    finally:
        module.httpx.AsyncClient = original  # type: ignore[assignment]
    assert result.text == "你好"
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["model"] == "qwen3-asr-flash"
    assert captured["json"]["asr_options"]["language"] == "zh"
    audio = captured["json"]["messages"][0]["content"][0]["input_audio"]
    assert audio["data"].startswith("data:audio/wav;base64,")
    assert audio["format"] == "wav"


def test_provider_error_does_not_echo_response_body(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "sk-secret in vendor body"}})
    )
    provider = OpenAICompatibleProvider(
        provider_id="api-test-auth",
        name="Auth fail",
        base_url="https://api.example.test/v1",
        model="gpt-4o-transcribe",
        api_key="test-key",
        protocol="audio_transcriptions",
        capabilities=("audio",),
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    import dounai_input.providers.openai_compatible as module

    module.httpx.AsyncClient = factory  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as exc_info:
            __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    finally:
        module.httpx.AsyncClient = original  # type: ignore[assignment]
    assert "sk-secret" not in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)


def test_local_whisper_consumes_segment_generator_in_worker(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace

    from dounai_input.providers.local_whisper import LocalWhisperProvider

    class FakeModel:
        def transcribe(self, *args, **kwargs):
            return iter(
                [
                    SimpleNamespace(text="你好"),
                    SimpleNamespace(text="，豆奶。"),
                ]
            ), SimpleNamespace(language="zh")

    provider = LocalWhisperProvider({"model": "small"}, models_dir=Path("unused"))

    async def fake_get_model():
        return FakeModel()

    provider._get_model = fake_get_model  # type: ignore[method-assign]
    result = asyncio.run(provider.transcribe(_request(tmp_path)))
    assert result.text == "你好，豆奶。"


def test_manager_status_survives_availability_exception(tmp_path: Path) -> None:
    from dounai_input.providers.manager import ProviderManager

    class BrokenAvailability(StubProvider):
        def availability(self):
            raise RuntimeError("boom")

    settings = make_settings(tmp_path, default_active_provider="broken")
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    registry.register(BrokenAvailability(provider_id="broken"))
    manager = ProviderManager(settings, store, registry=registry)
    assert manager.status()["active_available"] is False
    assert manager.list_providers()[0]["available"] is False


def test_provider_info_contains_no_api_key() -> None:
    provider = OpenAICompatibleProvider(
        provider_id="api-test-info",
        name="Info",
        base_url="https://api.example.test/v1",
        model="gpt-4o-transcribe",
        api_key="sk-secret-value",
        protocol="audio_transcriptions",
        capabilities=("audio",),
    )
    info = provider.info()
    assert "sk-secret-value" not in repr(info)
    assert info["model"] == "gpt-4o-transcribe"
