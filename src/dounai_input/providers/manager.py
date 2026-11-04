"""Runtime provider selection, capability-aware fallback and failure isolation."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..errors import ConflictError, NotFoundError, ProviderError
from ..store import Store
from .base import (
    BaseProvider,
    ProviderAttempt,
    TranscriptionOutcome,
    TranscriptionRequest,
)
from .registry import ProviderRegistry, build_registry

logger = logging.getLogger(__name__)


@dataclass
class _FailureState:
    open_until: float = 0.0
    failures: int = 0
    last_reason: str | None = None


class ProviderManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry or build_registry(settings)
        persisted = store.get_setting("active_provider")
        selected = persisted or settings.default_active_provider
        if self.registry.get(selected) is None:
            selected = (
                "offline"
                if self.registry.get("offline")
                else (self.registry.ids()[0] if self.registry.ids() else "")
            )
        self._active_id = selected
        self._failures: dict[str, _FailureState] = {}

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def active(self) -> BaseProvider | None:
        return self.registry.get(self._active_id)

    def list_providers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for provider in self.registry.all():
            try:
                info = provider.info()
            except Exception as exc:
                logger.warning(
                    "provider %s info failed: %s", provider.manifest.id, type(exc).__name__
                )
                info = {
                    "id": provider.manifest.id,
                    "name": provider.manifest.name,
                    "version": provider.manifest.version,
                    "description": provider.manifest.description,
                    "capabilities": list(provider.manifest.capabilities),
                    "privacy": provider.manifest.privacy,
                    "requires_api_key": provider.manifest.requires_api_key,
                    "can_transcribe": provider.manifest.can_transcribe,
                    "available": False,
                    "unavailable_reason": "Provider status check failed.",
                    "homepage": provider.manifest.homepage,
                }
            info["active"] = provider.manifest.id == self._active_id
            state = self._failures.get(provider.manifest.id)
            info["circuit_open"] = bool(state and state.open_until > time.monotonic())
            info["consecutive_failures"] = state.failures if state else 0
            result.append(info)
        priority = {
            provider_id: index for index, provider_id in enumerate(self.settings.fallback_providers)
        }
        result.sort(
            key=lambda item: (
                not item["active"],
                not item["available"],
                priority.get(str(item["id"]), 999),
                str(item["name"]),
            )
        )
        return result

    def status(self) -> dict[str, Any]:
        active = self.active
        fallback = list(self.settings.fallback_providers)
        active_available = False
        if active is not None:
            active_available, _ = self._availability(active)
        return {
            "active_provider": self._active_id,
            "active_provider_name": active.manifest.name if active else None,
            "active_capabilities": list(active.manifest.capabilities) if active else [],
            "active_privacy": active.manifest.privacy if active else None,
            "active_available": active_available,
            "fallback_chain": fallback,
            "allow_remote_fallback": self.settings.allow_remote_fallback,
            "locality_guard": (
                "Remote fallback candidates are skipped unless "
                "allow_remote_fallback is explicitly enabled."
            ),
            "plugin_errors": list(self.registry.errors),
        }

    def set_active(self, provider_id: str) -> dict[str, Any]:
        provider = self.registry.get(provider_id)
        if provider is None:
            raise NotFoundError(f"Unknown provider: {provider_id}")
        self._active_id = provider_id
        self.store.set_setting("active_provider", provider_id)
        self.store.audit_privacy("provider_switched", {"provider_id": provider_id})
        info = provider.info()
        info["active"] = True
        return info

    def register_dynamic(self, provider: BaseProvider) -> None:
        """Install or replace a connection-backed provider under the same id."""
        self.registry.unregister(provider.manifest.id)
        self.registry.register(provider, origin="api-connection")
        self._failures.pop(provider.manifest.id, None)

    def unregister_dynamic(self, provider_id: str, *, fallback: bool = True) -> None:
        self.registry.unregister(provider_id)
        self._failures.pop(provider_id, None)
        if fallback and self._active_id == provider_id:
            self.fallback_active()

    def fallback_active(self) -> str:
        """Pick a safe provider after the active one disappears."""
        for candidate in ("offline", *self.settings.fallback_providers):
            if self.registry.get(candidate) is not None:
                self._active_id = candidate
                self.store.set_setting("active_provider", candidate)
                self.store.audit_privacy(
                    "provider_switched", {"provider_id": candidate, "reason": "active_removed"}
                )
                return candidate
        available = self.registry.ids()
        candidate = available[0] if available else ""
        self._active_id = candidate
        self.store.set_setting("active_provider", candidate)
        return candidate

    def _candidate_ids(self) -> list[str]:
        candidates = [self._active_id, *self.settings.fallback_providers]
        seen: set[str] = set()
        result: list[str] = []
        for provider_id in candidates:
            if provider_id and provider_id not in seen:
                seen.add(provider_id)
                result.append(provider_id)
        return result

    def _availability(self, provider: BaseProvider) -> tuple[bool, str | None]:
        try:
            return provider.availability()
        except Exception as exc:
            logger.warning(
                "provider %s availability check failed: %s",
                provider.manifest.id,
                type(exc).__name__,
            )
            return False, "Availability check failed."

    def _can_attempt(
        self,
        provider: BaseProvider,
        *,
        active_privacy: str,
        now: float,
    ) -> tuple[bool, str | None]:
        del active_privacy  # the guard is based on the candidate, not only the source
        manifest = provider.manifest
        if not manifest.can_transcribe:
            return False, "does not implement transcription"
        if (
            manifest.privacy == "remote"
            and manifest.id != self._active_id
            and not self.settings.allow_remote_fallback
        ):
            return False, "remote fallback blocked by privacy guard"
        state = self._failures.get(manifest.id)
        if state and state.open_until > now:
            return False, "temporarily paused after a recent failure"
        available, reason = self._availability(provider)
        if not available:
            return False, reason or "not configured"
        return True, None

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionOutcome:
        outcome = TranscriptionOutcome()
        active = self.active
        active_privacy = active.manifest.privacy if active else "local"
        now = time.monotonic()
        attempted = False
        successful = False

        for provider_id in self._candidate_ids():
            provider = self.registry.get(provider_id)
            if provider is None:
                outcome.attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id, outcome="unavailable", detail="not installed"
                    )
                )
                continue
            allowed, reason = self._can_attempt(provider, active_privacy=active_privacy, now=now)
            if not allowed:
                outcome.attempts.append(
                    ProviderAttempt(provider_id=provider_id, outcome="skipped", detail=reason)
                )
                continue
            attempted = True
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    provider.transcribe(request),
                    timeout=request.timeout_seconds,
                )
            except TimeoutError:
                elapsed = int((time.monotonic() - started) * 1000)
                self._record_failure(provider_id, "timeout")
                outcome.attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        outcome="timeout",
                        detail="provider timed out",
                        elapsed_ms=elapsed,
                    )
                )
                logger.warning("provider %s timed out after %sms", provider_id, elapsed)
                continue
            except ProviderError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                self._record_failure(provider_id, exc.message)
                outcome.attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        outcome="failed",
                        detail=exc.message,
                        elapsed_ms=elapsed,
                    )
                )
                logger.warning("provider %s failed: %s", provider_id, exc.message)
                continue
            except Exception as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                self._record_failure(provider_id, type(exc).__name__)
                outcome.attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        outcome="failed",
                        detail="unexpected provider error",
                        elapsed_ms=elapsed,
                    )
                )
                logger.exception("unexpected provider failure in %s", provider_id)
                continue

            self._record_success(provider_id)
            outcome.text = result.text
            outcome.provider_id = result.provider_id
            outcome.model = result.model
            outcome.attempts.append(
                ProviderAttempt(
                    provider_id=provider_id,
                    outcome="success",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
            successful = True
            break

        primary = active
        primary_can = bool(primary and primary.manifest.can_transcribe)
        primary_available = self._availability(primary)[0] if primary else False
        primary_failed = any(
            attempt.provider_id == self._active_id and attempt.outcome in {"failed", "timeout"}
            for attempt in outcome.attempts
        )
        primary_skipped = any(
            attempt.provider_id == self._active_id
            and attempt.outcome == "skipped"
            and attempt.detail
            and "paused" in attempt.detail
            for attempt in outcome.attempts
        )
        outcome.degraded = bool(
            primary_failed
            or primary_skipped
            or (primary_can and not primary_available and not successful)
            or (attempted and not successful)
        )
        return outcome

    def _record_failure(self, provider_id: str, reason: str) -> None:
        state = self._failures.setdefault(provider_id, _FailureState())
        state.failures += 1
        state.last_reason = reason
        state.open_until = time.monotonic() + max(
            0.0, self.settings.provider_failure_cooldown_seconds
        )

    def _record_success(self, provider_id: str) -> None:
        self._failures[provider_id] = _FailureState()

    def capabilities(self) -> list[str]:
        provider = self.active
        return list(provider.manifest.capabilities) if provider else []

    def effective_model_label(self) -> str:
        provider = self.active
        if provider is None:
            return "offline"
        config = self.settings.provider_config(provider.manifest.id)
        if provider.manifest.id == "local-whisper":
            return f"faster-whisper/{config.get('model', 'small')}"
        model = config.get("model") or config.get("asr_model")
        return str(model or provider.manifest.id)

    async def aclose(self) -> None:
        for provider in self.registry.all():
            try:
                await provider.aclose()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                logger.warning(
                    "provider %s close failed: %s", provider.manifest.id, type(exc).__name__
                )

    def ensure_available(self, provider_id: str) -> BaseProvider:
        provider = self.registry.get(provider_id)
        if provider is None:
            raise ConflictError(f"Provider {provider_id!r} is not registered.")
        return provider
