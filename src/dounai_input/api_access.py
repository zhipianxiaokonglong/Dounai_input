"""Safe URL policy, model discovery and capability probes for API connections.

The probe deliberately uses only synthetic data and returns only statuses/latency;
vendor response bodies are never echoed to callers.  "OpenAI compatibility" is
not a single protocol, so audio is probed against both common shapes.
"""

from __future__ import annotations

import base64
import io
import ipaddress
import re
import socket
import struct
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import DounaiError, InputValidationError

DEFAULT_PROBE_TIMEOUT_SECONDS = 20.0
MAX_DISCOVERED_MODELS = 500

# A valid 1x1 transparent PNG.  Used for a minimal image-capability probe.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n0kAAAAASUVORK5CYII="
)

_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "100.100.100.200",
}
_MODEL_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")


class ApiConnectionError(DounaiError):
    code = "api_connection_error"
    status_code = 422


class ApiAuthError(ApiConnectionError):
    code = "api_auth_failed"


class ApiDiscoveryError(DounaiError):
    code = "api_discovery_failed"
    status_code = 502
    retryable = True


def validate_api_key(raw: str) -> str:
    key = raw.strip()
    if not key:
        raise InputValidationError("API key must not be empty.")
    if len(key) > 4096:
        raise InputValidationError("API key is too long.")
    if any(ord(character) < 32 for character in key):
        raise InputValidationError("API key contains control characters.")
    return key


def validate_connection_name(raw: str) -> str:
    name = raw.strip()
    if not _NAME_RE.fullmatch(name):
        raise InputValidationError("Connection name must be 1-80 printable characters.")
    return name


def validate_model_id(raw: str) -> str:
    model_id = raw.strip()
    if not _MODEL_ID_RE.fullmatch(model_id):
        raise InputValidationError("Model id must be 1-200 printable characters.")
    if "/" in model_id and model_id.startswith("/"):
        raise InputValidationError("Model id must not start with a slash.")
    return model_id


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _host_is_metadata(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address) in _METADATA_HOSTS


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InputValidationError(
            f"Cannot resolve endpoint host {host!r}. Check the URL or network."
        ) from exc
    return sorted({str(info[4][0]) for info in infos})


@dataclass(frozen=True)
class ValidatedEndpoint:
    base_url: str
    host: str
    port: int
    resolved_addresses: tuple[str, ...]


