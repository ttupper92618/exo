"""Setup actions separate observation from effects and preserve exact identities."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue, TypeAdapter

from skulk.api.operator_gateway import OperatorGatewayAuthorization
from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.setup_actions import (
    SetupAction,
    SetupActions,
    SetupMutation,
    SetupOperation,
)
from skulk.operator.pairing import PluginGrantUpdate


class SetupExtension(ManagedExtension):
    """Fixture owner holding observations independently of child enablement."""

    mode = "normal"
    requires_approval = False
    retained_requires_approval = False
    effects = 0

    def observation(self, node_id: str, operation_id: str) -> SetupOperation:
        """Return a safe observation, with deliberate boundary failures for tests."""
        if self.mode == "failure":
            raise RuntimeError("private-upstream-response")
        return SetupOperation(
            node_id="wrong" if self.mode == "node" else node_id,
            action_id="connect",
            operation_id="f" * 32 if self.mode == "operation" else operation_id,
            phase="running",
            requires_approval=self.retained_requires_approval,
        )

    async def node_setup_actions(self, node_id: str) -> SetupActions:
        """Read form metadata without changing the fixture's settings."""
        self.calls += 1
        action = SetupAction(
            action_id="connect",
            title="Connect service",
            description="Connect an existing service.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "writeOnly": self.mode == "secret",
                    }
                },
            },
            schema_digest="c" * 64,
            requires_approval=self.requires_approval,
        )
        return SetupActions(
            node_id=node_id,
            revision=0,
            schema_digest="a" * 64,
            credential_revision=1,
            credential_schema_digest="b" * 64,
            actions=(action, action) if self.mode == "duplicate" else (action,),
            operations=(self.observation(node_id, "d" * 32),),
        )

    async def start_node_setup(
        self, node_id: str, mutation: SetupMutation
    ) -> SetupOperation:
        """Count effect dispatch independently of observation."""
        self.effects += 1
        self.retained_requires_approval = mutation.expected_requires_approval
        self.calls += 1
        return self.observation(node_id, mutation.operation_id)

    async def node_setup_operation(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Read retained state independent of child readiness."""
        self.calls += 1
        return self.observation(node_id, operation_id)

    async def resume_node_setup(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Resume only the original operation ID."""
        self.effects += 1
        self.calls += 1
        return self.observation(node_id, operation_id)


def intent() -> SetupMutation:
    """Return a complete ordinary revision-fenced setup request."""
    return SetupMutation(
        operation_id="d" * 32,
        action_id="connect",
        expected_revision=0,
        expected_schema_digest="a" * 64,
        expected_credential_revision=1,
        expected_credential_schema_digest="b" * 64,
        expected_action_schema_digest="c" * 64,
        values={"host": "example.invalid"},
    )


async def test_setup_actions_enforce_scopes_revocation_and_cross_origin(
    tmp_path: Path,
) -> None:
    """A read grant never authorizes start/resume, including malformed mutations."""
    service, exchange = paired_service(tmp_path)
    extension = SetupExtension()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), service))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    base = "/v1/plugins/configurable/nodes/stable-node"
    operations = base + "/setup-operations"
    observation = operations + "/" + "d" * 32
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://localhost"
    ) as client:
        assert (
            await client.get(base + "/setup-actions", headers=bearer)
        ).status_code == 403
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:read",)),
        )
        for path in (base + "/setup-actions", observation):
            result = await client.get(path, headers=bearer)
            assert result.status_code == 200
            assert result.headers["Cache-Control"] == "no-store"
        before = extension.calls
        for path, body in (
            (operations, intent().model_dump(mode="json")),
            (observation + "/resume", {}),
        ):
            assert (
                await client.post(path, json=body, headers=bearer)
            ).status_code == 403
        invalid = await client.post(
            operations, json={"values": "private-secret"}, headers=bearer
        )
        assert invalid.status_code == 422 and "private-secret" not in invalid.text
        assert extension.calls == before
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(
                expected_revision=1, scopes=("plugins:read", "plugins:manage")
            ),
        )
        result = await client.post(
            operations, json=intent().model_dump(mode="json"), headers=bearer
        )
        assert result.status_code == 200 and result.json()["operationId"] == "d" * 32
        assert (
            await client.post(observation + "/resume", headers=bearer)
        ).status_code == 200
        before = extension.calls
        invalid_resume = await client.post(
            observation + "/resume", json={"values": "private-secret"}, headers=bearer
        )
        assert invalid_resume.status_code == 422
        assert "private-secret" not in invalid_resume.text
        for path in (operations, observation + "/resume"):
            result = await client.post(
                path,
                json={}
                if path.endswith("/resume")
                else intent().model_dump(mode="json"),
                headers={
                    "X-Skulk-Dashboard": "pairing-v1",
                    "Origin": "https://foreign.invalid",
                },
            )
            assert result.status_code == 403
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=2, scopes=())
        )
        assert (await client.get(observation, headers=bearer)).status_code == 403
        assert (
            await client.post(observation + "/resume", headers=bearer)
        ).status_code == 403
        assert extension.calls == before
        assert not extension.settings.enabled


