"""Provider registry, adapters and wire-protocol tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from dounai_input.providers.base import ProviderManifest, TranscriptionRequest
from dounai_input.providers.custom_http import CustomHttpProvider
from dounai_input.providers.local_whisper import LocalWhisperProvider
from dounai_input.providers.offline import OfflineProvider
from dounai_input.providers.openai_compatible import build_provider
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


def test_dashscope_chat_audio_request_and_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    captured: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": [{"text": " 你好，豆奶 "}]}}
                ]
            },
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = make_settings(tmp_path)
    provider = build_provider(
        "dashscope", settings.provider_config("dashscope"), models_dir=tmp_path / "models"
    )
    result = __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert result.text == "你好，豆奶"
    assert result.provider_id == "dashscope"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key"
    assert captured["json"]["model"] == "qwen3-asr-flash"
    assert captured["json"]["asr_options"]["language"] == "zh"
    assert captured["json"]["messages"][0]["content"][0]["type"] == "input_audio"
    assert captured["json"]["messages"][0]["content"][0]["input_audio"]["data"].startswith(
        "data:audio/wav;base64,"
    )


def test_openai_transcription_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "hello"})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = make_settings(tmp_path)
    provider = build_provider(
        "openai", settings.provider_config("openai"), models_dir=tmp_path / "models"
    )
    result = __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert result.text == "hello"
    assert captured["url"].endswith("/audio/transcriptions")
    assert b'name="model"' in captured["body"]
    assert b"gpt-4o-transcribe" in captured["body"]
    assert b'name="prompt"' in captured["body"]


def test_provider_error_does_not_echo_response_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(
                401, json={"error": {"message": "sk-secret in vendor body"}}
            )
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = make_settings(tmp_path)
    provider = build_provider(
        "dashscope", settings.provider_config("dashscope"), models_dir=tmp_path / "models"
    )
    with pytest.raises(Exception) as exc_info:
        __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert "sk-secret" not in str(exc_info.value)
    assert "DASHSCOPE_API_KEY" in str(exc_info.value)


def test_custom_http_provider_auth_and_field_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_TEST_TOKEN", "endpoint-secret")
    real_client = httpx.AsyncClient
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"transcript": "custom text"}})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    provider = CustomHttpProvider(
        {
            "endpoint": "http://127.0.0.1:9999/asr",
            "token_env": "CUSTOM_TEST_TOKEN",
            "text_field": "data.transcript",
        }
    )
    result = __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert result.text == "custom text"
    assert captured["auth"] == "Bearer endpoint-secret"
    assert provider.manifest.privacy == "remote"


def test_registry_rejects_duplicate_provider_ids() -> None:
    registry = ProviderRegistry()
    assert registry.register(StubProvider(provider_id="one")) is True
    assert registry.register(StubProvider(provider_id="one")) is False
    assert registry.ids() == ["one"]
    assert registry.errors


def test_broken_provider_info_is_isolated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.database_path, settings.media_dir)
    from dounai_input.providers.manager import ProviderManager
    from tests.helpers import ExplodingInfoProvider

    registry = ProviderRegistry()
    registry.register(ExplodingInfoProvider(provider_id="broken"))
    manager = ProviderManager(settings, store, registry=registry)
    manager._active_id = "broken"
    rows = manager.list_providers()
    assert rows[0]["available"] is False
    assert "status check failed" in rows[0]["unavailable_reason"]


def test_malformed_vendor_response_is_a_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"unexpected": True})
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = make_settings(tmp_path)
    provider = build_provider(
        "dashscope", settings.provider_config("dashscope"), models_dir=tmp_path / "models"
    )
    with pytest.raises(Exception) as exc_info:
        __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert "unexpected" in str(exc_info.value).lower()


def test_non_json_vendor_response_is_a_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>gateway</html>")
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    settings = make_settings(tmp_path)
    provider = build_provider(
        "dashscope", settings.provider_config("dashscope"), models_dir=tmp_path / "models"
    )
    with pytest.raises(Exception) as exc_info:
        __import__("asyncio").run(provider.transcribe(_request(tmp_path)))
    assert "non-JSON" in str(exc_info.value)


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
    from dounai_input.store import Store

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


def test_provider_info_contains_no_environment_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-secret-value")
    settings = make_settings(tmp_path)
    provider = build_provider(
        "dashscope", settings.provider_config("dashscope"), models_dir=tmp_path / "models"
    )
    info = provider.info()
    assert "sk-secret-value" not in repr(info)
    assert info["model"] == "qwen3-asr-flash"
