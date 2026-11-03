"""The public event contract.

This module is intentionally tiny and dependency-light.  Downstream consumers
depend on the *shape* of :class:`InputEvent`; do not add metadata fields without a
versioned contract change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

Modality = Literal["text", "image", "audio"]
Capability = Literal["text", "image", "audio"]
VoiceprintEvent = Literal["wakeup", "none"]
AsrStatus = Literal["completed", "empty", "unavailable", "not_applicable"]

CONTRACT_VERSION = "1.1"
_CAPABILITY_ORDER: tuple[str, ...] = ("text", "image", "audio")


class EventMetadata(BaseModel):
    """Metadata defined by the downstream contract.

    ``asr_status`` was added in contract v1.1.  It is optional at the schema level
    so consumers written against v1.0 keep working, while the producer always fills
    it.  Downstream must not treat an empty ``asr_text`` as usable text unless
    ``asr_status == "completed"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    asr_text: str = ""
    voiceprint_event: VoiceprintEvent = "none"
    model_capabilities: list[Capability] = Field(default_factory=list)
    asr_status: AsrStatus = "not_applicable"

    @field_validator("model_capabilities")
    @classmethod
    def _canonical_capabilities(cls, values: list[str]) -> list[str]:
        deduped = set(values)
        return [item for item in _CAPABILITY_ORDER if item in deduped]


class InputEvent(BaseModel):
    """Normalized input event consumed by the downstream processing module."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    timestamp: datetime
    modality: Modality
    content: str = Field(min_length=1)
    metadata: EventMetadata

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def create(
        cls,
        *,
        modality: Modality,
        content: str,
        capabilities: tuple[str, ...] | list[str] = (),
        asr_text: str = "",
        asr_status: AsrStatus = "not_applicable",
        voiceprint_event: VoiceprintEvent = "none",
    ) -> InputEvent:
        return cls(
            event_id=uuid4(),
            timestamp=datetime.now(UTC),
            modality=modality,
            content=content,
            metadata=EventMetadata(
                asr_text=asr_text,
                asr_status=asr_status,
                voiceprint_event=voiceprint_event,
                model_capabilities=list(capabilities),  # type: ignore[arg-type]
            ),
        )

    def contract_dict(self) -> dict[str, object]:
        """Serialize with exactly the fields promised by the contract."""
        return self.model_dump(mode="json")


EVENT_JSON_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/zhipianxiaokonglong/Dounai_input/schemas/input-event.schema.json",
    "title": "DounaiInputEvent",
    "x-contract-version": CONTRACT_VERSION,
    "type": "object",
    "additionalProperties": False,
    "required": ["event_id", "timestamp", "modality", "content", "metadata"],
    "properties": {
        "event_id": {"type": "string", "format": "uuid"},
        "timestamp": {"type": "string", "format": "date-time"},
        "modality": {"enum": ["text", "image", "audio"]},
        "content": {"type": "string", "minLength": 1},
        "metadata": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asr_text", "voiceprint_event", "model_capabilities"],
            "properties": {
                "asr_text": {"type": "string"},
                # Optional at the schema level for v1.0 consumers; the producer
                # always fills it.  Added in contract v1.1.
                "asr_status": {
                    "enum": ["completed", "empty", "unavailable", "not_applicable"],
                    "default": "not_applicable",
                    "description": (
                        "completed: non-empty transcript; empty: ASR ran but heard "
                        "nothing; unavailable: no ASR provider or failure; "
                        "not_applicable: text/image input."
                    ),
                },
                "voiceprint_event": {"enum": ["wakeup", "none"]},
                "model_capabilities": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"enum": ["text", "image", "audio"]},
                },
            },
        },
    },
}
