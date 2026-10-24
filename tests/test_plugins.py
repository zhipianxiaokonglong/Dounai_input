"""Local plugin discovery and local model catalog behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dounai_input.model_catalog import (
    MODEL_STATE_FILE,
    ModelInstallError,
    catalog_entries,
    get_model,
    install_model,
    is_installed,
    model_directory,
    remove_model,
    verify_model,
)
from dounai_input.providers.registry import (
    ProviderRegistry,
    build_registry,
    load_entry_point_plugins,
)
from tests.conftest import make_settings
from tests.helpers import StubProvider

PLUGIN_SOURCE = """
from dounai_input.providers.base import BaseProvider, ProviderManifest, TranscriptionResult


class EchoPlugin(BaseProvider):
    manifest = ProviderManifest(
        id="echo-plugin",
        name="Echo plugin",
        version="1.0.0",
        description="A plugin used to verify local discovery.",
        capabilities=("audio",),
        privacy="local",
        can_transcribe=True,
    )

    def availability(self):
        return True, None

    async def transcribe(self, request):
        return TranscriptionResult(text="plugin transcript", provider_id="echo-plugin")


PROVIDER = EchoPlugin
"""


def test_directory_plugin_is_discovered_and_constructible(tmp_path) -> None:
    settings = make_settings(tmp_path)
    (settings.plugins_dir / "echo_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    registry = build_registry(settings)
    provider = registry.get("echo-plugin")
    assert provider is not None
    assert provider.availability() == (True, None)


def test_disabled_directory_plugin_is_skipped(tmp_path) -> None:
    settings = make_settings(tmp_path)
    (settings.plugins_dir / "echo_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    provider_configs = {
        **settings.provider_configs,
        "echo_plugin": {"enabled": False},
    }
    settings = replace(settings, provider_configs=provider_configs)
    registry = build_registry(settings)
    assert registry.get("echo-plugin") is None


def test_invalid_plugin_is_reported_not_fatal(tmp_path) -> None:
    settings = make_settings(tmp_path)
    (settings.plugins_dir / "broken.py").write_text(
        "raise RuntimeError('bad plugin')\n", encoding="utf-8"
    )
    registry = build_registry(settings)
    assert any("broken.py" in error for error in registry.errors)
    # Built-ins still load.
    assert registry.get("offline") is not None


def test_entry_point_plugin_loads(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    registry = ProviderRegistry()

    class EntryPlugin(StubProvider):
        pass

    class FakeEntryPoint:
        name = "entry-plugin"

        def load(self):
            class Plugin(EntryPlugin):
                def __init__(self, config):
                    super().__init__(provider_id="entry-plugin", privacy="local", text="entry")

            return Plugin

    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **kwargs: [FakeEntryPoint()] if kwargs.get("group") else [],
    )
    load_entry_point_plugins(settings, registry)
    assert registry.get("entry-plugin") is not None


def test_provider_collision_is_reported(tmp_path) -> None:
    registry = ProviderRegistry()
    assert registry.register(StubProvider(provider_id="same")) is True
    assert registry.register(StubProvider(provider_id="same")) is False
    assert "collision" in registry.errors[0]


def test_model_catalog_entries_are_not_installed_by_default(tmp_path) -> None:
    settings = make_settings(tmp_path)
    entries = catalog_entries(settings.models_dir)
    assert len(entries) >= 5
    assert all(entry["installed"] is False for entry in entries)
    assert any(entry["recommended"] for entry in entries)


def test_installed_model_is_detected_and_verified(tmp_path) -> None:
    settings = make_settings(tmp_path)
    spec = get_model("small")
    directory = model_directory(settings.models_dir, spec.id)
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(b"weights")
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / MODEL_STATE_FILE).write_text(
        json.dumps(
            {
                "model_id": spec.id,
                "repo_id": spec.repo_id,
                "revision": "abc123",
                "weights_file": "model.bin",
                "weights_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }
        ),
        encoding="utf-8",
    )
    # The checksum above is deliberately for empty bytes, so verification fails.
    assert is_installed(settings.models_dir, spec.id) is True
    with pytest.raises(ModelInstallError):
        verify_model(settings.models_dir, spec.id)

    import hashlib

    digest = hashlib.sha256(b"weights").hexdigest()
    state = json.loads((directory / MODEL_STATE_FILE).read_text(encoding="utf-8"))
    state["weights_sha256"] = digest
    (directory / MODEL_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert verify_model(settings.models_dir, spec.id)["verified"] is True


def test_remove_model_refuses_unmanaged_directory(tmp_path) -> None:
    settings = make_settings(tmp_path)
    target = model_directory(settings.models_dir, "tiny")
    target.mkdir(parents=True)
    (target / "model.bin").write_bytes(b"weights")
    with pytest.raises(ModelInstallError):
        remove_model(settings.models_dir, "tiny")


def test_remove_model_removes_managed_directory(tmp_path) -> None:
    settings = make_settings(tmp_path)
    target = model_directory(settings.models_dir, "tiny")
    target.mkdir(parents=True)
    (target / MODEL_STATE_FILE).write_text(json.dumps({"model_id": "tiny"}), encoding="utf-8")
    assert remove_model(settings.models_dir, "tiny") is True
    assert target.exists() is False
    assert remove_model(settings.models_dir, "tiny") is False


def test_install_refuses_without_optional_dependencies(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ModelInstallError):
        install_model(tmp_path / "models", "small")


def test_install_wraps_tls_failure_with_actionable_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util
    import sys
    import types

    fake = types.ModuleType("huggingface_hub")

    class HfApi:
        def model_info(self, repo_id):  # pragma: no cover - revision is supplied
            raise AssertionError("revision should avoid the API call")

    def snapshot_download(**kwargs):
        raise RuntimeError("CERTIFICATE_VERIFY_FAILED")

    fake.HfApi = HfApi
    fake.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    with pytest.raises(ModelInstallError) as exc_info:
        install_model(tmp_path / "models", "tiny", revision="abc")
    assert "TLS certificate" in str(exc_info.value)
    assert not (tmp_path / "models" / "whisper-tiny").exists()


def test_unknown_model_id_is_rejected(tmp_path) -> None:
    with pytest.raises(ModelInstallError):
        get_model("unknown-model")
    with pytest.raises(ModelInstallError):
        model_directory(Path(tmp_path), "unknown-model")
