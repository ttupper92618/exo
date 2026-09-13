"""Explicit plugin grants must not inherit ordinary paired-device authority."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skulk.api.operator_auth import create_operator_auth_router
from skulk.api.operator_gateway import OperatorGatewayAuthorization
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.operator.pairing import (
    OperatorScopeError,
    OperatorTokenRequest,
    PairingSessionStateError,
    PluginGrantUpdate,
)


def test_plugin_grants_require_owner_and_revoke_existing_access(tmp_path: Path) -> None:
    """An old access token gains only explicitly granted scopes and loses them now."""
    service, exchange = paired_service(tmp_path)
    token = exchange.access_token
    assert service.plugin_grants()[0].scopes == ()
    with pytest.raises(OperatorScopeError):
        service.validate_access_token(token, required_scopes=("plugins:manage",))
    grant = service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(expected_revision=0, scopes=("plugins:manage",)),
    )
    assert grant.revision == 1
    service.validate_access_token(token, required_scopes=("plugins:manage",))
    with pytest.raises(OperatorScopeError):
        service.validate_access_token(token, required_scopes=("plugins:approve",))
    with pytest.raises(PairingSessionStateError):
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:approve",)),
        )
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(expected_revision=1, scopes=()),
    )
    with pytest.raises(OperatorScopeError):
        service.validate_access_token(token, required_scopes=("plugins:manage",))
    refreshed = service.refresh(
        OperatorTokenRequest(
            device_id=exchange.device_id,
            refresh_token=exchange.refresh_token,
        )
    )
    assert "plugins:manage" not in refreshed.scopes
    with pytest.raises(OperatorScopeError):
        service.validate_access_token(
            refreshed.access_token, required_scopes=("plugins:manage",)
        )


def test_relay_plugin_management_cannot_approve_or_self_grant(tmp_path: Path) -> None:
    """Route-specific authority is enforced before the ordinary mutation fallback."""
    service, exchange = paired_service(tmp_path)
    app = FastAPI()
    app.include_router(create_operator_auth_router(service))

    @app.get("/v1/plugins")
    @app.post("/v1/plugins/example/preflight")
    @app.post("/v1/plugins/example/proposals/proposal/approve")
    @app.post("/v1/plugins/example/resources/resource/release")
    def accepted() -> dict[str, bool]:
        return {"accepted": True}

    assert accepted() == {"accepted": True}
    client = TestClient(OperatorGatewayAuthorization(app, service))
    headers = {"Authorization": f"Bearer {exchange.access_token}"}
    assert client.get("/v1/plugins", headers=headers).status_code == 403
    assert (
        client.post("/v1/plugins/example/preflight", headers=headers).status_code == 403
    )
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(
            expected_revision=0, scopes=("plugins:read", "plugins:manage")
        ),
    )
    assert client.get("/v1/plugins", headers=headers).status_code == 200
    assert (
        client.post("/v1/plugins/example/preflight", headers=headers).status_code == 200
    )
    assert (
        client.post(
            "/v1/plugins/example/proposals/proposal/approve", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/plugins/example/resources/resource/release", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/v1/auth/plugin-grants/{exchange.device_id}",
            headers=headers,
            json={"expectedRevision": 1, "scopes": ["plugins:approve"]},
        ).status_code
        == 404
    )
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(expected_revision=1, scopes=("plugins:approve",)),
    )
    assert (
        client.post(
            "/v1/plugins/example/proposals/proposal/approve", headers=headers
        ).status_code
        == 200
    )
    service.revoke_device(exchange.access_token, exchange.device_id)
    assert (
        client.post(
            "/v1/plugins/example/proposals/proposal/approve", headers=headers
        ).status_code
        == 401
    )


def test_direct_owner_grant_wire_contract_and_origin_boundary(tmp_path: Path) -> None:
    """A bearer cannot bypass the owner boundary even on the direct listener."""
    service, exchange = paired_service(tmp_path)
    app = FastAPI()
    app.include_router(create_operator_auth_router(service))
    client = TestClient(
        app, base_url="http://localhost:52415", client=("127.0.0.1", 51000)
    )
    path = f"/v1/auth/plugin-grants/{exchange.device_id}"
    payload = {"expectedRevision": 0, "scopes": ["plugins:read", "plugins:manage"]}
    assert client.put(path, json=payload).status_code == 403
    assert (
        client.put(
            path,
            json=payload,
            headers={
                "Authorization": f"Bearer {exchange.access_token}",
                "Origin": "https://untrusted.example",
                "X-Skulk-Dashboard": "pairing-v1",
            },
        ).status_code
        == 403
    )
    headers = {"Origin": "http://localhost:52415", "X-Skulk-Dashboard": "pairing-v1"}
    assert (
        client.put(
            path, json=payload, headers={**headers, "X-Forwarded-For": "127.0.0.1"}
        ).status_code
        == 403
    )
    paired_headers = {**headers, "Authorization": f"Bearer {exchange.access_token}"}
    assert client.put(path, json=payload, headers=paired_headers).status_code == 403
    assert client.get("/v1/auth/plugin-grants", headers=paired_headers).status_code == 403
    assert service.plugin_grants()[0].revision == 0
    response = client.put(path, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert response.headers["Cache-Control"] == "no-store"
    assert exchange.access_token not in response.text
    assert exchange.refresh_token not in response.text
    assert client.put(path, json=payload, headers=headers).status_code == 409
    assert (
        client.put(
            path,
            json={"expectedRevision": 1, "scopes": ["devices:manage"]},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.put(
            path,
            json={"expectedRevision": 1, "scopes": ["plugins:read", "plugins:read"]},
            headers=headers,
        ).status_code
        == 422
    )
