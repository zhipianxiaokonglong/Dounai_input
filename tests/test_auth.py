"""Authentication, CSRF/origin guard and secret hygiene."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dounai_input.api import build_app


def test_loopback_without_token_is_open(client) -> None:
    assert client.get("/v1/privacy/status").status_code == 200


def test_bearer_token_is_required_when_configured(settings) -> None:
    settings = replace(settings, api_token="test-token-value")
    with TestClient(build_app(settings)) as client:
        assert client.get("/v1/privacy/status").status_code == 401
        assert (
            client.get(
                "/v1/privacy/status",
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        authorized = client.get(
            "/v1/privacy/status",
            headers={"Authorization": "Bearer test-token-value"},
        )
        assert authorized.status_code == 200
        health = client.get("/healthz")
        assert health.status_code == 200
        assert "test-token-value" not in health.text


def test_media_capability_url_does_not_require_token(settings) -> None:
    settings = replace(settings, api_token="test-token-value")
    with TestClient(build_app(settings)) as client:
        upload = client.post(
            "/v1/inputs/image",
            files={"file": ("pixel.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")},
            headers={"Authorization": "Bearer test-token-value"},
        )
        assert upload.status_code == 201
        media_url = upload.json()["content"]
        # The opaque 128-bit id is intentionally a capability URL for <img>.
        assert client.get(media_url).status_code == 200


def test_cross_origin_writes_are_blocked(client) -> None:
    response = client.post(
        "/v1/inputs/text",
        json={"text": "csrf attempt"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"

    response = client.post(
        "/v1/inputs/text",
        json={"text": "cross site"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_same_origin_write_is_allowed(client) -> None:
    response = client.post(
        "/v1/inputs/text",
        json={"text": "same origin"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201


def test_non_browser_write_without_origin_is_allowed(client) -> None:
    response = client.post("/v1/inputs/text", json={"text": "curl style"})
    assert response.status_code == 201


def test_provider_endpoint_never_returns_api_keys(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-this-must-not-appear")
    response = client.get("/v1/providers")
    assert response.status_code == 200
    assert "sk-this-must-not-appear" not in response.text
    assert "DASHSCOPE_API_KEY" not in response.text


def test_oversized_content_length_is_rejected_before_parsing(client) -> None:
    response = client.post(
        "/v1/inputs/text",
        content=b"{}",
        headers={"Content-Length": "999999999", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
