# pyright: reportUnusedFunction=false
"""Tests for the authenticated relay-only canonical API boundary."""

import base64
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skulk.api.operator_gateway import (
    OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY,
    OperatorGatewayAuthorization,
)
from skulk.operator.authority import EncryptedAuthorityStore
from skulk.operator.key_provider import LocalFileAuthorityKeyProvider
from skulk.operator.pairing import (
    OperatorPairingService,
    PairingChallengeRequest,
    PairingExchangeRequest,
    PairingExchangeResponse,
    pairing_signature_message,
)


def _base64url(value: bytes) -> str:
    """Encode device proof material for the pairing wire contract."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def paired_service(tmp_path: Path) -> tuple[OperatorPairingService, PairingExchangeResponse]:
    """Return a pairing service and one fully scoped operator credential."""

    provider = LocalFileAuthorityKeyProvider(tmp_path / "authority-key.bin")
    store = EncryptedAuthorityStore(provider, tmp_path / "authority.sqlite3")
    service = OperatorPairingService(
        store,
        provider,
        now=lambda: datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc),
    )
    package = service.create_session(exchange_url="https://example.invalid")
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    challenge = service.create_challenge(
        PairingChallengeRequest(
            nonce=package.nonce,
            device_name="Phone",
            device_public_key=_base64url(public_key),
        )
    )
    signature = private_key.sign(
        pairing_signature_message(
            cluster_id=UUID(str(package.cluster_id)),
            nonce=package.nonce,
            challenge=challenge.challenge,
        )
    )
    exchange = service.exchange(
        PairingExchangeRequest(
            nonce=package.nonce,
            signature=_base64url(signature),
        )
    )
    return service, exchange


def test_remote_listener_reuses_canonical_routes_and_requires_scoped_bearer(
    tmp_path: Path,
) -> None:
    """Reads and mutations reach one app only after bearer validation."""

    service, exchange = paired_service(tmp_path)
    canonical = FastAPI()

    @canonical.get("/state")
    def state() -> dict[str, bool]:
        return {"canonical": True}

    @canonical.post("/place_instance")
    def place() -> dict[str, bool]:
        return {"placed": True}

    @canonical.post("/models/remote-code-approvals/card_test")
    def approve_model_code(request: Request) -> dict[str, bool]:
        return {
            "operator_authorized": request.scope.get(
                OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY
            )
            is True
        }

    client = TestClient(OperatorGatewayAuthorization(canonical, service))
    assert client.get("/state").status_code == 401
    assert client.get(
        "/state",
        headers={"Authorization": f"Bearer {exchange.access_token}"},
    ).json() == {"canonical": True}
    assert client.post(
        "/place_instance",
        headers={"Authorization": f"Bearer {exchange.access_token}"},
    ).json() == {"placed": True}
    assert client.post(
        "/models/remote-code-approvals/card_test",
        headers={"Authorization": f"Bearer {exchange.access_token}"},
    ).json() == {"operator_authorized": True}
    assert client.get(
        "/state",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401


def test_pairing_and_refresh_paths_remain_reachable_before_access_authentication(
    tmp_path: Path,
) -> None:
    """The relay boundary does not block its own pairing bootstrap routes."""

    service, _ = paired_service(tmp_path)
    canonical = FastAPI()

    @canonical.post("/v1/auth/pairing-sessions/challenge")
    def challenge() -> dict[str, bool]:
        return {"open": True}

    @canonical.post("/v1/auth/pairing-sessions/exchange")
    def exchange() -> dict[str, bool]:
        return {"open": True}

    @canonical.post("/v1/auth/token")
    def refresh() -> dict[str, bool]:
        return {"open": True}

    client = TestClient(OperatorGatewayAuthorization(canonical, service))
    assert client.post("/v1/auth/pairing-sessions/challenge").status_code == 200
    assert client.post("/v1/auth/pairing-sessions/exchange").status_code == 200
    assert client.post("/v1/auth/token").status_code == 200


def test_dashboard_invitation_management_is_never_relay_accessible(
    tmp_path: Path,
) -> None:
    """Even a fully scoped device cannot reach the local dashboard authority."""

    service, exchange = paired_service(tmp_path)
    canonical = FastAPI()

    @canonical.post("/v1/auth/pairing-invitations")
    def create_invitation() -> dict[str, bool]:
        return {"created": True}

    @canonical.get("/v1/auth/pairing-invitations")
    def list_invitations() -> dict[str, bool]:
        return {"listed": True}

    @canonical.delete("/v1/auth/pairing-invitations/{invitation_id}")
    def revoke_invitation(invitation_id: UUID) -> dict[str, str]:
        return {"revoked": str(invitation_id)}

    client = TestClient(OperatorGatewayAuthorization(canonical, service))
    headers = {"Authorization": f"Bearer {exchange.access_token}"}
    invitation_id = UUID("00000000-0000-4000-8000-000000000001")
    assert client.post("/v1/auth/pairing-invitations", headers=headers).status_code == 404
    assert client.get("/v1/auth/pairing-invitations", headers=headers).status_code == 404
    assert (
        client.delete(
            f"/v1/auth/pairing-invitations/{invitation_id}",
            headers=headers,
        ).status_code
        == 404
    )


def test_remote_websocket_requires_an_operator_bearer(tmp_path: Path) -> None:
    """Canonical WebSockets share the same access-token boundary."""

    service, exchange = paired_service(tmp_path)
    canonical = FastAPI()

    @canonical.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    client = TestClient(OperatorGatewayAuthorization(canonical, service))
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/v1/realtime"),
    ):
        pass
    with client.websocket_connect(
        "/v1/realtime",
        headers={"Authorization": f"Bearer {exchange.access_token}"},
    ) as websocket:
        assert websocket.receive_text() == "ready"
