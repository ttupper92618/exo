"""Private owner callbacks cannot escape their current installed capability scope."""

import json
from dataclasses import replace

import pytest

from skulk.extensions import CapabilityDescriptor, CapabilityResult
from skulk.extensions.capabilities import descriptor_revision as revision_of
from skulk.extensions.managed_host import HostCallbacks, HostCapability
from skulk.extensions.tests.test_steward_tools import context
from skulk.shared.types.common import NodeId


def descriptor() -> CapabilityDescriptor:
    """Build one owned unary callback contract."""
    return CapabilityDescriptor(
        id="echo",
        version="1.0.0",
        title="Echo",
        description="Fixture",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "foreign-node",
        "foreign-capability",
        "revision",
        "withdrawn",
        "verb",
        "extra",
    ],
)
async def test_callback_invocation_repeats_exact_owned_admission(fault: str) -> None:
    """Rejected node, contract, revision or operation never reaches Fabric."""
    contract = descriptor()
    calls: list[str] = []

    async def call(
        node_id: NodeId,
        capability_id: str,
        version: str,
        descriptor_revision: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> CapabilityResult:
        assert node_id == context().node_id and timeout_seconds == 2.0
        assert version == contract.version and descriptor_revision == revision_of(
            contract
        )
        calls.append(capability_id)
        return CapabilityResult(call_id="fixture", ok=True, result=payload)

    callbacks = HostCallbacks(
        replace(context(), call_capability=call),
        lambda: () if fault == "withdrawn" else (HostCapability("owned", contract),),
    )
    payload: dict[str, object] = {
        "node_id": "foreign" if fault == "foreign-node" else "owned",
        "capability_id": "foreign" if fault == "foreign-capability" else contract.id,
        "version": contract.version,
        "descriptor_revision": "0" * 16
        if fault == "revision"
        else revision_of(contract),
        "payload": {"text": "hello"},
    }
    if fault == "extra":
        payload["command"] = "not a permitted callback"
    raw = json.dumps(
        {
            "request_id": 1,
            "operation": "approve" if fault == "verb" else "invoke",
            "payload": payload,
        }
    ).encode()
    if fault == "none":
        assert await callbacks.dispatch(raw) == {"text": "hello"}
        assert calls == ["echo"]
    else:
        with pytest.raises(ValueError):
            await callbacks.dispatch(raw)
        assert calls == []


async def test_callback_observations_read_live_policy_and_filter_visibility() -> None:
    """Neither a prior grant nor another installation's descriptor is exported."""
    owned = descriptor()
    foreign = owned.model_copy(update={"id": "foreign"})
    allowed = True
    visible = True

    async def describe(node_id: NodeId) -> tuple[CapabilityDescriptor, ...]:
        assert node_id == context().node_id
        return (owned, foreign) if visible else (foreign,)

    callbacks = HostCallbacks(
        replace(
            context(), steward_actions_allowed=lambda: allowed, describe_node=describe
        ),
        lambda: (HostCapability("owned", owned),),
    )
    actions = b'{"request_id":1,"operation":"actions","payload":{}}'
    revisions = b'{"request_id":2,"operation":"revisions","payload":{}}'
    assert await callbacks.dispatch(actions) is True
    allowed = False
    assert await callbacks.dispatch(actions) is False
    assert await callbacks.dispatch(revisions) == {
        owned.qualified_id: revision_of(owned)
    }
    visible = False
    assert await callbacks.dispatch(revisions) == {}
    with pytest.raises(ValueError):
        await callbacks.dispatch(
            b'{"request_id":3,"operation":"actions","payload":{"grant":true}}'
        )
