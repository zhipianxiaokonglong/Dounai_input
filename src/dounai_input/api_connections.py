"""API connection lifecycle: encrypted keys, discovery, probing and providers.

This service is the only place that turns a stored connection into a live
provider.  Keys are decrypted just-in-time, never returned to callers, and never
included in exports.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .api_access import (
    API_PRESETS,
    ProbeReport,
    ValidatedEndpoint,
    discover_models,
    probe_model,
    validate_api_key,
    validate_base_url,
    validate_connection_name,
    validate_model_id,
)
from .errors import ConflictError, InputValidationError, NotFoundError
from .providers.manager import ProviderManager
from .providers.openai_compatible import OpenAICompatibleProvider
from .secrets import SecretStore, SecretStoreError, key_fingerprint
from .settings_helpers import utc_now_iso
from .store import ApiConnectionRecord, ApiModelRecord, Store

logger = logging.getLogger(__name__)

_PROVIDER_ID_MAX = 48


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:36] or "model"


@dataclass(frozen=True)
class ConnectionView:
    id: str
    name: str
    base_url: str
    key_fingerprint: str
    allow_private: bool
    allow_http: bool
    created_at: str
    updated_at: str
    models: list[dict[str, Any]]


class ApiConnectionService:
    def __init__(
        self,
        store: Store,
        secrets: SecretStore,
        providers: ProviderManager,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
    ) -> None:
        self.store = store
        self.secrets = secrets
        self.providers = providers
        self._transport = transport
        self._resolver = resolver
        self._sync_providers()

    # ------------------------------------------------------------------ crypto

    def _key_for(self, record: ApiConnectionRecord) -> str:
        try:
            return self.secrets.decrypt(
                record.key_ciphertext, record.key_nonce, purpose="api-connection"
            )
        except SecretStoreError:
            raise ConflictError(
                "Stored API key cannot be decrypted. Re-save the connection key."
            ) from None

    def _encrypt_key(self, key: str) -> tuple[bytes, bytes]:
        try:
            return self.secrets.encrypt(key, purpose="api-connection")
        except SecretStoreError as exc:
            raise ConflictError(
                "Cannot encrypt the API key with the configured secret backend. "
                "Check DOUNAI_SECRET_BACKEND and the OS keyring."
            ) from exc

    # ------------------------------------------------------------------ views

    def _view(self, record: ApiConnectionRecord) -> ConnectionView:
        models = [
            {
                "provider_id": model.provider_id,
                "model_id": model.model_id,
                "display_name": model.display_name,
                "capabilities": list(model.capabilities),
                "transcription_protocol": model.transcription_protocol,
                "probes": model.probes,
                "updated_at": model.updated_at,
                "active": model.provider_id == self.providers.active_id,
            }
            for model in self.store.list_api_models(record.id)
        ]
        return ConnectionView(
            id=record.id,
            name=record.name,
            base_url=record.base_url,
            key_fingerprint=record.key_fingerprint,
            allow_private=record.allow_private,
            allow_http=record.allow_http,
            created_at=record.created_at,
            updated_at=record.updated_at,
            models=models,
        )

    def list_connections(self) -> list[dict[str, Any]]:
        return [vars(self._view(record)) for record in self.store.list_api_connections()]

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        record = self.store.get_api_connection(connection_id)
        if record is None:
            raise NotFoundError("API connection not found.")
        return vars(self._view(record))

    def presets(self) -> list[dict[str, Any]]:
        return [
            {
                "id": preset.id,
                "name": preset.name,
                "base_url": preset.base_url,
                "docs_url": preset.docs_url,
                "note": preset.note,
                "recommended_models": list(preset.recommended_models),
                "requires_http_permission": preset.requires_http_permission,
                "requires_private_permission": preset.requires_private_permission,
                "recommended": preset.recommended,
            }
            for preset in API_PRESETS
        ]

    # ------------------------------------------------------------------ discovery/probe

    def _endpoint(
        self, base_url: str, *, allow_private: bool, allow_http: bool
    ) -> ValidatedEndpoint:
        return validate_base_url(
            base_url,
            allow_private=allow_private,
            allow_http=allow_http,
            resolver=self._resolver,
        )

    def _validate_persisted_endpoint(
        self, base_url: str, *, allow_private: bool, allow_http: bool
    ) -> None:
        """Persisted rows only get syntax/policy checks; no DNS during reads."""
        validate_base_url(
            base_url,
            allow_private=allow_private,
            allow_http=allow_http,
            resolve_dns=False,
        )

    async def discover(
        self,
        *,
        base_url: str,
        api_key: str,
        allow_private: bool,
        allow_http: bool,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        endpoint = self._endpoint(base_url, allow_private=allow_private, allow_http=allow_http)
        key = validate_api_key(api_key)
        models = await discover_models(
            endpoint, key, timeout=timeout_seconds, transport=self._transport
        )
        return {
            "base_url": endpoint.base_url,
            "resolved_addresses": list(endpoint.resolved_addresses),
            "models": [{"id": item.id, "owned_by": item.owned_by} for item in models],
        }

    async def probe(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        allow_private: bool,
        allow_http: bool,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        endpoint = self._endpoint(base_url, allow_private=allow_private, allow_http=allow_http)
        key = validate_api_key(api_key)
        report = await probe_model(
            endpoint,
            key,
            validate_model_id(model_id),
            timeout=timeout_seconds,
            transport=self._transport,
        )
        return report.as_dict()

    async def refresh_models(self, connection_id: str) -> list[dict[str, Any]]:
        record = self._require_connection(connection_id)
        self._validate_persisted_endpoint(
            record.base_url,
            allow_private=record.allow_private,
            allow_http=record.allow_http,
        )
        endpoint = self._endpoint(
            record.base_url,
            allow_private=record.allow_private,
            allow_http=record.allow_http,
        )
        models = await discover_models(endpoint, self._key_for(record), transport=self._transport)
        return [{"id": item.id, "owned_by": item.owned_by} for item in models]

    async def probe_stored_model(self, connection_id: str, model_id: str) -> dict[str, Any]:
        record = self._require_connection(connection_id)
        self._validate_persisted_endpoint(
            record.base_url,
            allow_private=record.allow_private,
            allow_http=record.allow_http,
        )
        endpoint = self._endpoint(
            record.base_url,
            allow_private=record.allow_private,
            allow_http=record.allow_http,
        )
        existing = next(
            (
                model
                for model in self.store.list_api_models(connection_id)
                if model.model_id == model_id
            ),
            None,
        )
        report = await probe_model(
            endpoint,
            self._key_for(record),
            validate_model_id(model_id),
            transport=self._transport,
        )
        if existing is not None:
            self._save_model(record, existing.provider_id, report)
        return report.as_dict()

    # ------------------------------------------------------------------ persistence

    def _require_connection(self, connection_id: str) -> ApiConnectionRecord:
        record = self.store.get_api_connection(connection_id)
        if record is None:
            raise NotFoundError("API connection not found.")
        return record

    def create_connection(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        allow_private: bool,
        allow_http: bool,
        probes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        endpoint = self._endpoint(base_url, allow_private=allow_private, allow_http=allow_http)
        key = validate_api_key(api_key)
        if not probes:
            raise InputValidationError("Select at least one probed model.")
        now = utc_now_iso()
        connection_id = f"conn_{uuid.uuid4().hex[:16]}"
        ciphertext, nonce = self._encrypt_key(key)
        record = ApiConnectionRecord(
            id=connection_id,
            name=validate_connection_name(name),
            base_url=endpoint.base_url,
            allow_private=allow_private,
            allow_http=allow_http,
            key_ciphertext=ciphertext,
            key_nonce=nonce,
            key_fingerprint=key_fingerprint(key),
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_api_connection(record)
        try:
            for probe in probes:
                model_id = validate_model_id(str(probe.get("model_id", "")))
                report = ProbeReport(
                    model_id=model_id,
                    transcription_protocol=str(probe.get("transcription_protocol") or "") or None,
                )
                raw = probe.get("probes")
                if not isinstance(raw, dict):
                    raise InputValidationError("Each model needs probe results.")
                from .api_access import ProbeOutcome

                for capability in ("text", "image", "audio"):
                    item = raw.get(capability, {})
                    status = (
                        str(item.get("status", "not_tested"))
                        if isinstance(item, dict)
                        else "not_tested"
                    )
                    if status not in {"supported", "unsupported", "error", "not_tested"}:
                        status = "error"
                    detail = (
                        str(item.get("detail", "")).strip()[:160] if isinstance(item, dict) else ""
                    )
                    setattr(
                        report,
                        capability,
                        ProbeOutcome(
                            status=status,
                            detail=detail or "probe result",
                            latency_ms=int(item.get("latency_ms", 0))
                            if isinstance(item, dict)
                            else 0,
                        ),
                    )
                if not report.capabilities:
                    raise InputValidationError(
                        f"Model {model_id!r} has no verified capability; do not save it."
                    )
                protocol = report.transcription_protocol
                if protocol is not None and protocol not in {
                    "audio_transcriptions",
                    "chat_audio",
                }:
                    raise InputValidationError("Unsupported transcription protocol.")
                if report.audio.status == "supported" and protocol is None:
                    raise InputValidationError(
                        "An audio-capable probe must record its transcription protocol."
                    )
                if report.audio.status != "supported" and protocol is not None:
                    protocol = None
                    report.transcription_protocol = None
                if len(probes) > 20:
                    raise InputValidationError("At most 20 models can be saved per connection.")
                self._save_model(record, self._provider_id(connection_id, model_id), report)
        except Exception:
            self.store.delete_api_connection(connection_id)
            raise
        self._sync_providers()
        return self.get_connection(connection_id)

    def _provider_id(self, connection_id: str, model_id: str) -> str:
        short = connection_id.rsplit("_", 1)[-1][:8]
        prefix = f"api-{short}-"
        # "api-" + 8-char connection short id + "-" leaves 35 characters for the
        # slug; provider ids stay within the same 48-char budget as the schema.
        room = _PROVIDER_ID_MAX - len(prefix)
        base = f"{prefix}{_slug(model_id)[:room]}"
        candidate = base
        index = 2
        while self.store.get_api_model(candidate) is not None:
            suffix = f"-{index}"
            candidate = f"{base[: _PROVIDER_ID_MAX - len(suffix)]}{suffix}"
            index += 1
        return candidate

    def _save_model(
        self, connection: ApiConnectionRecord, provider_id: str, report: ProbeReport
    ) -> None:
        existing = self.store.get_api_model(provider_id)
        if existing is not None and existing.connection_id != connection.id:
            raise ConflictError("Provider id collision for this connection.")
        now = utc_now_iso()
        self.store.upsert_api_model(
            ApiModelRecord(
                provider_id=provider_id,
                connection_id=connection.id,
                model_id=report.model_id,
                display_name=report.model_id[:120],
                capabilities=report.capabilities,
                probes=report.as_dict()["probes"],
                transcription_protocol=report.transcription_protocol,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
        )

    def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        allow_private: bool | None = None,
        allow_http: bool | None = None,
    ) -> dict[str, Any]:
        record = self._require_connection(connection_id)
        new_name = validate_connection_name(name) if name is not None else record.name
        new_url = record.base_url
        new_private = record.allow_private if allow_private is None else allow_private
        new_http = record.allow_http if allow_http is None else allow_http
        if base_url is not None:
            new_url = self._endpoint(
                base_url, allow_private=new_private, allow_http=new_http
            ).base_url
        ciphertext, nonce, fingerprint = (
            record.key_ciphertext,
            record.key_nonce,
            record.key_fingerprint,
        )
        if api_key is not None:
            key = validate_api_key(api_key)
            ciphertext, nonce = self._encrypt_key(key)
            fingerprint = key_fingerprint(key)
        updated = ApiConnectionRecord(
            id=record.id,
            name=new_name,
            base_url=new_url,
            allow_private=new_private,
            allow_http=new_http,
            key_ciphertext=ciphertext,
            key_nonce=nonce,
            key_fingerprint=fingerprint,
            created_at=record.created_at,
            updated_at=utc_now_iso(),
        )
        self.store.upsert_api_connection(updated)
        # Existing capability probes are retained; URL/key changes may require a
        # re-probe, which the UI prompts for.  Syncing also refreshes live provider
        # instances so a rotated key takes effect immediately.
        self._sync_providers()
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: str) -> dict[str, Any]:
        record = self._require_connection(connection_id)
        models = self.store.list_api_models(connection_id)
        self.store.delete_api_connection(connection_id)
        self._sync_providers()
        if not self.store.list_api_connections():
            # Last ciphertext is gone; do not keep the master key around.
            self.secrets.delete_master_key()
        return {
            "deleted": True,
            "connection_id": record.id,
            "models_removed": len(models),
            "active_provider": self.providers.active_id,
        }

    def delete_model(self, connection_id: str, provider_id: str) -> dict[str, Any]:
        record = self._require_connection(connection_id)
        model = self.store.get_api_model(provider_id)
        if model is None or model.connection_id != record.id:
            raise NotFoundError("API model not found for this connection.")
        self.store.delete_api_model(provider_id)
        self._sync_providers()
        return {
            "deleted": True,
            "provider_id": provider_id,
            "active_provider": self.providers.active_id,
        }

    # ------------------------------------------------------------------ provider sync

    def _sync_providers(self) -> None:
        connections = {record.id: record for record in self.store.list_api_connections()}
        models = self.store.list_api_models()
        expected_ids = {model.provider_id for model in models}
        failed_ids: set[str] = set()
        for provider in self.providers.registry.all():
            if provider.manifest.id.startswith("api-") and provider.manifest.id not in expected_ids:
                self.providers.unregister_dynamic(provider.manifest.id, fallback=False)
        for model in models:
            record = connections.get(model.connection_id)
            if record is None:
                self.providers.unregister_dynamic(model.provider_id, fallback=False)
                failed_ids.add(model.provider_id)
                continue
            try:
                key = self._key_for(record)
            except ConflictError:
                logger.warning(
                    "cannot decrypt API key for connection %s; provider disabled", record.id
                )
                self.providers.unregister_dynamic(model.provider_id, fallback=False)
                failed_ids.add(model.provider_id)
                continue
            provider = OpenAICompatibleProvider(
                provider_id=model.provider_id,
                name=f"{record.name} · {model.model_id}",
                base_url=record.base_url,
                model=model.model_id,
                api_key=key,
                protocol=model.transcription_protocol or "audio_transcriptions",
                capabilities=model.capabilities,
                description=(
                    f"用户接入的 OpenAI 兼容 API：{record.base_url}。"
                    "能力由探测结果决定，密钥只保存在本机加密存储。"
                ),
                timeout_seconds=60.0,
            )
            self.providers.register_dynamic(provider)
        active = self.providers.active_id
        if active.startswith("api-") and (active not in expected_ids or active in failed_ids):
            # The active model vanished or cannot be decrypted: never leave the
            # gateway pointing at a provider that no longer exists.
            self.providers.fallback_active()

    def resync(self) -> None:
        self._sync_providers()


def create_api_connection_service(
    store: Store,
    providers: ProviderManager,
    data_dir: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ApiConnectionService:
    return ApiConnectionService(
        store,
        SecretStore(data_dir),
        providers,
        transport=transport,
    )
