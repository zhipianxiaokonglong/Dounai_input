"""Cross-repository error and version contract: retryable, request id, versions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import dounai_input
from dounai_input.api import build_app
from dounai_input.api_access import ApiDiscoveryError
from dounai_input.events import CONTRACT_VERSION

# --------------------------------------------------------------------------- versions


@pytest.mark.parametrize("path", ["/healthz", "/v1/contract", "/v1/providers"])
def test_every_response_carries_both_versions(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["x-dounai-contract-version"] == CONTRACT_VERSION
    assert response.headers["x-dounai-module-version"] == dounai_input.__version__


def test_error_responses_carry_versions_and_request_id(client) -> None:
    response = client.post("/v1/inputs/text", json={})
    assert response.status_code == 422
    assert response.headers["x-dounai-contract-version"] == CONTRACT_VERSION
    body = response.json()["error"]
    assert body["request_id"] == response.headers["x-request-id"]


def test_contract_endpoint_keeps_schema_shape(client) -> None:
    from dounai_input.events import EVENT_JSON_SCHEMA

    published = client.get("/v1/contract").json()
    assert published == EVENT_JSON_SCHEMA
    assert published["x-contract-version"] == CONTRACT_VERSION


# --------------------------------------------------------------------------- error envelope


def test_validation_error_is_not_retryable_but_keeps_fields(client) -> None:
    response = client.post("/v1/inputs/text", json={})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["retryable"] is False
    assert isinstance(error["fields"], list)


def test_csrf_rejection_is_not_retryable(client) -> None:
    response = client.post(
        "/v1/inputs/text",
        json={"text": "csrf"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "forbidden_origin"
    assert error["retryable"] is False
    assert error["request_id"] == response.headers["x-request-id"]


def test_missing_token_is_not_retryable(settings) -> None:
    protected = replace(settings, api_token="test-token-value")
    with TestClient(build_app(protected)) as client:
        response = client.get("/v1/providers")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["retryable"] is False
    assert error["request_id"]


def test_provider_discovery_failure_is_retryable(client, app, monkeypatch) -> None:
    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise ApiDiscoveryError("The provider did not respond.")

    monkeypatch.setattr(app.state.services["api_connections"], "discover", boom)
    response = client.post(
        "/v1/api-connections/discover",
        json={
            "base_url": "https://93.184.216.34/v1",
            "api_key": "sk-test",
        },
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "api_discovery_failed"
    assert error["retryable"] is True


def test_unexpected_failure_is_retryable_and_hides_details(app, monkeypatch) -> None:
    def explode() -> list[dict[str, Any]]:
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr(app.state.services["providers"], "list_providers", explode)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/providers")
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["retryable"] is True
    assert "secret stack detail" not in response.text
