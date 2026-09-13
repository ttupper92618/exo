"""Provider-neutral adapter to a separately supervised local plugin owner."""

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import time
from itertools import islice
from pathlib import Path
from typing import Literal, Self, final

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from skulk.extensions.calls import CapabilityCall
from skulk.extensions.capabilities import CapabilityDescriptor
from skulk.extensions.configuration import (
    ConfigurableNode,
    ConfigurationMutation,
    ConfigurationResult,
    NodeConfiguration,
)
from skulk.extensions.credentials import CredentialMutation, NodeCredentials
from skulk.extensions.managed_attachment import ManagedAttachment
from skulk.extensions.managed_host import HostCallbacks, HostCapability
from skulk.extensions.preflight import NodePreflight
from skulk.extensions.proposal_actions import ProposalApproval, ProposalOperation
from skulk.extensions.proposal_review import (
    ProposalPage,
    ProposalReference,
    ProposalReview,
)
from skulk.extensions.runtime_attachment import ProfileIdentifier
from skulk.extensions.setup import NodeSetup
from skulk.extensions.setup_actions import SetupActions, SetupMutation, SetupOperation
from skulk.extensions.steward import StewardTool
from skulk.extensions.types import ExtensionContext

_OBJECT = TypeAdapter(dict[str, JsonValue])


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class ManagedConnection(_WireModel):
    """Owner-local setup record; never accepted through management HTTP requests."""

    plugin_id: str = Field(
        pattern=r"^managed\.[a-z0-9][a-z0-9._-]{0,80}$",
        description="Stable local installation identifier.",
    )
    state_root: str = Field(
        min_length=1,
        max_length=4096,
        description="Locally provisioned absolute service-state directory.",
    )

    manager_root: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Locally provisioned manager root for automatic transport renewal.",
    )
    profile_id: ProfileIdentifier | None = Field(
        default=None,
        description="Exact locally generated manager profile identity.",
    )

    @model_validator(mode="after")
    def complete_attachment(self) -> Self:
        """Require the complete local profile, without changing legacy connections."""
        if (self.manager_root is None) != (self.profile_id is None):
            raise ValueError("incomplete managed attachment")
        if self.manager_root is not None and (
            not Path(self.manager_root).is_absolute()
            or Path(self.state_root)
            != Path(self.manager_root) / "installations" / self.plugin_id
        ):
            raise ValueError("managed installation is outside its configured manager")
        return self


class _Node(_WireModel):
    node_id: str
    bundle_id: str
    version: str
    status: str
    configurable: bool
    credentials_configurable: bool = False
    preflight_available: bool = False
    setup_available: bool = False
    setup_actions_available: bool = False
    proposals_available: bool = False
    proposal_actions_available: bool = False
    descriptors: tuple[CapabilityDescriptor, ...] = Field(max_length=8)

    def public(self) -> ConfigurableNode:
        """Project an installed node onto the ordinary management inventory."""
        return ConfigurableNode(
            node_id=self.node_id,
            bundle_id=self.bundle_id,
            version=self.version,
            status=self.status,
            configurable=self.configurable,
            credentials_configurable=self.credentials_configurable,
            preflight_available=self.preflight_available,
            setup_available=self.setup_available,
            setup_actions_available=self.setup_actions_available,
            proposals_available=self.proposals_available,
            proposal_actions_available=self.proposal_actions_available,
        )


class _Description(_WireModel):
    transport_node_id: str
    host_callbacks_available: bool = False
    nodes: tuple[_Node, ...] = Field(max_length=16)


class _Settings(_WireModel):
    revision: int = Field(ge=0)
    enabled: bool
    schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[str, JsonValue]


class _Configuration(_WireModel):
    configuration_schema: dict[str, JsonValue] = Field(alias="schema")
    settings: _Settings


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("managed plugin directory is not protected")


def _read_connection(path: Path) -> ManagedConnection:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("managed connection is not protected")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError("managed connection exceeds bound")
        return ManagedConnection.model_validate_json(raw)
    finally:
        os.close(descriptor)


