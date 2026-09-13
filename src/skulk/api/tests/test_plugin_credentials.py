"""Write-only node credentials retain the explicit plugin authorization boundary."""

from pathlib import Path

import httpx
from fastapi import FastAPI

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.credentials import (
    CredentialMutation,
    CredentialStatus,
    NodeCredentials,
)
from skulk.operator.pairing import PluginGrantUpdate


class CredentialExtension(ManagedExtension):
    """Return metadata and deliberately unsafe provider exceptions for redaction checks."""

    async def node_credentials(self, node_id: str) -> NodeCredentials:
        """Read one installed node while ordinary capability readiness is false."""
        await self.node_configuration(node_id)
        return NodeCredentials(
            node_id=node_id,
            revision=0,
            schema_digest="b" * 64,
            credentials=(
                CredentialStatus(
                    credential_id="provider",
                    title="Provider token",
                    description="External credential",
                    required=True,
                    ready=False,
                ),
            ),
        )

    async def change_node_credential(
        self, node_id: str, mutation: CredentialMutation
    ) -> NodeCredentials:
        """Count dispatch and keep any exception text outside the public response."""
        self.calls += 1
        if self.fail:
            raise RuntimeError(
                mutation.value.get_secret_value() if mutation.value else "fixture"
            )
        return await self.node_credentials(node_id)


async def test_credential_scope_revocation_and_secret_redaction(tmp_path: Path) -> None:
    """Broad grants, malformed input and provider exceptions cannot expose a value."""
    service, exchange = paired_service(tmp_path)
    extension = CredentialExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), service))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    path = "/v1/plugins/configurable/nodes/stable-node/credentials"
    body = {
        "operation": "replace",
        "operationId": "d" * 32,
        "credentialId": "provider",
        "expectedRevision": 0,
        "expectedSchemaDigest": "b" * 64,
        "value": "private-credential-fixture",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as client:
        assert (await client.get(path, headers=bearer)).status_code == 403
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:read",)),
        )
        assert (await client.get(path, headers=bearer)).status_code == 200
        assert (await client.post(path, headers=bearer, json=body)).status_code == 403
        assert extension.calls == 0
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(
                expected_revision=1, scopes=("plugins:read", "plugins:manage")
            ),
        )
        result = await client.post(path, headers=bearer, json=body)
        assert (
            result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        )
        assert "private-credential-fixture" not in result.text
        malformed = await client.post(
            path, headers=bearer, json={**body, "value": {"secret": body["value"]}}
        )
        assert (
            malformed.status_code == 422
            and "private-credential-fixture" not in malformed.text
        )
        invalid_retirement = await client.post(
            path, headers=bearer, json={**body, "operation": "retire"}
        )
        assert invalid_retirement.status_code == 422
        extension.fail = True
        failed = await client.post(path, headers=bearer, json=body)
        assert (
            failed.status_code == 503
            and "private-credential-fixture" not in failed.text
        )
        calls = extension.calls
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=2, scopes=())
        )
        assert (await client.post(path, headers=bearer, json=body)).status_code == 403
        assert extension.calls == calls
        crossed = await client.post(
            path,
            headers={
                "Origin": "https://untrusted.example",
                "X-Skulk-Dashboard": "pairing-v1",
            },
            json=body,
        )
        assert crossed.status_code == 403 and extension.calls == calls