def validate_base_url(
    raw: str,
    *,
    allow_private: bool = False,
    allow_http: bool = False,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    resolve_dns: bool = True,
) -> ValidatedEndpoint:
    """Validate a user-supplied OpenAI-compatible base URL.

    Only ``http(s)``, no userinfo, no query/fragment.  Private/loopback targets
    require ``allow_private``; link-local metadata addresses are always rejected.
    DNS names are resolved and every resolved address must satisfy the same rule.
    """

    value = raw.strip().rstrip("/")
    if not value or len(value) > 512:
        raise InputValidationError("Base URL must be 1-512 characters.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise InputValidationError("Base URL must start with http:// or https://.")
    if parsed.scheme == "http" and not allow_http:
        raise InputValidationError(
            "Plain HTTP is disabled for this connection. Enable allow_http only for "
            "a trusted local/private endpoint."
        )
    if parsed.username or parsed.password:
        raise InputValidationError("Base URL must not embed credentials.")
    if parsed.query or parsed.fragment:
        raise InputValidationError("Base URL must not contain a query or fragment.")
    if not parsed.hostname:
        raise InputValidationError("Base URL has no host.")
    host = parsed.hostname.strip("[]").lower()
    if host in _METADATA_HOSTS:
        raise InputValidationError("Cloud metadata endpoints are not allowed.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise InputValidationError("Base URL port is invalid.")
    literal = _as_ip(host)
    resolve = resolver or _default_resolver
    addresses: tuple[str, ...]
    if literal is not None:
        addresses = (str(literal),)
    elif not resolve_dns:
        # Used for persisted connections: validate syntax/policy without DNS.
        # Runtime HTTP still disables redirects; a re-probe can enforce reachability.
        addresses = ()
    else:
        addresses = tuple(resolve(host, port))
        if not addresses:
            raise InputValidationError("Endpoint host did not resolve to an address.")
    for address_text in addresses:
        address = _as_ip(address_text)
        if address is None:
            raise InputValidationError("Endpoint resolved to an invalid address.")
        if _host_is_metadata(address):
            raise InputValidationError("Cloud metadata endpoints are not allowed.")
        if not allow_private and not _address_is_public(address):
            raise InputValidationError(
                "This endpoint resolves to a private/loopback address. Enable "
                "'allow_private' only if you trust the target."
            )
    return ValidatedEndpoint(
        base_url=value,
        host=host,
        port=port,
        resolved_addresses=addresses,
    )


@dataclass(frozen=True)
class DiscoveredModel:
    id: str
    owned_by: str | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    status: str  # supported | unsupported | error | not_tested
    detail: str
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "latency_ms": self.latency_ms}


@dataclass
class ProbeReport:
    model_id: str
    text: ProbeOutcome = field(default_factory=lambda: ProbeOutcome("not_tested", "not probed"))
    image: ProbeOutcome = field(default_factory=lambda: ProbeOutcome("not_tested", "not probed"))
    audio: ProbeOutcome = field(default_factory=lambda: ProbeOutcome("not_tested", "not probed"))
    transcription_protocol: str | None = None

    @property
    def capabilities(self) -> tuple[str, ...]:
        supported = []
        if self.text.status == "supported":
            supported.append("text")
        if self.image.status == "supported":
            supported.append("image")
        if self.audio.status == "supported":
            supported.append("audio")
        return tuple(supported)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "capabilities": list(self.capabilities),
            "transcription_protocol": self.transcription_protocol,
            "probes": {
                "text": self.text.as_dict(),
                "image": self.image.as_dict(),
                "audio": self.audio.as_dict(),
            },
        }


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _safe_status_error(status: int, *, operation: str) -> tuple[bool, str]:
    """Return (is_auth_error, safe message) for an HTTP status."""
    if status in {401, 403}:
        return True, "Authentication failed. Check the API key and endpoint."
    if status == 404:
        return False, f"{operation} endpoint was not found. Check the base URL."
    if status == 429:
        return False, "The provider rate limit was reached. Try again later."
    if status >= 500:
        return False, "The provider reported a server error."
    return False, f"The provider rejected the {operation} request (HTTP {status})."


def _capped_json(response: httpx.Response, *, limit: int = 2_000_000) -> Any:
    if len(response.content) > limit:
        raise ApiDiscoveryError("Provider response is too large.")
    try:
        return response.json()
    except ValueError as exc:
        raise ApiDiscoveryError("Provider returned a non-JSON response.") from exc