@final
class ManagedOwner:
    """Adapt a fixed local owner channel without importing or launching its SDK.

    Owner process failure withdraws cached readiness. This adapter never starts
    services, substitutes an executable, retries a mutation or grants approval.
    Configuration remains an optional facet separate from capability admission.
    """

    skulk_requires = ">=1.5.2,<2"

    def __init__(
        self,
        connection: ManagedConnection,
        *,
        disabled: bool = False,
        attachment: ManagedAttachment | None = None,
    ) -> None:
        """Bind an explicitly installed local connection and admission kill switch."""
        self.name = connection.plugin_id
        self.root = Path(connection.state_root)
        if not self.root.is_absolute():
            raise ValueError("managed state root must be absolute")
        self.attachment = attachment
        if self.attachment is None and connection.manager_root is not None:
            assert connection.profile_id is not None
            self.attachment = ManagedAttachment(
                Path(connection.manager_root), connection.profile_id
            )
        self.disabled = disabled
        self.context: ExtensionContext | None = None
        self.nodes: tuple[_Node, ...] = ()
        self.observed = 0.0
        self.available = False
        self.manager_available = True
        self.manager_enabled: bool | None = None
        self.poll_task: asyncio.Task[None] | None = None
        self.host_task: asyncio.Task[None] | None = None
        self.host_callbacks_available = False
        self.refresh_lock = asyncio.Lock()

    def chat_middleware(self) -> None:
        """Managed owners do not intercept inference output."""
        return None

    def dynamic_capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """Reserve cached contracts unless an explicit disable withdraws ownership."""
        # Unknown health must not transfer a capability to another owner. A
        # confirmed disable is different: the operator withdrew that runtime,
        # while its cached nodes remain available for management and diagnostics.
        if self.disabled or self.manager_enabled is False:
            return ()
        return tuple(
            descriptor for node in self.nodes for descriptor in node.descriptors
        )

    def capability_ready(self, qualified_id: str) -> bool:
        """Admit only recently observed ready capacity while the adapter is active."""
        return (
            not self.disabled
            and self.manager_enabled is not False
            and self.available
            and self.manager_available
            and time.monotonic() - self.observed < 3
            and any(
                node.status == "ready"
                and any(d.qualified_id == qualified_id for d in node.descriptors)
                for node in self.nodes
            )
        )

    def _socket_path(self, name: Literal["control.sock", "host.sock"]) -> Path:
        _private_directory(self.root)
        digest = hashlib.sha256(str(self.root.absolute()).encode()).hexdigest()[:24]
        directory = Path("/tmp") / f"skulk-control-{os.getuid()}-{digest}"
        _private_directory(directory)
        socket = directory / name
        info = socket.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("managed socket is not protected")
        return socket

    async def _request(
        self, message: dict[str, JsonValue], *, timeout: float = 30
    ) -> dict[str, JsonValue]:
        raw = json.dumps(message, allow_nan=False).encode() + b"\n"
        if len(raw) > (65536 if message.get("operation") == "invoke" else 16384):
            raise ValueError("managed request exceeds bound")
        socket = self._socket_path("control.sock")
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(socket, limit=131073)
            try:
                writer.write(raw)
                await writer.drain()
                response = await reader.readline()
                if not response.endswith(b"\n") or len(response) > 131072:
                    raise ValueError("invalid managed response frame")
                result = _OBJECT.validate_json(response)
                if set(result) == {"error"}:
                    raise ValueError("managed operation refused")
                payload = result.get("result")
                if set(result) != {"result"} or not isinstance(payload, dict):
                    raise ValueError("invalid managed response")
                return payload
            finally:
                writer.close()
                await writer.wait_closed()

    async def refresh(self) -> None:
        """Refresh the exact local host snapshot; failures immediately remove readiness."""
        async with self.refresh_lock:
            try:
                if self.attachment is not None:
                    await self.attachment.ensure()
                result = await self._request({"operation": "describe"}, timeout=1)
                snapshot = _Description.model_validate_json(json.dumps(result))
                if self.context is None or snapshot.transport_node_id != str(
                    self.context.node_id
                ):
                    raise ValueError("managed owner belongs to a different Skulk host")
                identities = [node.node_id for node in snapshot.nodes]
                bundles = [node.bundle_id for node in snapshot.nodes]
                contracts = [
                    d.qualified_id for node in snapshot.nodes for d in node.descriptors
                ]
                if (
                    len(set(identities)) != len(identities)
                    or len(set(bundles)) != len(bundles)
                    or len(set(contracts)) != len(contracts)
                ):
                    raise ValueError("ambiguous managed owner inventory")
                for node in snapshot.nodes:
                    node.public()
                    if any(d.io_mode != "unary" for d in node.descriptors):
                        raise ValueError("managed owner requires unary contracts")
                self.nodes = snapshot.nodes
                self.host_callbacks_available = snapshot.host_callbacks_available
                self.observed = time.monotonic()
                self.available = True
            except (OSError, ValueError, TimeoutError):
                self.available = False
                self.host_callbacks_available = False
                raise RuntimeError("managed owner unavailable") from None

    def on_start(self, context: ExtensionContext) -> None:
        """Begin nonblocking local health observation; the OS owns the service."""
        if self.poll_task is not None:
            return
        self.context = context
        if self.attachment is not None:
            self.attachment.retain(str(context.node_id))
        self.poll_task = asyncio.create_task(self._poll())
        self.host_task = asyncio.create_task(self._host_loop(context))

    def _host_capabilities(self) -> tuple[HostCapability, ...]:
        return tuple(
            HostCapability(node.node_id, descriptor)
            for node in self.nodes
            for descriptor in node.descriptors
            if self.capability_ready(descriptor.qualified_id)
        )

    async def _host_loop(self, context: ExtensionContext) -> None:
        callbacks = HostCallbacks(context, self._host_capabilities)
        while True:
            if self.host_callbacks_available and self.available:
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    await callbacks.serve(self._socket_path("host.sock"))
            await asyncio.sleep(1)

    async def _poll(self) -> None:
        while True:
            with contextlib.suppress(RuntimeError):
                await self.refresh()
            await asyncio.sleep(1)

    async def on_stop(self) -> None:
        """Stop observation and admission without stopping independent owner cleanup."""
        self.available = False
        # Legacy discovery and live manager inventory can share one adapter.
        # Claim its observer before yielding so concurrent shutdown releases once.
        task, self.poll_task = self.poll_task, None
        host_task, self.host_task = self.host_task, None
        if host_task is not None:
            host_task.cancel()
            await asyncio.gather(host_task, return_exceptions=True)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.attachment is not None:
                await self.attachment.release()

    async def configuration_nodes(self) -> tuple[ConfigurableNode, ...]:
        """Read all installed nodes, including disabled children."""
        await self.refresh()
        return tuple(node.public() for node in self.nodes)

    async def steward_tools(self, context: ExtensionContext) -> tuple[StewardTool, ...]:
        """Discover optional private tools without importing provider policy into core."""
        if self.context is not context:
            return ()
        await self.refresh()
        if not self.host_callbacks_available:
            return ()
        result = await self._request({"operation": "steward-tools"}, timeout=1.5)
        if set(result) != {"tools"}:
            raise ValueError("invalid managed steward discovery")
        tools = TypeAdapter(tuple[StewardTool, ...]).validate_json(
            json.dumps(result["tools"])
        )
        if len(tools) > 16:
            raise ValueError("too many managed steward tools")
        return tools

    async def node_proposals(self, node_id: str, offset: int = 0) -> ProposalPage:
        """Read a bounded journal page without preparing or executing an effect."""
        await self.refresh()
        if not self._node(node_id).proposals_available or not 0 <= offset <= 127:
            raise LookupError("proposal review unavailable")
        result = await self._request(
            {
                "operation": "proposal-list",
                "plugin_id": self.name,
                "node_id": node_id,
                "offset": offset,
            }
        )
        return ProposalPage.model_validate_json(json.dumps(result))

    async def node_proposal(self, reference: ProposalReference) -> ProposalReview:
        """Resolve the exact owned reference; caller canonical input is never accepted."""
        if reference.plugin_id != self.name:
            raise ValueError("proposal belongs to another installation")
        await self.refresh()
        if not self._node(reference.node_id).proposals_available:
            raise LookupError("proposal review unavailable")
        result = await self._request(
            {
                "operation": "proposal-review",
                "plugin_id": self.name,
                "node_id": reference.node_id,
                "reference": reference.model_dump(mode="json"),
            }
        )
        reviewed = ProposalReview.model_validate_json(json.dumps(result))
        if reviewed.proposal.reference != reference:
            raise ValueError("proposal response differs from selected reference")
        return reviewed

    async def approve_node_proposal(
        self, mutation: ProposalApproval, operator_id: str
    ) -> ProposalOperation:
        """Forward an explicitly authorized owner action, never replacement canonical input."""
        reference = mutation.reference
        if reference.plugin_id != self.name:
            raise ValueError("proposal belongs to another installation")
        await self.refresh()
        if not self._node(reference.node_id).proposal_actions_available:
            raise LookupError("proposal actions unavailable")
        result = await self._request(
            {
                "operation": "proposal-approve",
                "plugin_id": self.name,
                "node_id": reference.node_id,
                "operation_id": mutation.operation_id,
                "mutation": mutation.model_dump(mode="json"),
                "operator_id": operator_id,
            }
        )
        operation = ProposalOperation.model_validate_json(json.dumps(result))
        if (
            operation.reference != reference
            or operation.operation_id != mutation.operation_id
        ):
            raise ValueError("proposal action response differs")
        return operation

    async def node_proposal_operation(
        self, node_id: str, operation_id: str
    ) -> ProposalOperation:
        """Observe retained progress without replaying the original owner action."""
        return await self._proposal_operation(node_id, operation_id, None)

    async def resume_node_proposal(
        self, node_id: str, operation_id: str, operator_id: str
    ) -> ProposalOperation:
        """Request explicit approval recovery; submitted execution is never replayed."""
        return await self._proposal_operation(node_id, operation_id, operator_id)

    async def _proposal_operation(
        self, node_id: str, operation_id: str, operator_id: str | None
    ) -> ProposalOperation:
        result = await self._request(
            {
                "operation": "proposal-operation"
                if operator_id is None
                else "proposal-resume",
                "plugin_id": self.name,
                "node_id": node_id,
                "operation_id": operation_id,
                "operator_id": operator_id,
            }
        )
        operation = ProposalOperation.model_validate_json(json.dumps(result))
        if (
            operation.reference.plugin_id != self.name
            or operation.reference.node_id != node_id
            or operation.operation_id != operation_id
        ):
            raise ValueError("proposal action response differs")
        return operation

    async def handle_steward_tool(
        self,
        context: ExtensionContext,
        tool: StewardTool,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Route a fresh exact tool to the owner; this facet cannot approve spending."""
        if self.context is not context or not self.host_callbacks_available:
            raise ValueError("managed steward unavailable")
        await self.refresh()
        return await self._request(
            {
                "operation": "steward-invoke",
                "tool": tool.model_dump(mode="json"),
                "arguments": arguments,
            },
            timeout=20,
        )

    def _node(self, node_id: str) -> _Node:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise LookupError("managed node is not installed")

    async def node_configuration(self, node_id: str) -> NodeConfiguration:
        """Read ordinary settings from the exact observed node with an identity fence."""
        await self.refresh()
        node = self._node(node_id)
        response = await self._request(
            {"operation": "get", "bundle_id": node.bundle_id, "node_id": node_id}
        )
        current = _Configuration.model_validate_json(json.dumps(response))
        return NodeConfiguration(
            node_id=node_id,
            configuration_schema=current.configuration_schema,
            revision=current.settings.revision,
            enabled=current.settings.enabled,
            schema_digest=current.settings.schema_digest,
            values=current.settings.values,
        )

    async def configure_node(
        self, node_id: str, mutation: ConfigurationMutation
    ) -> ConfigurationResult:
        """Send one exact revision/schema-fenced action; never replay a lost response."""
        await self.refresh()
        node = self._node(node_id)
        if (mutation.operation in {"validate", "edit"}) != (
            mutation.values is not None
        ):
            raise ValueError("invalid configuration mutation")
        await self._request(
            {
                "operation": mutation.operation,
                "bundle_id": node.bundle_id,
                "node_id": node_id,
                "values": mutation.values,
                "expected_revision": mutation.expected_revision,
                "expected_schema_digest": mutation.expected_schema_digest,
            }
        )
        return ConfigurationResult(
            configuration=await self.node_configuration(node_id), validated=True
        )

    async def node_preflight(self, node_id: str) -> NodePreflight:
        """Read fresh setup checks from the exact installed private owner."""
        await self.refresh()
        node = self._node(node_id)
        if not node.preflight_available:
            raise LookupError("managed node does not provide preflight")
        response = await self._request(
            {"operation": "preflight", "bundle_id": node.bundle_id, "node_id": node_id}
        )
        return NodePreflight.model_validate_json(json.dumps(response))

    async def node_setup(self, node_id: str) -> NodeSetup:
        """Read public setup artifacts; the owner never initializes keys on this read."""
        await self.refresh()
        node = self._node(node_id)
        if not node.setup_available:
            raise LookupError("managed node does not provide setup exports")
        response = await self._request(
            {"operation": "setup", "bundle_id": node.bundle_id, "node_id": node_id}
        )
        return NodeSetup.model_validate_json(json.dumps(response))

    async def _setup_request(
        self, node_id: str, operation: str, extra: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        await self.refresh()
        node = self._node(node_id)
        if not node.setup_actions_available:
            raise LookupError("managed node does not provide setup actions")
        return await self._request(
            {"operation": operation, "bundle_id": node.bundle_id, "node_id": node_id}
            | (extra or {})
        )

    async def node_setup_actions(self, node_id: str) -> SetupActions:
        """Read fixed setup forms and retained progress without doing setup work."""
        response = await self._setup_request(node_id, "setup-actions")
        return SetupActions.model_validate_json(json.dumps(response))

    async def start_node_setup(
        self, node_id: str, mutation: SetupMutation
    ) -> SetupOperation:
        """Submit exact durable intent once, independently of the browser lifetime."""
        response = await self._setup_request(
            node_id, "setup-start", {"mutation": mutation.model_dump(mode="json")}
        )
        return SetupOperation.model_validate_json(json.dumps(response))

    async def node_setup_operation(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Read retained progress without occupying the child's invocation slot."""
        response = await self._setup_request(
            node_id, "setup-operation", {"operation_id": operation_id}
        )
        return SetupOperation.model_validate_json(json.dumps(response))

    async def resume_node_setup(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Explicitly resume the same accepted setup intent through its durable ID."""
        response = await self._setup_request(
            node_id, "setup-resume", {"operation_id": operation_id}
        )
        return SetupOperation.model_validate_json(json.dumps(response))

    async def node_credentials(self, node_id: str) -> NodeCredentials:
        """Read reference readiness through the protected owner connection, without values."""
        await self.refresh()
        node = self._node(node_id)
        response = await self._request(
            {
                "operation": "credentials",
                "bundle_id": node.bundle_id,
                "node_id": node_id,
            }
        )
        return NodeCredentials.model_validate_json(json.dumps(response))

    async def change_node_credential(
        self, node_id: str, mutation: CredentialMutation
    ) -> NodeCredentials:
        """Forward a write-only value exactly once; ordinary errors never include it."""
        await self.refresh()
        node = self._node(node_id)
        wire = mutation.model_dump(mode="json")
        wire.update(
            {
                "operation": "replace-credential"
                if mutation.operation == "replace"
                else "retire-credential",
                "bundle_id": node.bundle_id,
                "node_id": node_id,
            }
        )
        if mutation.value is not None:
            wire["value"] = mutation.value.get_secret_value()
        response = await self._request(wire)
        return NodeCredentials.model_validate_json(json.dumps(response))

    async def handle_call(
        self, context: ExtensionContext, call: CapabilityCall
    ) -> dict[str, object]:
        """Forward one admitted unary call with its exact installed node and deadline."""
        qualified_id = f"{call.capability_id}@{call.version}"
        if self.context is not context or not self.capability_ready(qualified_id):
            raise RuntimeError("managed capability unavailable")
        node = next(
            node
            for node in self.nodes
            if any(d.qualified_id == qualified_id for d in node.descriptors)
        )
        payload = _OBJECT.validate_json(json.dumps(call.payload, allow_nan=False))
        result = await self._request(
            {
                "operation": "invoke",
                "node_id": node.node_id,
                "invoke": {
                    "protocol": 1,
                    "kind": "invoke",
                    "call_id": call.call_id,
                    "capability_id": call.capability_id,
                    "version": call.version,
                    "descriptor_revision": call.descriptor_revision,
                    "remaining_seconds": min(call.timeout_seconds, 30.0),
                    "payload": payload,
                },
            },
            timeout=min(call.timeout_seconds, 30.0),
        )
        return dict(result)


def load_managed_owners(
    directory: Path, *, disabled: bool = False
) -> tuple[ManagedOwner, ...]:
    """Read bounded owner-only connection records, without importing private code.

    Missing configuration leaves the existing host unchanged. Invalid records
    are reported without local details and cannot affect inference startup.
    """
    try:
        if not directory.exists():
            return ()
        _private_directory(directory)
        paths = sorted(islice(directory.glob("*.json"), 17))
        if len(paths) > 16:
            raise ValueError("too many managed owners")
        owners: dict[str, ManagedOwner] = {}
        conflicts: set[str] = set()
        attachments: dict[tuple[str, str], ManagedAttachment] = {}
        for path in paths:
            try:
                connection = _read_connection(path)
                attachment = None
                if connection.manager_root is not None:
                    assert connection.profile_id is not None
                    key = (connection.manager_root, connection.profile_id)
                    attachment = attachments.get(key)
                    if attachment is None:
                        attachment = ManagedAttachment(
                            Path(connection.manager_root), connection.profile_id
                        )
                        attachments[key] = attachment
                owner = ManagedOwner(
                    connection, disabled=disabled, attachment=attachment
                )
                if owner.name in owners or owner.name in conflicts:
                    owners.pop(owner.name, None)
                    conflicts.add(owner.name)
                else:
                    owners[owner.name] = owner
            except (OSError, ValueError):
                logger.warning(
                    "A managed plugin connection is unavailable; check local setup"
                )
        return tuple(owners.values())
    except (OSError, ValueError):
        logger.warning("Managed plugin connections are unavailable; check local setup")
        return ()
