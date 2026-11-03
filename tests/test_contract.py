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
    assert set(payload["metadata"]) == {
        "asr_text",
        "asr_status",
        "voiceprint_event",
        "model_capabilities",
    }
    assert payload["metadata"]["model_capabilities"] == ["text", "audio"]
    assert payload["metadata"]["voiceprint_event"] == "wakeup"
    assert payload["metadata"]["asr_status"] == "not_applicable"
    assert UUID(str(payload["event_id"]))
    assert isinstance(payload["timestamp"], str)
    parsed = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_asr_status_defaults_and_explicit_values() -> None:
    event = InputEvent.create(modality="audio", content="http://testserver/v1/media/x")
    assert event.metadata.asr_status == "not_applicable"
    completed = InputEvent.create(
        modality="audio",
        content="http://testserver/v1/media/x",
        asr_text="你好",
        asr_status="completed",
    )
    assert completed.metadata.asr_status == "completed"


def test_v10_event_without_asr_status_still_parses() -> None:
    payload = {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "timestamp": "2026-01-01T00:00:00Z",
        "modality": "text",
        "content": "hello",
        "metadata": {
            "asr_text": "",
            "voiceprint_event": "none",
            "model_capabilities": [],
        },
    }
    event = InputEvent.model_validate(payload)
    assert event.metadata.asr_status == "not_applicable"


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
                    "asr_status": "not_applicable",
                    "voiceprint_event": "none",
                    "model_capabilities": [],
                },
            }
        )


def test_published_json_schema_matches_code() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "input-event.schema.json"
    published = json.loads(schema_path.read_text(encoding="utf-8"))
    assert published == EVENT_JSON_SCHEMA
    assert published["x-contract-version"] == "1.1"
    required = published["properties"]["metadata"]["required"]
    assert "asr_status" not in required  # v1.1 field stays optional for old readers
    assert "asr_status" in published["properties"]["metadata"]["properties"]