async def discover_models(
    endpoint: ValidatedEndpoint,
    api_key: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[DiscoveredModel]:
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            response = await client.get(
                f"{endpoint.base_url}/models", headers=_auth_headers(api_key)
            )
    except httpx.TimeoutException as exc:
        raise ApiDiscoveryError("The provider did not respond before the timeout.") from exc
    except httpx.HTTPError as exc:
        raise ApiDiscoveryError("Could not reach the provider endpoint.") from exc
    if response.status_code >= 400:
        auth, message = _safe_status_error(response.status_code, operation="model list")
        if auth:
            raise ApiAuthError(message)
        raise ApiDiscoveryError(message)
    payload = _capped_json(response)
    if not isinstance(payload, dict):
        raise ApiDiscoveryError("Provider returned an unexpected model list.")
    entries = payload.get("data")
    if entries is None:
        entries = payload.get("models")
    if not isinstance(entries, list):
        raise ApiDiscoveryError("Provider returned an unexpected model list.")
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for entry in entries[:MAX_DISCOVERED_MODELS]:
        if isinstance(entry, str):
            model_id = entry.strip()
            owned_by = None
        elif isinstance(entry, dict):
            model_id = entry.get("id")
            owned_by = entry.get("owned_by")
        else:
            continue
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        if model_id in seen or len(model_id) > 200:
            continue
        seen.add(model_id)
        models.append(
            DiscoveredModel(
                id=model_id,
                owned_by=owned_by if isinstance(owned_by, str) else None,
            )
        )
    return models


def _probe_wav() -> bytes:
    """0.8 s synthetic 16 kHz mono sine; never user audio."""
    import math

    rate = 16_000
    samples = int(rate * 0.8)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(samples):
            value = int(6000 * math.sin(2 * math.pi * 440 * index / rate))
            frames.extend(struct.pack("<h", value))
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def _outcome(status: str, detail: str, started: float) -> ProbeOutcome:
    return ProbeOutcome(
        status=status,
        detail=detail,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


async def probe_model(
    endpoint: ValidatedEndpoint,
    api_key: str,
    model_id: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProbeReport:
    report = ProbeReport(model_id=model_id)
    model_id = validate_model_id(model_id)
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        report.text = await _probe_text(client, endpoint, api_key, model_id)
        # Do not waste calls after an auth failure: the same key would fail all.
        if report.text.status == "error" and "Authentication failed" in report.text.detail:
            report.image = ProbeOutcome("not_tested", "authentication failed")
            report.audio = ProbeOutcome("not_tested", "authentication failed")
            return report
        report.image = await _probe_image(client, endpoint, api_key, model_id)
        (
            report.audio,
            report.transcription_protocol,
        ) = await _probe_audio(client, endpoint, api_key, model_id)
    return report


async def _probe_text(
    client: httpx.AsyncClient,
    endpoint: ValidatedEndpoint,
    api_key: str,
    model_id: str,
) -> ProbeOutcome:
    started = time.monotonic()
    try:
        response = await client.post(
            f"{endpoint.base_url}/chat/completions",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            },
        )
    except httpx.TimeoutException:
        return _outcome("error", "text probe timed out", started)
    except httpx.HTTPError:
        return _outcome("error", "could not reach provider", started)
    if response.status_code == 200:
        if len(response.content) > 2_000_000:
            return _outcome("unsupported", "response too large", started)
        return _outcome("supported", "chat completion accepted", started)
    if response.status_code in {400, 404, 405, 415, 422}:
        return _outcome("unsupported", f"HTTP {response.status_code}", started)
    _auth, detail = _safe_status_error(response.status_code, operation="text")
    return _outcome("error", detail, started)


async def _probe_image(
    client: httpx.AsyncClient,
    endpoint: ValidatedEndpoint,
    api_key: str,
    model_id: str,
) -> ProbeOutcome:
    started = time.monotonic()
    data_url = f"data:image/png;base64,{base64.b64encode(_PNG_1X1).decode('ascii')}"
    try:
        response = await client.post(
            f"{endpoint.base_url}/chat/completions",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json={
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Reply with OK."},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            },
        )
    except httpx.TimeoutException:
        return _outcome("error", "image probe timed out", started)
    except httpx.HTTPError:
        return _outcome("error", "could not reach provider", started)
    if response.status_code == 200:
        return _outcome("supported", "image input accepted", started)
    if response.status_code in {400, 404, 405, 415, 422}:
        return _outcome("unsupported", f"HTTP {response.status_code}", started)
    _auth, detail = _safe_status_error(response.status_code, operation="image")
    return _outcome("error", detail, started)


async def _probe_audio(
    client: httpx.AsyncClient,
    endpoint: ValidatedEndpoint,
    api_key: str,
    model_id: str,
) -> tuple[ProbeOutcome, str | None]:
    audio = _probe_wav()
    started = time.monotonic()
    try:
        response = await client.post(
            f"{endpoint.base_url}/audio/transcriptions",
            headers=_auth_headers(api_key),
            data={"model": model_id, "response_format": "json", "language": "zh"},
            files={"file": ("probe.wav", audio, "audio/wav")},
        )
    except httpx.TimeoutException:
        return _outcome("error", "audio probe timed out", started), None
    except httpx.HTTPError:
        return _outcome("error", "could not reach provider", started), None
    if response.status_code == 200:
        if len(response.content) <= 2_000_000:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                return _outcome(
                    "supported", "audio transcriptions accepted", started
                ), "audio_transcriptions"
        return _outcome("unsupported", "unexpected transcription response", started), None
    if response.status_code in {401, 403, 429} or response.status_code >= 500:
        _auth, detail = _safe_status_error(response.status_code, operation="audio")
        return _outcome("error", detail, started), None

    # Common OpenAI-compatible audio-in-chat shape (Qwen3-ASR etc.).
    encoded = base64.b64encode(audio).decode("ascii")
    try:
        chat_response = await client.post(
            f"{endpoint.base_url}/chat/completions",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json={
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:audio/wav;base64,{encoded}",
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
                "asr_options": {"enable_itn": False, "language": "zh"},
            },
        )
    except httpx.TimeoutException:
        return _outcome("error", "chat-audio probe timed out", started), None
    except httpx.HTTPError:
        return _outcome("error", "could not reach provider", started), None
    if chat_response.status_code == 200:
        if len(chat_response.content) > 2_000_000:
            return _outcome("unsupported", "response too large", started), None
        try:
            payload = chat_response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return _outcome("unsupported", "unexpected chat-audio response", started), None
        if isinstance(content, str) or (
            isinstance(content, list) and any(isinstance(item, dict) for item in content)
        ):
            return _outcome("supported", "chat audio accepted", started), "chat_audio"
        return _outcome("unsupported", "unexpected chat-audio content", started), None
    if chat_response.status_code in {400, 404, 405, 415, 422}:
        return _outcome("unsupported", f"HTTP {chat_response.status_code}", started), None
    _auth, detail = _safe_status_error(chat_response.status_code, operation="audio")
    return _outcome("error", detail, started), None


@dataclass(frozen=True)
class ApiPreset:
    id: str
    name: str
    base_url: str
    docs_url: str
    note: str
    recommended_models: tuple[str, ...]
    requires_http_permission: bool = False
    requires_private_permission: bool = False
    recommended: bool = False


API_PRESETS: tuple[ApiPreset, ...] = (
    ApiPreset(
        id="dashscope",
        name="阿里云百炼 Qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        docs_url="https://bailian.console.aliyun.com/",
        note="中文、方言与中英混说场景推荐；使用 qwen3-asr-flash 做语音转写。",
        recommended_models=("qwen3-asr-flash", "qwen3.8-omni-flash"),
        recommended=True,
    ),
    ApiPreset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        docs_url="https://platform.openai.com/api-keys",
        note="官方转写与视觉接口；需要可访问 OpenAI 的网络。",
        recommended_models=("gpt-4o-transcribe", "gpt-4o-mini", "gpt-4o"),
    ),
    ApiPreset(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        docs_url="https://openrouter.ai/keys",
        note="多厂商聚合，模型列表较大；按需选择并测试。",
        recommended_models=(),
    ),
    ApiPreset(
        id="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        docs_url="https://cloud.siliconflow.cn/account/ak",
        note="国内 OpenAI 兼容聚合，模型可用性以账号为准。",
        recommended_models=(),
    ),
    ApiPreset(
        id="ollama",
        name="Ollama（本机）",
        base_url="http://127.0.0.1:11434/v1",
        docs_url="https://docs.ollama.com/api/openai-compatibility",
        note="本机服务；需要允许 HTTP 与私网访问。API Key 可填任意非空值。",
        recommended_models=(),
        requires_http_permission=True,
        requires_private_permission=True,
    ),
)

PRESET_BY_ID = {preset.id: preset for preset in API_PRESETS}

__all__ = [
    "API_PRESETS",
    "ApiAuthError",
    "ApiConnectionError",
    "ApiDiscoveryError",
    "ApiPreset",
    "DiscoveredModel",
    "ProbeOutcome",
    "ProbeReport",
    "ValidatedEndpoint",
    "discover_models",
    "probe_model",
    "validate_api_key",
    "validate_base_url",
    "validate_connection_name",
    "validate_model_id",
]
