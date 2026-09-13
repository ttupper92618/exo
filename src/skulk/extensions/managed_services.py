"""Live local manager inventory shared by plugin configuration and Fabric lookup."""

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Literal, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from skulk.extensions.managed import ManagedConnection, ManagedOwner
from skulk.extensions.managed_attachment import ManagedAttachment
from skulk.extensions.runtime_artifacts import Digest
from skulk.extensions.runtime_attachment import (
    InstallationIdentifier,
    ProfileIdentifier,
    ServiceConnection,
)
from skulk.extensions.runtime_files import read_private
from skulk.extensions.runtime_manager import (
    InstallationRequest,
    InstallRecoveryRequest,
    InstallSubmission,
    InventoryRequest,
    OperationRequest,
    ReleaseRequest,
    SourceRegistration,
    SubmitRequest,
    manager_request,
)
from skulk.extensions.runtime_service import RuntimeServiceStatus
from skulk.extensions.types import ExtensionContext


class ManagedInstallation(BaseModel):
    """Bounded local desired state and process observation, with no paths or secrets."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    plugin_id: InstallationIdentifier = Field(
        description="Stable local installed-plugin identifier."
    )
    error_code: (
        Literal[
            "initializing",
            "installation_unavailable",
            "service_unavailable",
            "attachment_recovery_required",
        ]
        | None
    ) = Field(default=None, description="Sanitized local installation failure class.")
    operation_id: ProfileIdentifier | None = Field(
        default=None,
        description="Pending or selected local operation, for reconnect without resubmission.",
    )
    operation_state: (
        Literal["accepted", "applying", "complete", "failed", "recovery_required"]
        | None
    ) = Field(
        default=None, description="Current status of the referenced local operation."
    )
    selected_digest: Digest | None = Field(
        default=None, description="Desired signed runtime generation."
    )
    selection_revision: int = Field(
        default=0,
        ge=0,
        description="Revision of the selected runtime, or zero before selection.",
    )
    enabled: bool = Field(
        default=False, description="Whether the selected runtime is enabled."
    )
    uninstalled: bool = Field(
        default=False,
        description="Published uninstall intent; retained state remains available for cleanup and explicit reinstallation.",
    )
    service: RuntimeServiceStatus | None = Field(
        default=None,
        description="Observed process health, independent of capability readiness.",
    )
    stale: bool = Field(
        description="Whether the process observation is absent or stale."
    )


class ManagedInventory(BaseModel):
    """Manager inventory available even when every plugin runtime is disabled or broken."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    installations: tuple[ManagedInstallation, ...] = Field(
        max_length=16, description="Bounded registered plugin installations."
    )


type ManagementRequest = (
    InventoryRequest
    | InstallationRequest
    | SubmitRequest
    | OperationRequest
    | ReleaseRequest
    | InstallSubmission
    | SourceRegistration
    | InstallRecoveryRequest
)


