"""End-to-end API tests for text, image and audio inputs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest

from tests.helpers import PNG_1X1, make_png, synthetic_voice


def _assert_event_shape(payload: dict) -> None:
    assert set(payload) == {"event_id", "timestamp", "modality", "content", "metadata"}
    assert set(payload["metadata"]) == {"asr_text", "voiceprint_event", "model_capabilities"}
    assert payload["metadata"]["voiceprint_event"] in {"wakeup", "none"}
    assert UUID(payload["event_id"])
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))


def test_text_event_round_trip(client) -> None:
    text = "今天有一点累，想找人说说话。"
    response = client.post(
        "/v1/inputs/text",
        json={"text": text, "session_id": "sess_api_test", "source": "unit-test"},
    )
    assert response.status_code == 201
    payload = response.json()
    _assert_event_shape(payload)
    assert payload["modality"] == "text"
    assert payload["content"] == text
    assert payload["metadata"]["asr_text"] == ""
    assert payload["metadata"]["model_capabilities"] == ["text", "image", "audio"]
    assert response.headers["X-Dounai-Session"] == "sess_api_test"
    assert response.headers["X-Dounai-Degraded"] == "false"
    assert response.headers["Cache-Control"] == "no-store"

    stored = client.get(f"/v1/events/{payload['event_id']}")
    assert stored.status_code == 200
    assert stored.json() == payload


def test_text_validation(client) -> None:
    assert client.post("/v1/inputs/text", json={"text": "   "}).status_code == 422
    too_long = "x" * 20_001
    assert client.post("/v1/inputs/text", json={"text": too_long}).status_code == 422
    assert (
        client.post("/v1/inputs/text", json={"text": "hello", "source": "bad source!"}).status_code
        == 422
    )
    assert client.post("/v1/inputs/text", json={"text": "hello", "unknown": 1}).status_code == 422
    null_byte = client.post("/v1/inputs/text", json={"text": "a\u0000b"})
    assert null_byte.status_code == 422


def test_image_upload_and_media_access(client) -> None:
    response = client.post(
        "/v1/inputs/image",
        files={"file": ("pixel.png", PNG_1X1, "image/png")},
        data={"session_id": "sess_image", "source": "camera"},
    )
    assert response.status_code == 201
    payload = response.json()
    _assert_event_shape(payload)
    assert payload["modality"] == "image"
    assert payload["content"].startswith("http://testserver/v1/media/")
    media = client.get(payload["content"])
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"
    assert media.headers["cache-control"] == "private, no-store"
    assert media.content == PNG_1X1


def test_image_rejects_content_that_is_not_an_image(client) -> None:
    response = client.post(
        "/v1/inputs/image",
        files={"file": ("not-an-image.jpg", b"<html>hello</html>", "image/jpeg")},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media"


def test_image_size_limit_is_enforced(settings) -> None:
    from fastapi.testclient import TestClient

    from dounai_input.api import build_app

    settings = settings.__class__(
        **{**settings.__dict__, "max_image_bytes": 4096, "max_audio_bytes": 4096}
    )
    with TestClient(build_app(settings)) as client:
        response = client.post(
            "/v1/inputs/image",
            files={"file": ("large.png", make_png(padding=5000), "image/png")},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_audio_without_model_still_creates_event(client) -> None:
    response = client.post(
        "/v1/inputs/audio",
        files={"file": ("voice.wav", synthetic_voice(), "audio/wav")},
        data={"session_id": "sess_audio", "language": "zh"},
    )
    assert response.status_code == 201
    payload = response.json()
    _assert_event_shape(payload)
    assert payload["modality"] == "audio"
    assert payload["metadata"]["asr_text"] == ""
    assert payload["metadata"]["voiceprint_event"] == "none"
    media = client.get(payload["content"])
    assert media.status_code == 200
    assert media.headers["content-type"] == "audio/wav"


def test_audio_rejects_invalid_media(client) -> None:
    response = client.post(
        "/v1/inputs/audio",
        files={"file": ("voice.wav", b"not a wave file at all", "audio/wav")},
    )
    assert response.status_code == 415


def test_audio_rejects_invalid_language(client) -> None:
    response = client.post(
        "/v1/inputs/audio",
        files={"file": ("voice.wav", synthetic_voice(), "audio/wav")},
        data={"language": "../../etc"},
    )
    assert response.status_code == 422


def test_unknown_event_is_404(client) -> None:
    assert client.get("/v1/events/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/v1/media/../../etc/passwd").status_code == 404


def test_event_media_response_has_security_headers(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_request_validation_uses_stable_error_shape(client) -> None:
    response = client.post("/v1/inputs/text", json={})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert isinstance(error["fields"], list)


@pytest.mark.parametrize("path", ["/v1/providers", "/v1/privacy/status", "/v1/models"])
def test_read_only_endpoints_available(client, path: str) -> None:
    assert client.get(path).status_code == 200
