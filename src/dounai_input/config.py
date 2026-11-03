"""Application configuration.

Configuration is deliberately boring and inspectable: defaults live here, an
optional ``dounai.toml`` overrides them, and a small set of environment variables
override both.  Secrets are *never* read from TOML; providers receive the name of
an environment variable and read the value at call time.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_WAKE_PHRASE = "你好豆奶"
CONSENT_POLICY_VERSION = "1.0"
_DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "offline": {
        "enabled": True,
    },
    "local-whisper": {
        "enabled": True,
        "model": "small",
        "device": "auto",
        "compute_type": "int8",
        "language": "zh",
    },
    "dashscope": {
        "enabled": True,
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "asr_model": "qwen3-asr-flash",
        "omni_model": "qwen3.8-omni-flash",
        "language": "zh",
        "timeout_seconds": 60,
    },
    "openai": {
        "enabled": True,
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "asr_model": "gpt-4o-transcribe",
        "language": "zh",
        "timeout_seconds": 60,
    },
    "custom-http": {
        "enabled": False,
        "endpoint": "",
        "token_env": "DOUNAI_CUSTOM_HTTP_TOKEN",
        "timeout_seconds": 30,
    },
}


class ConfigurationError(ValueError):
    """Raised when configuration is syntactically valid but unsafe or invalid."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    config_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = "http://127.0.0.1:8000"
    api_token: str | None = None
    allow_insecure_bind: bool = False
    plugin_paths: tuple[Path, ...] = ()
    max_text_chars: int = 20_000
    max_image_bytes: int = 15 * 1024 * 1024
    max_audio_bytes: int = 25 * 1024 * 1024
    media_retention_hours: int = 72
    session_context_events: int = 3
    provider_timeout_seconds: float = 60.0
    provider_failure_cooldown_seconds: float = 30.0
    allow_remote_fallback: bool = False
    voiceprint_enabled: bool = False
    wake_phrase: str = DEFAULT_WAKE_PHRASE
    wake_phrase_required: bool = True
    voiceprint_threshold: float = 0.82
    consent_policy_version: str = CONSENT_POLICY_VERSION
    default_active_provider: str = "offline"
    fallback_providers: tuple[str, ...] = ("local-whisper", "dashscope")
    provider_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def provider_config(self, provider_id: str) -> dict[str, Any]:
        """Return a mutable copy so providers cannot mutate shared settings."""
        return dict(self.provider_configs.get(provider_id, {}))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "dounai.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.media_dir, self.models_dir, self.plugins_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _best_effort_private(directory)

    def validate_bind(self) -> None:
        """Refuse to expose an unauthenticated gateway beyond loopback."""
        if _is_loopback(self.host) or self.api_token or self.allow_insecure_bind:
            return
        raise ConfigurationError(
            "Binding to a non-loopback address requires DOUNAI_API_TOKEN. "
            "Set a bearer token, or set DOUNAI_ALLOW_INSECURE_BIND=1 only for an "
            "isolated development network."
        )

    def public_base_url_warnings(self) -> list[str]:
        """Warn when media references cannot match how clients reach the server.

        A reverse proxy may legitimately expose a public hostname while the
        gateway listens on loopback, so that combination is allowed.  The
        unsafe/confusing combinations are a loopback public URL while listening
        publicly, or a loopback URL whose port does not match the listener.
        """
        parsed = urlsplit(self.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ["public_base_url must be an absolute http(s) URL."]
        public_host = parsed.hostname.strip("[]").lower()
        public_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        warnings: list[str] = []
        if _is_loopback(public_host) and not _is_loopback(self.host):
            warnings.append(
                "public_base_url points at loopback while the gateway listens on a "
                "non-loopback address; remote consumers cannot fetch media URLs. "
                "Set DOUNAI_PUBLIC_BASE_URL to the address clients actually use."
            )
        if _is_loopback(public_host) and _is_loopback(self.host) and public_port != self.port:
            warnings.append(
                f"public_base_url port {public_port} does not match the listening port "
                f"{self.port}; generated media URLs will point at the wrong port."
            )
        return warnings


def _best_effort_private(path: Path) -> None:
    """Tighten permissions where the OS supports POSIX modes.

    Windows ACLs are managed by the installer/OS account; chmod is a no-op there.
    The data directory is never placed under the repository's version control.
    """
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # A hostname that we cannot prove is loopback must be treated as public.
        return False


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    raw = _env(name)
    if raw is None:
        return None
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false).")


def _env_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc


