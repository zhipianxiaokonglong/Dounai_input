"""The downstream event contract is frozen and schema-checked."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from dounai_input.events import EVENT_JSON_SCHEMA, EventMetadata, InputEvent


def test_event_has_exactly_the_contract_fields() -> None:
    event = InputEvent.create(
        modality="audio",
        content="http://testserver/v1/media/abc",
        capabilities=["audio", "text"],
        asr_text="你好",
        voiceprint_event="wakeup",
    )
    payload = event.contract_dict()
    assert set(payload) == {"event_id", "timestamp", "modality", "content", "metadata"}
    assert set(payload["metadata"]) == {"asr_text", "voiceprint_event", "model_capabilities"}
    assert payload["metadata"]["model_capabilities"] == ["text", "audio"]
    assert payload["metadata"]["voiceprint_event"] == "wakeup"
    assert UUID(str(payload["event_id"]))
    assert isinstance(payload["timestamp"], str)
    parsed = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_metadata_rejects_extension_fields() -> None:
    with pytest.raises(ValidationError):
        EventMetadata.model_validate(
            {
                "asr_text": "",
                "voiceprint_event": "none",
                "model_capabilities": [],
                "provider": "leak",
            }
        )


def test_event_rejects_missing_metadata() -> None:
    with pytest.raises(ValidationError):
        InputEvent.model_validate(
            {
                "event_id": "00000000-0000-0000-0000-000000000000",
                "timestamp": "2026-01-01T00:00:00Z",
                "modality": "text",
                "content": "hello",
            }
        )


def test_capabilities_are_canonical_and_deduplicated() -> None:
    metadata = EventMetadata(
        model_capabilities=["audio", "text", "audio", "image"]  # type: ignore[list-item]
    )
    assert metadata.model_capabilities == ["text", "image", "audio"]


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InputEvent.model_validate(
            {
                "event_id": "00000000-0000-0000-0000-000000000000",
                "timestamp": "2026-01-01T00:00:00",
                "modality": "text",
                "content": "hello",
                "metadata": {
                    "asr_text": "",
                    "voiceprint_event": "none",
                    "model_capabilities": [],
                },
            }
        )


def test_published_json_schema_matches_code() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "input-event.schema.json"
    published = json.loads(schema_path.read_text(encoding="utf-8"))
    assert published == EVENT_JSON_SCHEMA
