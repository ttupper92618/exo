"""Actual OS-assigned ports are observable without reconnecting a running host."""

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest
from skulk_pyo3_bindings import Keypair, NetworkingHandle, ZenohHandle

from skulk.routing.router import Router


@pytest.mark.parametrize("zenoh", [False, True])
async def test_native_listener_snapshot_reports_reachable_assigned_ports(
    monkeypatch: pytest.MonkeyPatch, zenoh: bool
) -> None:
    monkeypatch.setenv("SKULK_LIBP2P_NAMESPACE", uuid4().hex)
    identity = Keypair.generate()
    network = NetworkingHandle(identity, [], 0)
    data = ZenohHandle(["tcp/127.0.0.1:0"]) if zenoh else None
    router = Router(handle=network, zenoh=data, node_id=identity.to_node_id())

    async def receive() -> None:
        while True:
            await network.recv()

    receiving = asyncio.create_task(receive())
    try:
        async with asyncio.timeout(10):
            # Drive NewListenAddr on the real swarm before requesting a snapshot.
            while not await network.listen_addresses():
                await asyncio.sleep(0.01)
            before = await router.host_network("v0.0.2", "test-token")
            assert before.node_id == identity.to_node_id()
            assert before.data_transport == ("zenoh" if zenoh else "gossipsub")
            for endpoint in (*before.control, *before.data):
                _, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
                writer.close()
                await writer.wait_closed()
            after = await router.host_network("v0.0.2", "test-token")
            assert before == after
    finally:
        receiving.cancel()
        with suppress(asyncio.CancelledError):
            await receiving
