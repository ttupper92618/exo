"""Safe listener projection and namespace non-disclosure at the plugin boundary."""

import hashlib

import pytest
from pydantic import ValidationError

from skulk.extensions.host_network import (
    HostNetwork,
    namespace_fingerprint,
    tcp_endpoints,
)


def test_bound_tcp_projection_deduplicates_and_preserves_actual_ports() -> None:
    control = tcp_endpoints(
        ("/ip4/0.0.0.0/tcp/49123", "/ip4/127.0.0.1/tcp/49123", "/ip4/127.0.0.1/udp/9"),
        multiaddr=True,
    )
    assert [(item.host, item.port) for item in control] == [("127.0.0.1", 49123)]
    data = tcp_endpoints(("tcp/[::1]:49124",), multiaddr=False)
    assert [(item.host, item.port) for item in data] == [("::1", 49124)]


@pytest.mark.parametrize(
    "address",
    [
        "tcp/localhost:1234",
        "tcp/127.0.0.1:0",
        "tcp/user@127.0.0.1:1234",
        "tcp/127.0.0.1:1234?secret=yes",
    ],
)
def test_unusable_or_nonliteral_locator_is_refused(address: str) -> None:
    with pytest.raises(ValueError):
        tcp_endpoints((address,), multiaddr=False)


def test_fingerprint_cannot_be_used_as_zenoh_routing_digest() -> None:
    token = "v0.0.2\0private-fabric-token"
    assert namespace_fingerprint(token) != hashlib.sha256(token.encode()).hexdigest()
    assert namespace_fingerprint(token) != namespace_fingerprint("v0.0.2")
    with pytest.raises(ValidationError):
        HostNetwork(
            node_id="peer",
            network_version="v0.0.2",
            namespace_fingerprint=namespace_fingerprint(token),
            control=tcp_endpoints(("/ip4/127.0.0.1/tcp/12",), multiaddr=True),
            data_transport="zenoh",
            data=(),
        )
