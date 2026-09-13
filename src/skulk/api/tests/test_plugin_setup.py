"""Public setup export enforces plugin grants and never invokes an effect."""

from pathlib import Path

import httpx
from fastapi import FastAPI

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.setup import NodeSetup, SetupArtifact
from skulk.operator.pairing import PluginGrantUpdate


class SetupExtension(ManagedExtension):
    """Disabled-node public export with injected invalid response modes."""

    mode = "normal"

    async def node_setup(self, node_id: str) -> NodeSetup:
        """Return only synthetic public text; no key generation or enablement."""
        self.calls += 1
        if self.mode == "failure":
            raise RuntimeError("private-host-detail")
        artifact = SetupArtifact(
            name="owner.json",
            title="Owner identity",
            media_type="application/json",
            content="{}",
        )
        return NodeSetup(
            node_id="other" if self.mode == "wrong_node" else node_id,
            revision=0,
            schema_digest="a" * 64,
            credential_revision=1,
            artifacts=(artifact, artifact) if self.mode == "duplicate" else (artifact,),
        )


async def test_setup_requires_current_grant_and_bounds_public_response(
    tmp_path: Path,
) -> None:
    """Missing/revoked grants cannot read; bad exports disclose no provider errors."""
    service, exchange = paired_service(tmp_path)
    extension = SetupExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), service))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    path = "/v1/plugins/configurable/nodes/stable-node/setup"
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as client:
        assert (await client.get(path, headers=bearer)).status_code == 403
        assert extension.calls == 0
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:read",)),
        )
        result = await client.get(path, headers=bearer)
        assert (
            result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        )
        assert result.json()["artifacts"][0]["content"] == "{}"
        assert not extension.settings.enabled
        for mode, status in (("duplicate", 409), ("wrong_node", 409), ("failure", 503)):
            extension.mode = mode
            result = await client.get(path, headers=bearer)
            assert (
                result.status_code == status
                and "private-host-detail" not in result.text
            )
        calls = extension.calls
        assert (
            await client.get(path, headers={"Origin": "https://foreign.invalid"})
        ).status_code == 403
        assert extension.calls == calls
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=1, scopes=())
        )
        assert (await client.get(path, headers=bearer)).status_code == 403
        assert extension.calls == calls
