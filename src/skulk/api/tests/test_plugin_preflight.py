"""Preflight is bounded, explicitly authorized and independent of capability readiness."""

from pathlib import Path

import httpx
from fastapi import FastAPI

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.preflight import NodePreflight, PreflightCheck
from skulk.operator.pairing import PluginGrantUpdate


class PreflightExtension(ManagedExtension):
    """Inert disabled-node checks with injected malformed or failing responses."""

    duplicate = False

    async def node_preflight(self, node_id: str) -> NodePreflight:
        """Return only prerequisite metadata; never enable or acquire capacity."""
        self.calls += 1
        if self.fail:
            raise RuntimeError("protected-host-detail")
        check = PreflightCheck(
            code="credentials",
            passed=False,
            corrective_action="Supply the required credential.",
        )
        return NodePreflight(
            node_id=node_id,
            revision=0,
            schema_digest="a" * 64,
            values_digest="b" * 64,
            observed_at=1_800_000_000,
            checks=(check, check) if self.duplicate else (check,),
        )


async def test_preflight_scope_revocation_sanitization_and_no_enable(
    tmp_path: Path,
) -> None:
    """Broad scope cannot inspect setup and provider failures never disclose internals."""
    service, exchange = paired_service(tmp_path)
    extension = PreflightExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), service))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    path = "/v1/plugins/configurable/nodes/stable-node/preflight"
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
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"
        assert (
            result.json()["checks"][0]["correctiveAction"]
            == "Supply the required credential."
        )
        assert not extension.settings.enabled
        extension.fail = True
        failed = await client.get(path, headers=bearer)
        assert failed.status_code == 503 and "protected-host-detail" not in failed.text
        extension.fail = False
        extension.duplicate = True
        assert (await client.get(path, headers=bearer)).status_code == 409
        calls = extension.calls
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=1, scopes=())
        )
        assert (await client.get(path, headers=bearer)).status_code == 403
        assert extension.calls == calls
