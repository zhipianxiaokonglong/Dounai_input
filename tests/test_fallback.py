"""Failure isolation, capability-aware fallback and context continuity."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile

from dounai_input.api import build_app
from dounai_input.ingest import IngestService
from dounai_input.providers.base import TranscriptionRequest
from dounai_input.providers.manager import ProviderManager
from dounai_input.providers.registry import ProviderRegistry
from dounai_input.services import create_services
from dounai_input.store import Store
from tests.conftest import make_settings
from tests.helpers import StubProvider, synthetic_voice


def _manager(tmp_path: Path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    primary = StubProvider(provider_id="primary", text="primary text")
    secondary = StubProvider(provider_id="secondary", text="secondary text")
    registry.register(primary)
    registry.register(secondary)
    manager = ProviderManager(settings, store, registry=registry)
    return settings, store, registry, manager, primary, secondary


def _request(tmp_path: Path, timeout: float = 1.0) -> TranscriptionRequest:
    audio = tmp_path / "input.wav"
    audio.write_bytes(synthetic_voice(seconds=1.5))
    return TranscriptionRequest(path=audio, mime="audio/wav", timeout_seconds=timeout)


def test_primary_failure_uses_fallback_and_marks_degraded(tmp_path: Path) -> None:
    _, _, _, manager, primary, _ = _manager(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
    )
    primary.fail = "vendor unavailable"
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == "secondary text"
    assert outcome.provider_id == "secondary"
    assert outcome.degraded is True
    assert [attempt.outcome for attempt in outcome.attempts] == ["failed", "success"]


def test_primary_timeout_uses_fallback(tmp_path: Path) -> None:
    _, _, _, manager, primary, _ = _manager(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
    )
    primary.delay = 1.0
    outcome = asyncio.run(manager.transcribe(_request(tmp_path, timeout=0.05)))
    assert outcome.text == "secondary text"
    assert outcome.attempts[0].outcome == "timeout"
    assert outcome.degraded is True


def test_all_providers_failing_still_returns_an_outcome(tmp_path: Path) -> None:
    _, _, _, manager, primary, secondary = _manager(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
    )
    primary.fail = "down"
    secondary.fail = "also down"
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == ""
    assert outcome.provider_id is None
    assert outcome.degraded is True
    assert [attempt.outcome for attempt in outcome.attempts] == ["failed", "failed"]


def test_locality_guard_blocks_remote_fallback(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("cloud",),
        allow_remote_fallback=False,
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    primary = StubProvider(provider_id="primary", fail="local model failed")
    cloud = StubProvider(provider_id="cloud", privacy="remote", text="cloud text")
    registry.register(primary)
    registry.register(cloud)
    manager = ProviderManager(settings, store, registry=registry)
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == ""
    assert outcome.degraded is True
    cloud_attempt = next(item for item in outcome.attempts if item.provider_id == "cloud")
    assert cloud_attempt.outcome == "skipped"
    assert "privacy guard" in (cloud_attempt.detail or "")


def test_remote_primary_may_fall_back_to_local(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="cloud",
        fallback_providers=("local",),
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    cloud = StubProvider(provider_id="cloud", privacy="remote", fail="cloud down")
    local = StubProvider(provider_id="local", privacy="local", text="local text")
    registry.register(cloud)
    registry.register(local)
    manager = ProviderManager(settings, store, registry=registry)
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == "local text"
    assert outcome.degraded is True


def test_remote_to_remote_fallback_requires_explicit_opt_in(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="cloud-a",
        fallback_providers=("cloud-b",),
        allow_remote_fallback=False,
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    registry.register(StubProvider(provider_id="cloud-a", privacy="remote", fail="down"))
    registry.register(StubProvider(provider_id="cloud-b", privacy="remote", text="other cloud"))
    manager = ProviderManager(settings, store, registry=registry)
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == ""
    cloud_b = next(item for item in outcome.attempts if item.provider_id == "cloud-b")
    assert cloud_b.outcome == "skipped"

    permissive = make_settings(
        tmp_path,
        default_active_provider="cloud-a",
        fallback_providers=("cloud-b",),
        allow_remote_fallback=True,
    )
    manager2 = ProviderManager(permissive, store, registry=registry)
    manager2.set_active("cloud-a")
    outcome2 = asyncio.run(manager2.transcribe(_request(tmp_path)))
    assert outcome2.text == "other cloud"
    assert outcome2.degraded is True


def test_circuit_breaker_skips_recently_failed_provider(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
        provider_failure_cooldown_seconds=60,
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    primary = StubProvider(provider_id="primary", fail="down")
    secondary = StubProvider(provider_id="secondary", text="fallback")
    registry.register(primary)
    registry.register(secondary)
    manager = ProviderManager(settings, store, registry=registry)

    first = asyncio.run(manager.transcribe(_request(tmp_path)))
    second = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert first.provider_id == "secondary"
    assert second.provider_id == "secondary"
    assert second.attempts[0].outcome == "skipped"
    assert "paused" in (second.attempts[0].detail or "")
    assert second.degraded is True


def test_successful_provider_with_empty_text_is_empty_not_unavailable(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="active",
        fallback_providers=(),
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    registry.register(StubProvider(provider_id="active", text=""))
    manager = ProviderManager(settings, store, registry=registry)
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == ""
    assert outcome.provider_id == "active"
    assert outcome.degraded is False


def test_active_offline_can_use_local_transcriber_without_degradation(
    tmp_path: Path,
) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="offline",
        fallback_providers=("local",),
    )
    store = Store(settings.database_path, settings.media_dir)
    registry = ProviderRegistry()
    registry.register(
        StubProvider(
            provider_id="offline", can_transcribe=False, capabilities=("text", "image", "audio")
        )
    )
    registry.register(StubProvider(provider_id="local", text="local transcript"))
    manager = ProviderManager(settings, store, registry=registry)
    outcome = asyncio.run(manager.transcribe(_request(tmp_path)))
    assert outcome.text == "local transcript"
    assert outcome.provider_id == "local"
    assert outcome.degraded is False


def test_context_survives_model_switch(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
    )
    services = create_services(settings)
    registry = ProviderRegistry()
    primary = StubProvider(provider_id="primary", text="first transcript")
    secondary = StubProvider(provider_id="secondary", text="second transcript")
    registry.register(primary)
    registry.register(secondary)
    manager = ProviderManager(settings, services.store, registry=registry)
    manager.set_active("primary")
    ingest = IngestService(
        settings,
        services.store,
        services.media,
        manager,
        services.voiceprint,
    )

    asyncio.run(
        ingest.ingest_text(text="我昨天梦见一片海", session_id="sess_context", source="test")
    )
    manager.set_active("secondary")
    upload = UploadFile(
        file=io.BytesIO(synthetic_voice()),
        filename="voice.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )
    result = asyncio.run(
        ingest.ingest_audio(
            upload=upload,
            session_id="sess_context",
            source="test",
            language="zh",
        )
    )
    assert result.event.metadata.asr_text == "second transcript"
    assert len(secondary.requests) == 1
    assert "我昨天梦见一片海" in secondary.requests[0].context


def test_api_reports_degradation_in_headers_not_event(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="primary",
        fallback_providers=("secondary",),
    )
    app = build_app(settings)
    manager = app.state.services["providers"]
    registry = ProviderRegistry()
    registry.register(StubProvider(provider_id="primary", fail="down"))
    registry.register(StubProvider(provider_id="secondary", text="fallback transcript"))
    manager.registry = registry
    manager.set_active("primary")
    with TestClient(app) as client:
        response = client.post(
            "/v1/inputs/audio",
            files={"file": ("voice.wav", synthetic_voice(), "audio/wav")},
        )
    assert response.status_code == 201
    payload = response.json()
    assert payload["metadata"]["asr_text"] == "fallback transcript"
    assert payload["metadata"]["asr_status"] == "completed"
    assert set(payload["metadata"]) == {
        "asr_text",
        "asr_status",
        "voiceprint_event",
        "model_capabilities",
    }
    assert response.headers["X-Dounai-Degraded"] == "true"
    assert response.headers["X-Dounai-ASR-Status"] == "completed"
    assert response.headers["X-Dounai-Provider"] == "secondary"


def test_api_marks_empty_transcript_separately_from_unavailable(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        default_active_provider="silent",
        fallback_providers=(),
    )
    app = build_app(settings)
    manager = app.state.services["providers"]
    registry = ProviderRegistry()
    registry.register(StubProvider(provider_id="silent", text=""))
    manager.registry = registry
    manager.set_active("silent")
    with TestClient(app) as client:
        response = client.post(
            "/v1/inputs/audio",
            files={"file": ("voice.wav", synthetic_voice(), "audio/wav")},
        )
    assert response.status_code == 201
    assert response.json()["metadata"]["asr_status"] == "empty"
    assert response.json()["metadata"]["asr_text"] == ""
    # Empty is a successful capability run, not a provider failure.
    assert response.headers["X-Dounai-Degraded"] == "false"
