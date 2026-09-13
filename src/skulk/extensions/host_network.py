"""Read-only local transport facts for provider-owned secure attachment."""

import hashlib
from collections.abc import Sequence
from ipaddress import ip_address
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from skulk.utils.pydantic_ext import FrozenModel


class TcpEndpoint(FrozenModel):
    """One numeric address on which the local process actually listens."""

    host: str = Field(max_length=45, description="Numeric local IPv4 or IPv6 address.")
    port: int = Field(ge=1, le=65535, description="Actual bound TCP port, never zero.")

    @model_validator(mode="after")
    def numeric_host(self) -> Self:
        """Reject DNS, wildcard and scoped addresses at the attachment boundary."""
        address = ip_address(self.host)
        if address.is_unspecified or address.is_multicast or "%" in self.host:
            raise ValueError("attachment requires a concrete numeric listener")
        return self


class HostNetwork(FrozenModel):
    """Current local listeners, identity and non-routing namespace comparison."""

    node_id: str = Field(
        min_length=1, max_length=128, description="Live local peer ID."
    )
    network_version: str = Field(max_length=32, description="Control protocol version.")
    namespace_fingerprint: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="Domain-separated comparison digest, not a key or routing namespace.",
    )
    control: tuple[TcpEndpoint, ...] = Field(
        min_length=1, max_length=16, description="Live local control listeners."
    )
    data_transport: Literal["gossipsub", "zenoh"] = Field(
        description="Transport the new peer must match for inference output."
    )
    data: tuple[TcpEndpoint, ...] = Field(
        max_length=16, description="Live Zenoh TCP listeners; empty for gossipsub."
    )

    @model_validator(mode="after")
    def data_matches_transport(self) -> Self:
        """Never advertise a ready Zenoh attachment without a TCP listener."""
        if bool(self.data) != (self.data_transport == "zenoh"):
            raise ValueError("data listener and transport differ")
        return self


def namespace_fingerprint(token: str) -> str:
    """Compare namespace tokens without disclosing their routing-derived digest."""
    # Zenoh uses the ordinary SHA-256 of the token as its routing namespace.
    # Publishing that digest here would therefore expose the isolation value.
    return hashlib.sha256(b"skulk-host-attachment-v1\0" + token.encode()).hexdigest()


def tcp_endpoints(
    addresses: Sequence[str], *, multiaddr: bool
) -> tuple[TcpEndpoint, ...]:
    """Project bound numeric TCP listeners, normalizing wildcard binds to loopback.

    Unsupported transports are ignored. Ambiguous, invalid and excessive results
    fail closed instead of producing an endpoint that requires a guessed port.
    """
    endpoints: set[tuple[str, int]] = set()
    for address in addresses:
        if multiaddr:
            parts = address.split("/")
            if len(parts) != 5 or parts[1] not in ("ip4", "ip6") or parts[3] != "tcp":
                continue
            host, raw_port = parts[2], parts[4]
        else:
            if not address.startswith("tcp/"):
                continue
            parsed = urlsplit("tcp://" + address[4:])
            if parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("invalid local TCP locator")
            host, raw_port = parsed.hostname or "", str(parsed.port or 0)
        parsed_host = ip_address(host)
        if parsed_host.is_unspecified:
            host = "127.0.0.1" if parsed_host.version == 4 else "::1"
        endpoint = TcpEndpoint(host=host, port=int(raw_port))
        endpoints.add((endpoint.host, endpoint.port))
        if len(endpoints) > 16:
            raise ValueError("too many local TCP listeners")
    return tuple(TcpEndpoint(host=host, port=port) for host, port in sorted(endpoints))
