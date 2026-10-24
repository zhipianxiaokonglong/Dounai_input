"""Shared fixtures for deterministic, isolated tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dounai_input.api import build_app
from dounai_input.config import Settings, load_settings
from dounai_input.services import Services, create_services


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    config_path = tmp_path / "test-empty.toml"
    config_path.write_text("", encoding="utf-8")
    settings = load_settings(config_path)
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "public_base_url": "http://testserver",
        "api_token": None,
        "default_active_provider": "offline",
        "fallback_providers": ("local-whisper", "dashscope"),
        "allow_remote_fallback": False,
        "voiceprint_enabled": False,
        "voiceprint_threshold": 0.82,
        "wake_phrase": "你好豆奶",
        "wake_phrase_required": True,
        "media_retention_hours": 72,
        "provider_failure_cooldown_seconds": 30,
    }
    values.update(overrides)
    settings = replace(settings, **values)
    settings.ensure_directories()
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def services(settings: Settings) -> Services:
    return create_services(settings)


@pytest.fixture
def app(settings: Settings):
    application = build_app(settings)
    yield application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
