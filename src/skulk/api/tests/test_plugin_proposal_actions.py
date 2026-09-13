"""Owner proposal actions require explicit approval scope and trusted actor identity."""

from pathlib import Path
from typing import final

import httpx
from fastapi import FastAPI

from skulk.api.operator_gateway import OperatorGatewayAuthorization
from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.proposal_actions import ProposalApproval, ProposalOperation
from skulk.extensions.proposal_review import ProposalReference
from skulk.operator.pairing import PluginGrantUpdate


@final
class ActionProvider(ManagedExtension):
    """Keep effect evidence private and expose only durable safe operation status."""

    name = "managed.fixture"

    def __init__(self) -> None:
        super().__init__()
        self.actors: list[str] = []
        self.mutation = ProposalApproval(
            operation_id="1" * 32,
            review_revision="b" * 64,
            reference=ProposalReference(
                plugin_id=self.name,
                node_id="node",
                proposal_id="opaque-proposal",
                proposal_digest="a" * 64,
            ),
        )

    def observed(self) -> ProposalOperation:
        """Return progress without any signed proof or executable input."""
        return ProposalOperation(
            operation_id=self.mutation.operation_id,
            reference=self.mutation.reference,
            phase="accepted",
            updated_at=1800000000,
        )

    async def approve_node_proposal(
        self, mutation: ProposalApproval, operator_id: str
    ) -> ProposalOperation:
        """Record only the actor supplied by authorization, never request JSON."""
        assert mutation == self.mutation
        self.actors.append(operator_id)
        return self.observed()

    async def node_proposal_operation(
        self, node_id: str, operation_id: str
    ) -> ProposalOperation:
        """Observation leaves the effect counter untouched."""
        assert node_id == "node" and operation_id == self.mutation.operation_id
        return self.observed()

    async def resume_node_proposal(
        self, node_id: str, operation_id: str, operator_id: str
    ) -> ProposalOperation:
        """Explicit recovery receives the same independently authenticated actor."""
        self.actors.append(operator_id)
        return await self.node_proposal_operation(node_id, operation_id)


async def test_direct_and_relay_approval_scopes_actor_and_recovery(
    tmp_path: Path,
) -> None:
    """Manage cannot approve or resume; revoked callers cannot reach provider code."""
    service, exchange = paired_service(tmp_path)
    provider = ActionProvider()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([provider]), service))
    headers = {"Authorization": f"Bearer {exchange.access_token}"}
    base = "/v1/plugins/managed.fixture/nodes/node"
    approve = base + "/proposals/opaque-proposal/approve"
    observe = base + "/proposal-operations/" + provider.mutation.operation_id
    payload = provider.mutation.model_dump(mode="json", by_alias=True)
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
            headers=headers,
        ) as client:
            assert (await client.get(observe)).status_code == 200
            assert (await client.post(approve, json=payload)).status_code == 403
            assert (await client.post(observe + "/resume", json={})).status_code == 403
    assert provider.actors == []
    service.set_plugin_grant(
        exchange.device_id,
        PluginGrantUpdate(
            expected_revision=1, scopes=("plugins:read", "plugins:approve")
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=OperatorGatewayAuthorization(app, service)),
        base_url="https://localhost",
        headers=headers,
    ) as client:
        response = await client.post(approve, json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert provider.actors == [str(exchange.device_id)]
        assert "signature" not in response.text and "credential" not in response.text
        assert (
            await client.post(approve, json=payload | {"operatorId": "spoofed"})
        ).status_code == 422
        assert (await client.post(observe + "/resume", json={})).status_code == 200
        assert provider.actors == [str(exchange.device_id)] * 2
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=2, scopes=())
        )
        assert (await client.post(approve, json=payload)).status_code == 403
        assert (await client.post(observe + "/resume", json={})).status_code == 403
        assert len(provider.actors) == 2
