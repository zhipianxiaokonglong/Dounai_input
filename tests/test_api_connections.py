"""API connection lifecycle, URL policy, probes and encrypted key storage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from dounai_input.api_access import (
    ApiAuthError,
    ApiDiscoveryError,
    discover_models,
    probe_model,
    validate_base_url,
)
from dounai_input.errors import InputValidationError
from dounai_input.secrets import SecretStore, SecretStoreError, key_fingerprint
from dounai_input.services import create_services
from tests.conftest import make_settings

# --------------------------------------------------------------------------- secrets


def test_secret_store_roundtrip_and_random_nonce(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    first_cipher, first_nonce = store.encrypt("sk-first")
    second_cipher, second_nonce = store.encrypt("sk-first")
    assert first_cipher != second_cipher
    assert first_nonce != second_nonce
    assert store.decrypt(first_cipher, first_nonce) == "sk-first"
    assert store.decrypt(second_cipher, second_nonce) == "sk-first"
    assert (tmp_path / ".secret.key").stat().st_size == 32


def test_secret_store_does_not_store_plaintext(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    ciphertext, nonce = store.encrypt("sk-very-secret")
    raw = ciphertext + nonce
    assert b"sk-very-secret" not in raw
    assert b"very-secret" not in raw


def test_secret_store_detects_key_rotation(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    ciphertext, nonce = store.encrypt("sk-value")
    (tmp_path / ".secret.key").write_bytes(b"\x00" * 32)
    # A new instance represents a restart; the old instance intentionally caches key.
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path).decrypt(ciphertext, nonce)


def test_fingerprint_is_not_reversible() -> None:
    first = key_fingerprint("sk-abc")
    assert first != "sk-abc"
    assert len(first) <= 12
    assert key_fingerprint("sk-abc") == first
    assert key_fingerprint("sk-abd") != first


# --------------------------------------------------------------------------- keyring


class _FakeKeyring:
    """Minimal stand-in for the optional ``keyring`` module."""

    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.fail_reads = False
        self.fail_writes = False

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_reads:
            raise RuntimeError("keyring unavailable")
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        if self.fail_writes:
            raise RuntimeError("keyring read-only")
        self.passwords[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        self.passwords.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    import sys

    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_keyring_backend_stores_master_key_outside_data_dir(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    store = SecretStore(tmp_path, backend="keyring")
    ciphertext, nonce = store.encrypt("sk-keyring-value")
    assert not (tmp_path / ".secret.key").exists()
    assert len(fake_keyring.passwords) == 1
    assert store.backend == "aes-gcm-keyring"
    # A restart must read the same key from the keyring.
    assert SecretStore(tmp_path, backend="keyring").decrypt(ciphertext, nonce) == "sk-keyring-value"


def test_existing_file_key_is_migrated_into_keyring(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    file_store = SecretStore(tmp_path, backend="file")
    ciphertext, nonce = file_store.encrypt("sk-migrate")
    assert (tmp_path / ".secret.key").exists()

    keyring_store = SecretStore(tmp_path, backend="keyring")
    assert keyring_store.decrypt(ciphertext, nonce) == "sk-migrate"
    assert not (tmp_path / ".secret.key").exists()
    assert fake_keyring.passwords


def test_keyring_key_is_recovered_when_switching_back_to_file(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    keyring_store = SecretStore(tmp_path, backend="keyring")
    ciphertext, nonce = keyring_store.encrypt("sk-recover")
    assert not (tmp_path / ".secret.key").exists()

    file_store = SecretStore(tmp_path, backend="file")
    assert file_store.decrypt(ciphertext, nonce) == "sk-recover"
    assert (tmp_path / ".secret.key").exists()
    assert not fake_keyring.passwords


def test_auto_backend_falls_back_when_keyring_is_unusable(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    fake_keyring.fail_writes = True
    fake_keyring.fail_reads = True
    store = SecretStore(tmp_path, backend="auto")
    ciphertext, nonce = store.encrypt("sk-fallback")
    assert (tmp_path / ".secret.key").exists()
    assert store.backend == "aes-gcm-local"
    assert store.decrypt(ciphertext, nonce) == "sk-fallback"


def test_keyring_backend_reports_missing_dependency(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path, backend="keyring").encrypt("sk-value")
    assert not (tmp_path / ".secret.key").exists()


def test_unknown_secret_backend_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DOUNAI_SECRET_BACKEND", "vault")
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path)


def test_delete_master_key_clears_file_and_keyring(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    store = SecretStore(tmp_path, backend="keyring")
    store.encrypt("sk-remove")
    assert fake_keyring.passwords
    store.delete_master_key()
    assert not fake_keyring.passwords
    assert not (tmp_path / ".secret.key").exists()


# --------------------------------------------------------------------------- URL policy


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "file:///tmp/x",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?x=1",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/v1",
    ],
)
def test_unsafe_base_urls_are_rejected(url: str) -> None:
    with pytest.raises(InputValidationError):
        validate_base_url(
            url, allow_private=True, allow_http=True, resolver=lambda h, p: ["93.184.216.34"]
        )


def test_private_endpoint_requires_explicit_permission() -> None:
    with pytest.raises(InputValidationError):
        validate_base_url(
            "http://127.0.0.1:11434/v1",
            allow_private=False,
            allow_http=True,
            resolver=lambda host, port: ["127.0.0.1"],
        )
    endpoint = validate_base_url(
        "http://127.0.0.1:11434/v1",
        allow_private=True,
        allow_http=True,
        resolver=lambda host, port: ["127.0.0.1"],
    )
    assert endpoint.port == 11434


def test_http_requires_explicit_permission() -> None:
    with pytest.raises(InputValidationError):
        validate_base_url(
            "http://example.com/v1",
            allow_http=False,
            resolver=lambda host, port: ["93.184.216.34"],
        )
    endpoint = validate_base_url(
        "http://example.com/v1",
        allow_http=True,
        resolver=lambda host, port: ["93.184.216.34"],
    )
    assert endpoint.base_url.startswith("http://")


def test_dns_rebinding_to_private_metadata_is_rejected() -> None:
    with pytest.raises(InputValidationError):
        validate_base_url(
            "https://public.example/v1",
            resolver=lambda host, port: ["93.184.216.34", "169.254.169.254"],
        )


def test_private_dns_requires_permission() -> None:
    with pytest.raises(InputValidationError):
        validate_base_url(
            "https://internal.example/v1",
            resolver=lambda host, port: ["10.0.0.5"],
        )


# --------------------------------------------------------------------------- discovery/probes


def _model_list_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer test-key"
    return httpx.Response(
        200,
        json={
            "object": "list",
            "data": [
                {"id": "model-a", "object": "model", "owned_by": "vendor"},
                {"id": "model-b", "object": "model"},
                {"id": "model-a"},
                {"id": ""},
                42,
            ],
        },
    )


def test_discover_models_filters_and_authenticates() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )
    models = __import__("asyncio").run(
        discover_models(
            endpoint,
            "test-key",
            transport=httpx.MockTransport(_model_list_handler),
        )
    )
    assert [model.id for model in models] == ["model-a", "model-b"]
    assert models[0].owned_by == "vendor"


def test_discover_models_auth_error_is_safe() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "secret in body"}})

    with pytest.raises(ApiAuthError) as exc_info:
        __import__("asyncio").run(
            discover_models(endpoint, "test-key", transport=httpx.MockTransport(handler))
        )
    assert "secret in body" not in str(exc_info.value)
    assert "Authentication failed" in str(exc_info.value)


def test_discover_models_non_json_and_oversize() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )
    for response in (
        httpx.Response(200, text="<html>"),
        httpx.Response(200, content=b"x" * 2_000_001, headers={"content-type": "application/json"}),
    ):
        with pytest.raises(ApiDiscoveryError):
            __import__("asyncio").run(
                discover_models(
                    endpoint,
                    "test-key",
                    transport=httpx.MockTransport(lambda request, r=response: r),
                )
            )


def _probe_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": []})
    if request.url.path.endswith("/audio/transcriptions"):
        return httpx.Response(200, json={"text": ""})
    body = json.loads(request.content)
    content = body["messages"][0]["content"]
    if isinstance(content, list) and any(item.get("type") == "image_url" for item in content):
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    if isinstance(content, list) and any(item.get("type") == "input_audio" for item in content):
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})


def test_probe_detects_text_image_and_transcription_audio() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )
    report = __import__("asyncio").run(
        probe_model(
            endpoint,
            "test-key",
            "multi-model",
            transport=httpx.MockTransport(_probe_handler),
        )
    )
    assert report.capabilities == ("text", "image", "audio")
    assert report.transcription_protocol == "audio_transcriptions"
    assert report.text.status == "supported"


def test_probe_audio_falls_back_to_chat_audio() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(404, json={"error": "not found"})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            content = body["messages"][0]["content"]
            if isinstance(content, list) and any(
                item.get("type") == "input_audio" for item in content
            ):
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": [{"text": "你好"}]}}]}
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        return httpx.Response(404)

    report = __import__("asyncio").run(
        probe_model(
            endpoint,
            "test-key",
            "qwen-asr",
            transport=httpx.MockTransport(handler),
        )
    )
    assert report.audio.status == "supported"
    assert report.transcription_protocol == "chat_audio"


def test_probe_marks_unsupported_without_guessing_from_model_name() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "unsupported"})

    report = __import__("asyncio").run(
        probe_model(
            endpoint,
            "test-key",
            "whisper-large",
            transport=httpx.MockTransport(handler),
        )
    )
    assert report.capabilities == ()
    assert report.text.status == "unsupported"
    assert report.audio.status == "unsupported"


def test_probe_stops_after_auth_failure() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": "bad key"})

    report = __import__("asyncio").run(
        probe_model(
            endpoint,
            "bad-key",
            "model",
            transport=httpx.MockTransport(handler),
        )
    )
    assert len(calls) == 1
    assert report.image.status == "not_tested"
    assert "Authentication failed" in report.text.detail


def test_probe_timeout_is_an_error_not_a_crash() -> None:
    endpoint = validate_base_url(
        "https://api.example.test/v1",
        resolver=lambda host, port: ["93.184.216.34"],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    report = __import__("asyncio").run(
        probe_model(
            endpoint,
            "key",
            "model",
            transport=httpx.MockTransport(handler),
        )
    )
    assert report.text.status == "error"
    assert "timed out" in report.text.detail


# --------------------------------------------------------------------------- service/API


def _connection_payload(model_id: str = "model-a") -> dict:
    return {
        "name": "Test API",
        "base_url": "https://93.184.216.34/v1",
        "api_key": "sk-test-secret",
        "allow_private": False,
        "allow_http": False,
        "models": [
            {
                "model_id": model_id,
                "capabilities": ["text", "audio"],
                "transcription_protocol": "audio_transcriptions",
                "probes": {
                    "text": {"status": "supported", "detail": "ok", "latency_ms": 5},
                    "image": {"status": "unsupported", "detail": "HTTP 400"},
                    "audio": {"status": "supported", "detail": "ok"},
                },
            }
        ],
    }


def test_connection_service_encrypts_key_and_registers_provider(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = create_services(settings)
    result = services.api_connections.create_connection(
        name="Test API",
        base_url="https://93.184.216.34/v1",
        api_key="sk-test-secret",
        allow_private=False,
        allow_http=False,
        probes=_connection_payload()["models"],
    )
    assert result["models"][0]["capabilities"] == ["text", "audio"]
    provider = services.providers.registry.get(result["models"][0]["provider_id"])
    assert provider is not None
    assert provider.availability() == (True, None)
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute("SELECT key_ciphertext FROM api_connections").fetchone()
    assert b"sk-test-secret" not in row[0]


def test_connection_api_never_returns_key(client, app) -> None:
    payload = _connection_payload()
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 201, response.text
    assert "sk-test-secret" not in response.text
    listing = client.get("/v1/api-connections")
    assert "sk-test-secret" not in listing.text
    assert response.json()["key_fingerprint"]
    resolved = app.state.services["api_connections"].store.list_api_connections()[0]
    assert resolved.key_ciphertext


def test_connection_requires_probed_capabilities(client) -> None:
    payload = _connection_payload()
    payload["models"][0]["capabilities"] = []
    payload["models"][0]["probes"]["text"]["status"] = "unsupported"
    payload["models"][0]["probes"]["audio"]["status"] = "unsupported"
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 422


def test_connection_capabilities_come_from_probes_only(client) -> None:
    payload = _connection_payload()
    payload["models"][0].pop("capabilities")
    payload["models"][0]["probes"]["image"]["status"] = "supported"
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["models"][0]["capabilities"] == ["text", "image", "audio"]


def test_connection_requires_protocol_for_audio(client) -> None:
    payload = _connection_payload()
    payload["models"][0]["transcription_protocol"] = None
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 422


def test_connection_patch_rotates_key_without_echo(client, app) -> None:
    payload = _connection_payload()
    created = client.post("/v1/api-connections", json=payload).json()
    connection_id = created["id"]
    old_fingerprint = created["key_fingerprint"]
    patched = client.patch(
        f"/v1/api-connections/{connection_id}",
        json={"name": "Renamed", "api_key": "sk-rotated-secret"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "Renamed"
    assert body["key_fingerprint"] != old_fingerprint
    assert "sk-rotated-secret" not in patched.text
    provider = app.state.services["providers"].registry.get(created["models"][0]["provider_id"])
    assert provider is not None
    assert provider.availability() == (True, None)


def test_delete_connection_unregisters_and_falls_back(client, app) -> None:
    payload = _connection_payload()
    created = client.post("/v1/api-connections", json=payload).json()
    provider_id = created["models"][0]["provider_id"]
    assert client.put("/v1/providers/active", json={"provider_id": provider_id}).status_code == 200
    deleted = client.delete(f"/v1/api-connections/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["active_provider"] == "offline"
    assert app.state.services["providers"].registry.get(provider_id) is None
    # The last ciphertext is gone, so the master key is deleted as well.
    assert not (app.state.services["settings"].data_dir / ".secret.key").exists()


def test_delete_connection_keeps_master_key_while_others_remain(client, app) -> None:
    first = client.post("/v1/api-connections", json=_connection_payload("model-a")).json()
    second_payload = _connection_payload("model-b")
    second_payload["name"] = "Second API"
    client.post(
        "/v1/api-connections",
        json=second_payload,
    )
    assert client.delete(f"/v1/api-connections/{first['id']}").status_code == 200
    assert (app.state.services["settings"].data_dir / ".secret.key").exists()


def test_delete_model_keeps_connection(client, app) -> None:
    payload = _connection_payload()
    created = client.post("/v1/api-connections", json=payload).json()
    provider_id = created["models"][0]["provider_id"]
    response = client.delete(f"/v1/api-connections/{created['id']}/models/{provider_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    connection = client.get(f"/v1/api-connections/{created['id']}").json()
    assert connection["models"] == []


def test_ssrf_rejected_over_api(client) -> None:
    payload = _connection_payload()
    payload["base_url"] = "http://169.254.169.254/latest/meta-data"
    payload["allow_http"] = True
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 422
    assert "metadata" in response.text.lower()


def test_private_http_endpoint_requires_flags(client) -> None:
    payload = _connection_payload()
    payload["base_url"] = "http://127.0.0.1:11434/v1"
    payload["allow_http"] = True
    payload["allow_private"] = False
    response = client.post("/v1/api-connections", json=payload)
    assert response.status_code == 422
    assert "private" in response.text.lower() or "loopback" in response.text.lower()


def test_discover_and_probe_endpoints_dont_persist(client, app) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "alpha"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    # Discovery uses the real resolver; this test only checks the endpoint wiring.
    response = client.post(
        "/v1/api-connections/discover",
        json={
            "base_url": "https://93.184.216.34/v1",
            "api_key": "sk-temporary",
            "allow_private": False,
            "allow_http": False,
        },
    )
    # Example.test has no resolvable public IP; the safe error is 422, not a crash.
    assert response.status_code in {422, 502}
    assert "sk-temporary" not in response.text
    assert app.state.services["store"].list_api_connections() == []


def test_connection_presets_available(client) -> None:
    response = client.get("/v1/api-presets")
    assert response.status_code == 200
    presets = response.json()["presets"]
    assert any(item["id"] == "dashscope" for item in presets)
    assert any(item["recommended"] for item in presets)


def test_config_has_no_static_vendor_providers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert "dashscope" not in settings.provider_configs
    assert "openai" not in settings.provider_configs


def test_provider_sync_survives_undecryptable_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = create_services(settings)
    result = services.api_connections.create_connection(
        name="Test",
        base_url="https://93.184.216.34/v1",
        api_key="sk-test-secret",
        allow_private=False,
        allow_http=False,
        probes=_connection_payload()["models"],
    )
    provider_id = result["models"][0]["provider_id"]
    services.providers.set_active(provider_id)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE api_connections SET key_ciphertext = ? WHERE id = ?",
            (b"corrupted", services.store.list_api_connections()[0].id),
        )
    services.api_connections.resync()
    assert services.providers.registry.get(provider_id) is None
    # An active provider that disappears must not stay selected.
    assert services.providers.active_id == "offline"


def test_long_model_ids_keep_provider_ids_valid_and_distinct(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = create_services(settings)
    long_a = "very-long-vendor-model-name-alpha-with-many-segments-2026-preview"
    long_b = "very-long-vendor-model-name-beta-with-many-segments-2026-preview"
    probes = []
    for model_id in (long_a, long_b):
        item = dict(_connection_payload(model_id)["models"][0])
        probes.append(item)
    result = services.api_connections.create_connection(
        name="Long names",
        base_url="https://93.184.216.34/v1",
        api_key="sk-test-secret",
        allow_private=False,
        allow_http=False,
        probes=probes,
    )
    provider_ids = [model["provider_id"] for model in result["models"]]
    assert len(provider_ids) == 2
    assert len(set(provider_ids)) == 2
    for provider_id in provider_ids:
        assert len(provider_id) <= 48
        assert provider_id.startswith("api-")
        assert services.providers.registry.get(provider_id) is not None


def test_delete_all_data_clears_connections(client, app) -> None:
    created = client.post("/v1/api-connections", json=_connection_payload()).json()
    provider_id = created["models"][0]["provider_id"]
    assert client.delete("/v1/privacy/data?confirm=true").status_code == 200
    assert client.get("/v1/api-connections").json()["connections"] == []
    assert app.state.services["providers"].registry.get(provider_id) is None
    # No encrypted key survives, so the AES master key is deleted too.
    assert not (app.state.services["settings"].data_dir / ".secret.key").exists()