@final
class ManagedServices:
    """Observe locally installed services without reloading existing extensions.

    Missing setup is inert. A later locally generated connection starts observation
    automatically. API shutdown withdraws its adapters and releases attachment;
    the OS continues to own plugin controllers and independent cleanup.
    """

    def __init__(
        self,
        connection_path: Path,
        *,
        disabled: bool = False,
        existing_owners: tuple[ManagedOwner, ...] = (),
    ) -> None:
        """Observe one fixed protected local connection, never an HTTP-supplied path."""
        self.path = connection_path
        self.disabled = disabled
        self.existing_owners = existing_owners
        self.context: ExtensionContext | None = None
        self.connection: ServiceConnection | None = None
        self.attachment: ManagedAttachment | None = None
        self.owners: dict[str, ManagedOwner] = {}
        self.task: asyncio.Task[None] | None = None
        self.guard = asyncio.Lock()
        self.closed = False

    def on_start(self, context: ExtensionContext) -> None:
        """Start nonblocking discovery, including when local setup does not yet exist."""
        if self.task is not None or self.closed:
            return
        self.context = context
        self.task = asyncio.create_task(self._poll())

    async def _connect(self) -> None:
        if self.closed or self.context is None:
            raise ValueError("managed service observation is not running")
        connection = ServiceConnection.model_validate_json(
            read_private(self.path, 8192)
        )
        if connection != self.connection:
            await self._detach()
            self.connection = connection
            self.attachment = next(
                (
                    owner.attachment
                    for owner in self.existing_owners
                    if owner.attachment is not None
                    and owner.attachment.root == Path(connection.manager_root)
                    and owner.attachment.profile_id == connection.profile_id
                ),
                None,
            )
            if self.attachment is None:
                self.attachment = ManagedAttachment(
                    Path(connection.manager_root), connection.profile_id
                )
            self.attachment.retain(str(self.context.node_id))
        assert self.attachment is not None
        await self.attachment.ensure()

    async def request(self, request: ManagementRequest) -> dict[str, JsonValue]:
        """Send one typed local management request; never accept an attachment override.

        Local setup determines the service root and profile. The original operation
        identifier is preserved; a lost mutation response is never replayed here.
        """
        async with self.guard:
            await self._connect()
            assert self.connection is not None
            root = Path(self.connection.manager_root)
        # Source HTTPS and durable operations must not monopolize membership
        # observation. The fixed connection is captured before this request starts.
        result = await manager_request(root, request)
        payload = result.get("result")
        if set(result) != {"result"} or not isinstance(payload, dict):
            raise ValueError("managed service request refused")
        return payload

    async def refresh(self) -> ManagedInventory:
        """Reconcile current manager membership without restarting Skulk's API."""
        async with self.guard:
            try:
                await self._connect()
                assert self.connection is not None and self.context is not None
                assert self.attachment is not None
                result = await manager_request(
                    Path(self.connection.manager_root), InventoryRequest()
                )
                if set(result) != {"result"}:
                    raise ValueError("managed service inventory refused")
                inventory = ManagedInventory.model_validate_json(
                    json.dumps(result["result"])
                )
                identifiers = [item.plugin_id for item in inventory.installations]
                if len(set(identifiers)) != len(identifiers):
                    raise ValueError("ambiguous managed installation inventory")
                for identifier in set(self.owners) - set(identifiers):
                    owner = self.owners.pop(identifier)
                    await owner.on_stop()
                for item in inventory.installations:
                    identifier = item.plugin_id
                    if identifier not in self.owners:
                        expected_root = (
                            Path(self.connection.manager_root)
                            / "installations"
                            / identifier
                        )
                        owner = next(
                            (
                                existing
                                for existing in self.existing_owners
                                if existing.name == identifier
                                and existing.root == expected_root
                                and existing.attachment is self.attachment
                            ),
                            None,
                        )
                        if owner is None:
                            owner = ManagedOwner(
                                ManagedConnection(
                                    plugin_id=identifier,
                                    state_root=str(expected_root),
                                    manager_root=self.connection.manager_root,
                                    profile_id=self.connection.profile_id,
                                ),
                                disabled=self.disabled,
                                attachment=self.attachment,
                            )
                        self.owners[identifier] = owner
                        owner.on_start(self.context)
                    # Error summaries omit selection fields and default enabled
                    # to false. Only an observed selection proves owner withdrawal.
                    self.owners[identifier].manager_enabled = (
                        item.enabled
                        if item.selected_digest is not None and item.error_code is None
                        else None
                    )
                    self.owners[identifier].manager_available = (
                        item.enabled
                        and not item.stale
                        and item.error_code is None
                        and item.service is not None
                        and item.service.state == "running"
                        and item.service.active_digest == item.selected_digest
                    )
                return inventory
            except (OSError, ValueError, TimeoutError):
                # Keep unavailable installations visible for configuration, but
                # synchronously withdraw admission while manager state is unknown.
                for owner in self.owners.values():
                    owner.available = False
                    owner.manager_available = False
                    owner.manager_enabled = None
                raise

    async def _poll(self) -> None:
        while True:
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                await self.refresh()
            await asyncio.sleep(1)

    async def _detach(self) -> None:
        owners, self.owners = tuple(self.owners.values()), {}
        await asyncio.gather(*(owner.on_stop() for owner in owners))
        if self.attachment is not None:
            await self.attachment.release()
            self.attachment = None
        self.connection = None

    async def on_stop(self) -> None:
        """Stop observation and local API attachment without stopping managed services."""
        if self.closed:
            return
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        async with self.guard:
            await self._detach()
