"""Authorization, origin rejection and safe errors for local attachment facts."""

from pathlib import Path

import httpx
from fastapi import FastAPI

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.extensions.host_network import (
    HostNetwork,
    TcpEndpoint,
    namespace_fingerprint,
)
from skulk.extensions.loader import LoadedExtensions
from skulk.operator.pairing import PluginGrantUpdate


async def test_network_observation_requires_scope_and_never_discloses_errors(
    tmp_path: Path,
) -> None:
    pairing, exchange = paired_service(tmp_path / "pairing")
    observations = 0
    fail = False

    async def observe() -> HostNetwork:
        nonlocal observations
        observations += 1
        if fail:
            raise ValueError("private-routing-secret")
        return HostNetwork(
            node_id="peer",
            network_version="v0.0.2",
            namespace_fingerprint=namespace_fingerprint("private-routing-secret"),
            control=(TcpEndpoint(host="127.0.0.1", port=49123),),
            data_transport="gossipsub",
            data=(),
        )

    app = FastAPI()
    app.include_router(
        create_plugins_router(LoadedExtensions([]), pairing, host_network=observe)
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 52000))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    owner = {"X-Skulk-Dashboard": "pairing-v1", "Origin": "https://localhost"}
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as client:
        path = "/v1/plugins/host-network"
        assert (await client.get(path, headers=bearer)).status_code == 403
        assert (
            await client.get(
                path, headers={**owner, "Origin": "https://foreign.example"}
            )
        ).status_code == 403
        assert observations == 0
        pairing.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:read",)),
        )
        response = await client.get(path, headers=bearer)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "private-routing-secret" not in response.text
        assert (
            HostNetwork.model_validate_json(response.content).control[0].port == 49123
        )
        pairing.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=1, scopes=())
        )
        assert (await client.get(path, headers=bearer)).status_code == 403
        assert observations == 1
        fail = True
        response = await client.get(path, headers=owner)
        assert response.status_code == 503
        assert "private-routing-secret" not in response.text