def _env_float(name: str) -> float | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        # utf-8-sig accepts both plain UTF-8 and the BOM that Windows editors may
        # add.  tomllib rejects a leading BOM, which would otherwise be a confusing
        # "Invalid statement" error for users editing a generated config.
        text = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(text)
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"Configuration file is not valid UTF-8: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("The configuration root must be a TOML table.")
    return data


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Build validated settings from defaults, TOML, then environment."""

    selected_path: Path | None
    if config_path is not None:
        selected_path = Path(config_path).expanduser()
    elif _env("DOUNAI_CONFIG"):
        selected_path = Path(_env("DOUNAI_CONFIG") or "").expanduser()
    else:
        candidate = Path.cwd() / "dounai.toml"
        selected_path = candidate if candidate.exists() else None

    if selected_path is not None and not selected_path.exists():
        raise ConfigurationError(f"Configuration file not found: {selected_path}")

    raw = _load_toml(selected_path)
    providers = _deep_merge(_DEFAULT_PROVIDERS, raw.get("providers", {}))
    models = raw.get("models", {})
    privacy = raw.get("privacy", {})
    server = raw.get("server", {})
    input_limits = raw.get("input", {})
    processing = raw.get("processing", {})

    data_dir = Path(_env("DOUNAI_DATA_DIR") or raw.get("data_dir") or "data").expanduser()
    host = _env("DOUNAI_HOST") or server.get("host", "127.0.0.1")
    port = _env_int("DOUNAI_PORT") or int(server.get("port", 8000))
    public_base_url = (
        _env("DOUNAI_PUBLIC_BASE_URL")
        or server.get("public_base_url")
        or f"http://127.0.0.1:{port}"
    ).rstrip("/")
    parsed_public = urlsplit(public_base_url)
    if parsed_public.scheme not in {"http", "https"} or not parsed_public.hostname:
        raise ConfigurationError("public_base_url must be an absolute http(s) URL.")

    api_token = _env("DOUNAI_API_TOKEN")
    allow_insecure_bind = _env_bool("DOUNAI_ALLOW_INSECURE_BIND")
    if allow_insecure_bind is None:
        allow_insecure_bind = bool(server.get("allow_insecure_bind", False))

    raw_plugins = _env("DOUNAI_PLUGIN_PATH") or server.get("plugin_path") or ""
    plugin_paths = tuple(
        Path(item).expanduser() for item in raw_plugins.split(os.pathsep) if item.strip()
    )

    active_provider = _env("DOUNAI_ACTIVE_PROVIDER") or models.get("active") or "offline"
    fallback_raw = models.get("fallback", ["local-whisper", "dashscope"])
    if not isinstance(fallback_raw, list) or not all(
        isinstance(item, str) for item in fallback_raw
    ):
        raise ConfigurationError("[models].fallback must be a list of provider ids.")
    fallback_providers = tuple(fallback_raw)

    threshold = _env_float("DOUNAI_VOICEPRINT_THRESHOLD")
    if threshold is None:
        threshold = float(privacy.get("voiceprint_threshold", 0.82))
    if not 0.0 < threshold <= 1.0:
        raise ConfigurationError("voiceprint_threshold must be in (0, 1].")

    retention_hours = _env_int("DOUNAI_MEDIA_RETENTION_HOURS")
    if retention_hours is None:
        retention_hours = int(privacy.get("retention_hours", 72))
    if retention_hours < 1:
        raise ConfigurationError("retention_hours must be at least 1.")

    voiceprint_enabled = _env_bool("DOUNAI_VOICEPRINT_ENABLED")
    if voiceprint_enabled is None:
        voiceprint_enabled = bool(privacy.get("voiceprint_enabled", False))

    wake_phrase = (
        _env("DOUNAI_WAKE_PHRASE") or privacy.get("wake_phrase") or DEFAULT_WAKE_PHRASE
    ).strip()
    if not wake_phrase:
        raise ConfigurationError("wake_phrase cannot be empty.")

    max_text_chars = int(input_limits.get("max_text_chars", 20_000))
    max_image_bytes = int(input_limits.get("max_image_bytes", 15 * 1024 * 1024))
    max_audio_bytes = int(input_limits.get("max_audio_bytes", 25 * 1024 * 1024))
    if max_text_chars < 1 or max_image_bytes < 1024 or max_audio_bytes < 1024:
        raise ConfigurationError("Input limits must be positive.")

    timeout = _env_float("DOUNAI_PROVIDER_TIMEOUT_SECONDS") or float(
        processing.get("provider_timeout_seconds", 60)
    )
    if timeout <= 0:
        raise ConfigurationError("provider_timeout_seconds must be positive.")

    fallback_remote = _env_bool("DOUNAI_ALLOW_REMOTE_FALLBACK")
    if fallback_remote is None:
        fallback_remote = bool(processing.get("allow_remote_fallback", False))

    return Settings(
        data_dir=data_dir.resolve(),
        config_path=selected_path.resolve() if selected_path else None,
        host=host,
        port=port,
        public_base_url=public_base_url,
        api_token=api_token,
        allow_insecure_bind=allow_insecure_bind,
        plugin_paths=plugin_paths,
        max_text_chars=max_text_chars,
        max_image_bytes=max_image_bytes,
        max_audio_bytes=max_audio_bytes,
        media_retention_hours=retention_hours,
        session_context_events=int(processing.get("session_context_events", 3)),
        provider_timeout_seconds=timeout,
        provider_failure_cooldown_seconds=float(
            processing.get("provider_failure_cooldown_seconds", 30)
        ),
        allow_remote_fallback=fallback_remote,
        voiceprint_enabled=voiceprint_enabled,
        wake_phrase=wake_phrase,
        wake_phrase_required=bool(privacy.get("wake_phrase_required", True)),
        voiceprint_threshold=threshold,
        consent_policy_version=str(privacy.get("consent_policy_version", CONSENT_POLICY_VERSION)),
        default_active_provider=active_provider,
        fallback_providers=fallback_providers,
        provider_configs=providers,
    )
