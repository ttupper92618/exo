"""Plugin configuration crosses an explicit owner boundary and preserves identity."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.extensions import (
    ConfigurableNode,
    ConfigurationMutation,
    ConfigurationResult,
    LoadedExtensions,
    NodeConfiguration,
)
from skulk.operator.pairing import PluginGrantUpdate

_HEADERS = {"X-Skulk-Dashboard": "pairing-v1", "Origin": "http://localhost"}


class ManagedExtension:
    """Inert management fixture; all lifecycle effects remain outside Skulk."""

    name = "configurable"
    skulk_requires = ">=1"

    def __init__(self) -> None:
        self.settings = NodeConfiguration(
            node_id="stable-node",
            revision=0,
            schema_digest="a" * 64,
            configuration_schema={"type": "object"},
            values={},
            enabled=False,
        )
        self.fail = False
        self.calls = 0

    def chat_middleware(self) -> None:
        """Management does not participate in inference hooks."""
        return None

    async def configuration_nodes(self) -> tuple[ConfigurableNode, ...]:
        """A disabled child remains configurable."""
        if self.fail:
            raise RuntimeError("sensitive provider exception")
        return (
            ConfigurableNode(
                node_id="stable-node",
                bundle_id="bundle",
                version="1",
                status="disabled",
                configurable=True,
            ),
        )

    async def node_configuration(self, node_id: str) -> NodeConfiguration:
        """Read only this installed node."""
        if node_id != self.settings.node_id:
            raise LookupError("sensitive provider exception")
        return self.settings

    async def configure_node(
        self, node_id: str, mutation: ConfigurationMutation
    ) -> ConfigurationResult:
        """Enforce both revision fences before changing ordinary settings."""
        prior = await self.node_configuration(node_id)
        if (
            prior.revision != mutation.expected_revision
            or prior.schema_digest != mutation.expected_schema_digest
        ):
            raise ValueError("sensitive provider exception")
        self.calls += 1
        if mutation.operation == "edit":
            self.settings = prior.model_copy(
                update={
                    "revision": prior.revision + 1,
                    "values": mutation.values,
                }
            )
        return ConfigurationResult(configuration=self.settings, validated=True)


@pytest.mark.asyncio
async def test_configuration_requires_explicit_scope_and_revision(
    tmp_path: Path,
) -> None:
    """Broad paired-device operation grants cannot read or edit plugin settings."""
    service, exchange = paired_service(tmp_path)
    extension = ManagedExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), service))
    path = "/v1/plugins/configurable/nodes/stable-node/configuration"
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as client:
        assert (await client.get(path, headers=bearer)).status_code == 403
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(
                expected_revision=0,
                scopes=("plugins:read",),
            ),
        )
        result = await client.get(path, headers=bearer)
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"
        body: dict[str, JsonValue] = {
            "operation": "edit",
            "expectedRevision": 0,
            "expectedSchemaDigest": "a" * 64,
            "values": {"limit": 3},
        }
        assert (await client.post(path, json=body, headers=bearer)).status_code == 403
        assert extension.calls == 0
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(
                expected_revision=1,
                scopes=("plugins:read", "plugins:manage"),
            ),
        )
        assert (await client.post(path, json=body, headers=bearer)).status_code == 200
        stale = await client.post(path, json=body, headers=bearer)
        assert stale.status_code == 409
        assert "sensitive" not in stale.text
        assert extension.calls == 1
        body["operation"] = "approve"
        assert (await client.post(path, json=body, headers=bearer)).status_code == 422
        assert extension.calls == 1


@pytest.mark.asyncio
async def test_owner_reads_disabled_nodes_but_never_password_values() -> None:
    """Secret schemas and failed providers return sanitized errors, not payloads."""
    extension = ManagedExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), None))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    path = "/v1/plugins/configurable/nodes/stable-node/configuration"
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost"
    ) as client:
        assert (await client.get(path)).status_code == 403
        assert (
            await client.get(
                path, headers={**_HEADERS, "Origin": "https://foreign.example"}
            )
        ).status_code == 403
        inventory = await client.get("/v1/plugins", headers=_HEADERS)
        assert inventory.json()[0]["nodes"][0]["status"] == "disabled"
        extension.fail = True
        failed = await client.get("/v1/plugins", headers=_HEADERS)
        assert failed.json() == [
            {"pluginId": "configurable", "nodes": [], "available": False}
        ]
        extension.settings = extension.settings.model_copy(
            update={
                "configuration_schema": {
                    "type": "object",
                    "properties": {"token": {"type": "string", "writeOnly": True}},
                },
                "values": {"token": "never-disclose"},
            }
        )
        refused = await client.get(path, headers=_HEADERS)
        assert refused.status_code == 409
        assert "never-disclose" not in refused.text
        assert (
            await client.get(
                path.replace("stable-node", "foreign-node"), headers=_HEADERS
            )
        ).status_code == 404


def test_configuration_facet_survives_builtin_composition_and_refuses_duplicates() -> (
    None
):
    """Identity collisions never silently route a management request to a sibling."""
    extension = ManagedExtension()
    loaded = LoadedExtensions([extension])
    assert loaded.with_builtin_extensions([]).configuration_providers == {
        extension.name: extension
    }
    assert (
        LoadedExtensions(
            [extension, ManagedExtension(), ManagedExtension()]
        ).configuration_providers
        == {}
    )
    assert (
        loaded.with_builtin_extensions([ManagedExtension()]).configuration_providers
        == {}
    )
