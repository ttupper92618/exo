"""Bounded provider-neutral callbacks for one installed private owner."""

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from skulk.extensions.capabilities import CapabilityDescriptor, descriptor_revision
from skulk.extensions.configuration import ConfigurationNodeId
from skulk.extensions.types import ExtensionContext


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class _Request(_Wire):
    request_id: int = Field(ge=1, le=2**53 - 1)
    operation: Literal["actions", "revisions", "invoke"]
    payload: dict[str, JsonValue]


class _Invocation(_Wire):
    node_id: ConfigurationNodeId
    capability_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    payload: dict[str, JsonValue]


@final
@dataclass(frozen=True)
class HostCapability:
    """One currently ready capability owned by the exact installed adapter."""

    node_id: str
    descriptor: CapabilityDescriptor


@final
class HostCallbacks:
    """Expose live policy and ordinary Fabric admission to one protected owner.

    The owner can request only the three fixed operations below, and invocation
    must match an installed node, descriptor and revision. It cannot select a
    URL, command, executable, arbitrary node or approval credential.
    """

    def __init__(
        self,
        context: ExtensionContext,
        capabilities: Callable[[], tuple[HostCapability, ...]],
    ) -> None:
        """Bind live context and owned readiness; never retain a spending grant."""
        self.context, self.capabilities = context, capabilities

    async def dispatch(self, raw: bytes) -> JsonValue:
        """Validate a fixed callback before reading policy or using Fabric routing."""
        request = _Request.model_validate_json(raw)
        if request.operation != "invoke" and request.payload:
            raise ValueError("host observation accepts no arguments")
        if request.operation == "actions":
            return self.context.steward_actions_allowed()
        owned = self.capabilities()
        if len(owned) > 128:
            raise ValueError("too many installed callback capabilities")
        if request.operation == "revisions":
            described = await self.context.describe_node(self.context.node_id)
            visible = {
                item.qualified_id: descriptor_revision(item) for item in described
            }
            return {
                item.descriptor.qualified_id: descriptor_revision(item.descriptor)
                for item in owned
                if visible.get(item.descriptor.qualified_id)
                == descriptor_revision(item.descriptor)
            }
        invocation = _Invocation.model_validate_json(json.dumps(request.payload))
        if not any(
            item.node_id == invocation.node_id
            and item.descriptor.id == invocation.capability_id
            and item.descriptor.version == invocation.version
            and descriptor_revision(item.descriptor) == invocation.descriptor_revision
            for item in owned
        ):
            raise ValueError("callback target is outside the ready installation")
        result = await self.context.call_capability(
            self.context.node_id,
            invocation.capability_id,
            invocation.version,
            invocation.descriptor_revision,
            dict(invocation.payload),
            timeout_seconds=2.0,
        )
        if not result.ok or result.result is None:
            return {
                "state": "unavailable",
                "error": result.error.code
                if result.error is not None
                else "provider_error",
            }
        return TypeAdapter(dict[str, JsonValue]).validate_json(
            json.dumps(result.result, allow_nan=False)
        )

    async def serve(self, socket: Path) -> None:
        """Serve one verified local connection; the adapter owns reconnection."""
        reader, writer = await asyncio.open_unix_connection(socket, limit=65537)
        try:
            async with asyncio.timeout(3):
                writer.write(
                    json.dumps(
                        {"protocol": 1, "transport_node_id": str(self.context.node_id)}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                if await reader.readline() != b'{"ready":true}\n':
                    raise ValueError("host callback binding refused")
            previous = 0
            while True:
                raw = await reader.readline()
                if not raw.endswith(b"\n") or len(raw) > 65536:
                    raise ValueError("invalid host callback frame")
                request = _Request.model_validate_json(raw)
                if request.request_id <= previous:
                    raise ValueError("host callback was replayed")
                previous = request.request_id
                response: dict[str, JsonValue] = {"request_id": request.request_id}
                try:
                    async with asyncio.timeout(2.5):
                        response["value"] = await self.dispatch(raw)
                    encoded = json.dumps(response, allow_nan=False).encode() + b"\n"
                    if len(encoded) > 65536:
                        raise ValueError("host callback result exceeds bound")
                except Exception:
                    # Provider failures may contain secrets or request data.
                    encoded = (
                        json.dumps(
                            {"request_id": request.request_id, "error": "unavailable"}
                        ).encode()
                        + b"\n"
                    )
                writer.write(encoded)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