@pytest.mark.parametrize(
    "mode", ["node", "operation", "failure", "secret", "duplicate"]
)
async def test_setup_responses_reject_mismatched_identity_and_secret_forms(
    mode: str,
) -> None:
    """Untrusted provider data cannot relabel operations or expose credential forms."""
    extension = SetupExtension()
    extension.mode = mode
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([extension]), None))
    headers = {"X-Skulk-Dashboard": "pairing-v1", "Origin": "http://localhost"}
    base = "/v1/plugins/configurable/nodes/stable-node"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
    ) as client:
        if mode in {"node", "failure", "secret", "duplicate"}:
            result = await client.get(base + "/setup-actions", headers=headers)
            assert result.status_code == (503 if mode == "failure" else 409)
            assert "private-upstream-response" not in result.text
        if mode in {"node", "operation", "failure"}:
            result = await client.get(
                base + "/setup-operations/" + "d" * 32, headers=headers
            )
            assert result.status_code == (503 if mode == "failure" else 409)
            assert "private-upstream-response" not in result.text
        before = extension.calls
        result = await client.post(
            base + "/setup-operations",
            json=intent().model_dump(mode="json") | {"executable": "/invalid"},
            headers=headers,
        )
        assert result.status_code == 422
        assert extension.calls == before


def test_setup_routes_are_documented_in_openapi() -> None:
    """All four operations have generated public contracts, including path ID bounds."""
    app = FastAPI()
    app.include_router(
        create_plugins_router(LoadedExtensions([SetupExtension()]), None)
    )
    schema = TypeAdapter(dict[str, JsonValue]).validate_python(app.openapi())
    paths = schema["paths"]
    assert isinstance(paths, dict)
    selected = {
        p: value
        for p, value in paths.items()
        if "setup-actions" in p or "setup-operations" in p
    }
    assert len(selected) == 4
    for route in selected.values():
        assert isinstance(route, dict)
        for operation in route.values():
            assert isinstance(operation, dict)
            assert (
                operation["summary"] and operation["description"] and operation["tags"]
            )


async def test_approval_setup_never_inherits_management_authority(
    tmp_path: Path,
) -> None:
    """Both ingress paths check declared and retained approval requirements."""
    service, exchange = paired_service(tmp_path)
    provider = SetupExtension()
    provider.requires_approval = True
    provider.retained_requires_approval = True
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([provider]), service))
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    route = "/v1/plugins/configurable/nodes/stable-node/setup-operations"
    mutation = intent().model_copy(update={"expected_requires_approval": True})
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(
            expected_revision=0, scopes=("plugins:read", "plugins:manage")
        ),
    )
    for application in (app, OperatorGatewayAuthorization(app, service)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="https://localhost",
            headers=bearer,
        ) as client:
            assert (
                await client.post(route, json=mutation.model_dump(mode="json"))
            ).status_code == 403
            assert (
                await client.post(route + "/" + "d" * 32 + "/resume")
            ).status_code == 403
            # Caller-supplied false is a stale expectation, never permission.
            assert (
                await client.post(route, json=intent().model_dump(mode="json"))
            ).status_code == 409
            provider.requires_approval = False
            assert (
                await client.post(route + "/" + "d" * 32 + "/resume")
            ).status_code == 403
            provider.requires_approval = True
    assert provider.effects == 0
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(
            expected_revision=1,
            scopes=("plugins:read", "plugins:manage", "plugins:approve"),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=OperatorGatewayAuthorization(app, service)),
        base_url="https://localhost",
        headers=bearer,
    ) as client:
        assert (
            await client.post(route, json=mutation.model_dump(mode="json"))
        ).status_code == 200
        assert (
            await client.post(route + "/" + "d" * 32 + "/resume")
        ).status_code == 200
        assert provider.effects == 2
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(
                expected_revision=2, scopes=("plugins:read", "plugins:manage")
            ),
        )
        assert (
            await client.post(route + "/" + "d" * 32 + "/resume")
        ).status_code == 403
        assert provider.effects == 2
