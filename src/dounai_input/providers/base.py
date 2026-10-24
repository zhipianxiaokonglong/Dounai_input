"""Provider adapter contract.

A provider hides one vendor's wire protocol behind a deliberately small interface:
check availability, declare capabilities, and transcribe audio.  Capabilities are
declarations, not guesses: the gateway never sends an image to an adapter that
does not advertise image support.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ProviderError

CAPABILITIES = ("text", "image", "audio")


@dataclass(frozen=True)
class ProviderManifest:
    id: str
    name: str
    version: str
    description: str
    capabilities: tuple[str, ...]
    privacy: str  # "local" or "remote"
    requires_api_key: bool = False
    can_transcribe: bool = False
    homepage: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.capabilities) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"Unknown capabilities: {sorted(unknown)}")
        if self.privacy not in {"local", "remote"}:
            raise ValueError("privacy must be 'local' or 'remote'")
        object.__setattr__(
            self,
            "capabilities",
            tuple(item for item in CAPABILITIES if item in set(self.capabilities)),
        )


@dataclass(frozen=True)
class TranscriptionRequest:
    path: Path
    mime: str
    language: str | None = None
    context: tuple[str, ...] = ()
    timeout_seconds: float = 60.0

    def safe_context(self, max_chars: int = 400) -> str:
        text = " ".join(item.strip() for item in self.context if item.strip())
        return text[-max_chars:]


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    provider_id: str
    model: str | None = None
    language: str | None = None


@dataclass
class ProviderAttempt:
    provider_id: str
    outcome: str  # success | unavailable | skipped | failed | timeout
    detail: str | None = None
    elapsed_ms: int = 0


@dataclass
class TranscriptionOutcome:
    text: str = ""
    provider_id: str | None = None
    model: str | None = None
    attempts: list[ProviderAttempt] = field(default_factory=list)
    degraded: bool = False


class BaseProvider(abc.ABC):
    """Base class for built-in and third-party provider plugins.

    Plugin authors subclass this, set :attr:`manifest` and implement
    :meth:`transcribe` when ``can_transcribe`` is true.  Constructors must accept
    a single mapping so the registry can instantiate plugins uniformly.
    """

    manifest: ProviderManifest

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})

    @abc.abstractmethod
    def availability(self) -> tuple[bool, str | None]:
        """Return availability and a safe, actionable reason when unavailable."""

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        raise ProviderError(
            f"{self.manifest.name} does not implement audio transcription.",
            provider_id=self.manifest.id,
            retryable=False,
        )

    async def aclose(self) -> None:  # noqa: B027 - optional lifecycle hook
        """Release network clients or model handles.  Default is a no-op."""

    def info(self) -> dict[str, Any]:
        available, reason = self.availability()
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "version": self.manifest.version,
            "description": self.manifest.description,
            "capabilities": list(self.manifest.capabilities),
            "privacy": self.manifest.privacy,
            "requires_api_key": self.manifest.requires_api_key,
            "can_transcribe": self.manifest.can_transcribe,
            "available": available,
            "unavailable_reason": reason,
            "homepage": self.manifest.homepage,
        }


def unique_context(items: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = " ".join(item.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)
