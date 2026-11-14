"""Configuration parsing and safe defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from dounai_input.config import ConfigurationError, load_settings


def test_safe_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOUNAI_API_TOKEN", raising=False)
    config = tmp_path / "empty.toml"
    config.write_text("", encoding="utf-8")
    settings = load_settings(config)
    assert settings.host == "127.0.0.1"
    assert settings.api_token is None
    assert settings.voiceprint_enabled is False
    assert settings.allow_remote_fallback is False
    assert settings.provider_config("local-whisper")["model"] == "small"
    assert "dashscope" not in settings.provider_configs


def test_non_loopback_bind_requires_token(tmp_path: Path) -> None:
    settings = load_settings(_empty(tmp_path))
    from dataclasses import replace

    with pytest.raises(ConfigurationError):
        replace(settings, host="0.0.0.0").validate_bind()
    with pytest.raises(ConfigurationError):
        replace(settings, host="gateway.example.com").validate_bind()
    with pytest.raises(ConfigurationError):
        replace(settings, host="127.0.0.1.nip.io").validate_bind()
    replace(settings, host="0.0.0.0", allow_insecure_bind=True).validate_bind()
    replace(settings, host="0.0.0.0", api_token="token").validate_bind()
    replace(settings, host="[::1]").validate_bind()
    replace(settings, host="localhost").validate_bind()


def test_toml_overrides_provider_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOUNAI_VOICEPRINT_THRESHOLD", raising=False)
    config = tmp_path / "dounai.toml"
    config.write_text(
        """
data_dir = "custom-data"
[privacy]
voiceprint_threshold = 0.9
voiceprint_enabled = true
wake_phrase = "小豆"
[models]
active = "offline"
fallback = ["local-whisper"]
[providers.local-whisper]
model = "base"
""",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.data_dir == (Path.cwd() / "custom-data").resolve()
    assert settings.voiceprint_threshold == 0.9
    assert settings.voiceprint_enabled is True
    assert settings.wake_phrase == "小豆"
    assert settings.fallback_providers == ("local-whisper",)
    assert settings.provider_config("local-whisper")["model"] == "base"


def test_environment_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "empty.toml"
    config.write_text("[models]\nactive = 'offline'\n", encoding="utf-8")
    monkeypatch.setenv("DOUNAI_ACTIVE_PROVIDER", "dashscope")
    monkeypatch.setenv("DOUNAI_VOICEPRINT_THRESHOLD", "0.88")
    settings = load_settings(config)
    assert settings.default_active_provider == "dashscope"
    assert settings.voiceprint_threshold == 0.88


def test_invalid_threshold_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[privacy]\nvoiceprint_threshold = 1.5\n", encoding="utf-8")
    monkeypatch.delenv("DOUNAI_VOICEPRINT_THRESHOLD", raising=False)
    with pytest.raises(ConfigurationError):
        load_settings(config)


def test_fallback_must_be_a_list(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text("[models]\nfallback = 'local-whisper'\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(config)


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path / "does-not-exist.toml")


def test_example_config_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A CLI test may have loaded the developer's local .env into os.environ;
    # this test verifies the example file itself, not local overrides.
    monkeypatch.delenv("DOUNAI_ACTIVE_PROVIDER", raising=False)
    example = Path(__file__).resolve().parents[1] / "dounai.example.toml"
    settings = load_settings(example)
    assert settings.default_active_provider == "offline"
    assert settings.provider_config("local-whisper")["model"] == "small"


def test_public_base_url_warnings(tmp_path: Path) -> None:
    from dataclasses import replace

    settings = load_settings(_empty(tmp_path))
    assert settings.public_base_url_warnings() == []
    mismatch = replace(
        settings,
        host="0.0.0.0",
        public_base_url="http://127.0.0.1:8000",
    )
    assert "remote consumers" in mismatch.public_base_url_warnings()[0]
    port_mismatch = replace(
        settings,
        host="127.0.0.1",
        port=8010,
        public_base_url="http://127.0.0.1:8000",
    )
    assert "does not match" in port_mismatch.public_base_url_warnings()[0]
    invalid = replace(settings, public_base_url="not-a-url")
    assert "absolute http(s)" in invalid.public_base_url_warnings()[0]


def test_invalid_public_base_url_is_rejected_on_load(tmp_path: Path) -> None:
    config = tmp_path / "bad-url.toml"
    config.write_text('[server]\npublic_base_url = "ftp://example.com"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(config)


def test_utf8_bom_config_is_accepted(tmp_path: Path) -> None:
    config = tmp_path / "bom.toml"
    config.write_bytes(b"\xef\xbb\xbf[server]\nport = 8124\n")
    settings = load_settings(config)
    assert settings.port == 8124


def test_invalid_utf8_config_is_a_clean_error(tmp_path: Path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_bytes(b"[server]\n\xff\xfe\n")
    with pytest.raises(ConfigurationError):
        load_settings(config)


def _empty(tmp_path: Path) -> Path:
    config = tmp_path / "empty.toml"
    config.write_text("", encoding="utf-8")
    return config
