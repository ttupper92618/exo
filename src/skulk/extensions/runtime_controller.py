"""Durable local stop/select/start operations independent of plugin health."""

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Literal, Self, final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skulk.extensions.runtime_artifacts import Digest
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_install import finish_runtime_work
from skulk.extensions.runtime_selection import (
    RuntimeSelection,
    RuntimeSelector,
    SelectionOperation,
)
from skulk.extensions.runtime_service import RuntimeService

_OWNER_EXIT_SECONDS = 35.0


class LifecycleRequest(BaseModel):
    """Immutable local lifecycle intent with no command, path or paid approval."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$",
        description="Caller-retained idempotent local operation ID.",
    )
    action: Literal["activate", "select", "disable", "uninstall"] = Field(
        description="Activate or select a retained generation, disable it, or uninstall while retaining cleanup state and recovery artifacts."
    )
    expected_revision: int = Field(
        ge=0, description="Reviewed current installation selection revision."
    )
    runtime_digest: Digest | None = Field(
        default=None, description="Exact retained signed generation for activation."
    )
    rollback: bool = Field(
        default=False,
        description="Explicit permission to select an older retained release.",
    )
    accept_permissions: bool = Field(
        default=False,
        description="Explicit acceptance of expanded plugin permissions; never spending approval.",
    )

    @model_validator(mode="after")
    def valid_action(self) -> Self:
        """Reject ambiguous or irrelevant fields before any local operation starts."""
        if self.action in ("activate", "select") and self.runtime_digest is None:
            raise ValueError("runtime selection requires a runtime digest")
        if self.action in ("disable", "uninstall") and (
            self.runtime_digest is not None or self.rollback or self.accept_permissions
        ):
            raise ValueError("withdrawal accepts no activation options")
        return self


class LifecycleOperation(BaseModel):
    """Durable desired-state transition; completion does not imply provider readiness."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    request: LifecycleRequest = Field(
        description="Exact accepted local request; contains no credentials."
    )
    selection: RuntimeSelection = Field(
        description="Exact previewed selection; activation revalidates it."
    )
    state: Literal[
        "accepted", "applying", "complete", "failed", "recovery_required", "superseded"
    ] = Field(
        description="Local operation progress, independent of service and capability health."
    )
    error_code: (
        Literal["validation_failed", "ownership_busy", "local_io_failed"] | None
    ) = Field(
        default=None,
        description="Sanitized corrective failure class; no exception payload.",
    )
    withdraws_operation_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{32}$",
        description="Prior stalled local intent withdrawn by explicit disable or uninstall; never a provider request.",
    )

    @model_validator(mode="after")
    def valid_identity(self) -> Self:
        """Bind the durable intent to one exact operation and revision."""
        if (
            self.request.operation_id != self.selection.operation_id
            or self.selection.revision != self.request.expected_revision + 1
        ):
            raise ValueError("lifecycle selection identity differs")
        if self.selection.enabled != (self.request.action == "activate"):
            raise ValueError("lifecycle selection action differs")
        if self.withdraws_operation_id is not None and (
            self.request.action not in ("disable", "uninstall")
            or self.withdraws_operation_id == self.request.operation_id
        ):
            raise ValueError("only withdrawal can replace another lifecycle operation")
        if (
            self.request.action in ("activate", "select")
            and self.selection.runtime_digest != self.request.runtime_digest
        ):
            raise ValueError("lifecycle selected runtime differs")
        if len(self.model_dump_json().encode()) > 131072:
            raise ValueError("lifecycle intent exceeds bound")
        return self


@final
class RuntimeController:
    """Manage one installation inside a separately supervised generic manager.

    One exclusive controller owns durable local operations and its runtime task.
    Client disconnect cannot abandon accepted work. Restart resumes only recorded
    local selection intent; no provider call or spending approval is performed.
    """

    def __init__(self, root: Path) -> None:
        """Prepare protected storage without acquiring control or starting code."""
        self.selector = RuntimeSelector(root)
        self.root = self.selector.root
        self.records = self.root / "lifecycle-operations"
        private_directory(self.records)
        self.pending = self.records / "pending.json"
        self.lock: RuntimeLock | None = None
        self.guard = asyncio.Lock()
        self.closed = False
        self.work: asyncio.Task[None] | None = None
        self.service: RuntimeService | None = None
        self.service_task: asyncio.Task[bool] | None = None
        self.stopped = asyncio.Event()
        self.close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Acquire control and reconcile interrupted local intent before admission."""
        if self.lock is not None or self.closed:
            raise ValueError("controller lifetime already used")
        self.lock = RuntimeLock(self.root, "manager.lock")
        try:
            if self.pending.exists():
                operation = LifecycleOperation.model_validate_json(
                    read_private(self.pending)
                )
                self.work = asyncio.create_task(self._perform(operation))
                await finish_runtime_work(self.work)
            else:
                # A pending selector operation from local CLI is reconciled by
                # RuntimeService before it executes any private interpreter.
                await self._wait_stopped()
                self._start_service()
        except BaseException:
            await self.close()
            raise

    def operation(self, operation_id: str) -> LifecycleOperation:
        """Read durable progress without replaying work on client reconnect."""
        if len(operation_id) != 32 or any(
            c not in "0123456789abcdef" for c in operation_id
        ):
            raise ValueError("invalid lifecycle operation ID")
        try:
            raw = read_private(self.records / (operation_id + ".json"))
        except FileNotFoundError:
            raw = read_private(self.pending)
        result = LifecycleOperation.model_validate_json(raw)
        if result.request.operation_id != operation_id:
            raise FileNotFoundError("lifecycle operation not found")
        if result.state in {"accepted", "applying"} and (
            self.work is None or self.work.done()
        ):
            # A journal write may fail after its atomic rename but before its
            # directory sync. A retained accepted record without live owned work
            # is recovery-needed, never a claim that background work is running.
            return result.model_copy(
                update={"state": "recovery_required", "error_code": "local_io_failed"}
            )
        return result

    def _save(self, operation: LifecycleOperation) -> None:
        write_private(
            self.records / (operation.request.operation_id + ".json"),
            operation.model_dump_json().encode(),
        )

    def is_uninstalled(self, selection: RuntimeSelection | None) -> bool:
        """Read uninstall status from published intent, independently of pending work.

        Retained state remains manageable. Only a later committed selection or
        activation reinstalls it; downloading or failing a new operation cannot.
        Legacy direct-selector selections have no lifecycle record.
        """
        if selection is None:
            return False
        try:
            operation = self.operation(selection.operation_id)
        except FileNotFoundError:
            return False
        return operation.request.action == "uninstall"

    def _clear_pending(self) -> None:
        self.pending.unlink(missing_ok=True)
        descriptor = os.open(self.records, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def _preview(self, request: LifecycleRequest) -> RuntimeSelection:
        if request.action == "disable" and self.is_uninstalled(self.selector.current()):
            raise ValueError("installation is uninstalled; select or activate to reinstall")
        if request.action in ("activate", "select"):
            assert request.runtime_digest is not None
            return await self.selector.preview(
                request.runtime_digest,
                expected_revision=request.expected_revision,
                operation_id=request.operation_id,
                rollback=request.rollback,
                accept_permissions=request.accept_permissions,
                enabled=request.action == "activate",
            )
        pending = (
            LifecycleOperation.model_validate_json(read_private(self.pending))
            if self.pending.exists()
            else None
        )
        return self.selector.preview_disable(
            expected_revision=request.expected_revision,
            operation_id=request.operation_id,
            initial_selection=pending.selection if pending is not None else None,
        )

    async def submit(self, request: LifecycleRequest) -> LifecycleOperation:
        """Validate and durably accept work, then return its reconnectable status.

        A stale revision or invalid target is refused before stopping a healthy
        owner. A repeated ID returns its exact original operation without replay.
        """
        async with self.guard:
            if self.closed or self.lock is None:
                raise ValueError("controller is unavailable")
            try:
                prior = self.operation(request.operation_id)
            except FileNotFoundError:
                prior = None
            if prior is not None:
                if prior.request != request:
                    raise ValueError("lifecycle operation identity conflict")
                return prior
            if self.work is not None and not self.work.done():
                raise ValueError("another lifecycle operation needs completion")
            pending = (
                LifecycleOperation.model_validate_json(read_private(self.pending))
                if self.pending.exists()
                else None
            )
            if pending is not None and (
                request.action not in ("disable", "uninstall")
                or pending.request.action in ("disable", "uninstall")
            ):
                raise ValueError("another lifecycle operation needs completion")
            selection = await self._preview(request)
            operation = LifecycleOperation(
                request=request,
                selection=selection,
                state="accepted",
                withdraws_operation_id=(
                    pending.request.operation_id if pending is not None else None
                ),
            )
            if pending is not None:
                # Preserve the original request before replacing its last
                # recovery pointer. The new durable intent completes withdrawal
                # on restart, even if the old release can never run again.
                self._save(self.operation(pending.request.operation_id))
            # The full intent is durable before acknowledging or creating a task.
            # Pending-first permits recovery if the per-operation write fails.
            write_private(self.pending, operation.model_dump_json().encode())
            self._save(operation)
            self.work = asyncio.create_task(self._perform(operation))
            return operation

    async def recover(self, operation_id: str) -> LifecycleOperation:
        """Resume only an existing durable local intent after its fault is corrected."""
        async with self.guard:
            if self.closed or self.lock is None:
                raise ValueError("controller is unavailable")
            prior = self.operation(operation_id)
            if self.work is not None and not self.work.done():
                return prior
            if not self.pending.exists():
                return prior
            pending = LifecycleOperation.model_validate_json(read_private(self.pending))
            if pending.request != prior.request or pending.selection != prior.selection:
                raise ValueError("different lifecycle operation needs recovery")
            accepted = pending.model_copy(
                update={"state": "accepted", "error_code": None}
            )
            self._save(accepted)
            self.work = asyncio.create_task(self._perform(accepted))
            return accepted

    def _start_service(self) -> None:
        if self.closed or (
            self.service_task is not None and not self.service_task.done()
        ):
            return
        self.stopped = asyncio.Event()
        self.service = RuntimeService(self.root)
        self.service_task = asyncio.create_task(self.service.serve(self.stopped))

    async def _stop_service(self) -> None:
        self.stopped.set()
        if self.service_task is not None:
            # An already failed owner must not permanently prevent disable.
            # The actual process fences below still have to prove absence.
            with contextlib.suppress(OSError, ValueError):
                await finish_runtime_work(self.service_task)
            self.service_task = None
        # A previous manager may have died while its private owner is still
        # shutting down. Never switch under it or assume a PID proves absence.
        await self._wait_stopped()

    async def _wait_stopped(self) -> None:
        deadline = asyncio.get_running_loop().time() + _OWNER_EXIT_SECONDS
        while True:
            try:
                RuntimeLock(self.root, "service.lock").close()
                RuntimeLock(self.root, "supervisor.lock").close()
                return
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _perform(self, operation: LifecycleOperation) -> None:
        result = operation
        try:
            current = self.selector.current()
            if operation.request.action in ("disable", "uninstall"):
                if (
                    current != operation.selection
                    and await self._preview(operation.request) != operation.selection
                ):
                    raise ValueError("reviewed withdrawal changed")
                await self._stop_service()
                pending = (
                    SelectionOperation.model_validate_json(
                        read_private(self.selector.pending)
                    )
                    if self.selector.pending.exists()
                    else None
                )
                if pending is not None and pending.operation_id == operation.request.operation_id:
                    selected = await self.selector.recover()
                elif pending is not None and current == operation.selection:
                    raise ValueError("different selection needs recovery")
                elif current != operation.selection:
                    selected = self.selector.disable(
                        expected_revision=operation.request.expected_revision,
                        operation_id=operation.request.operation_id,
                        initial_selection=operation.selection,
                    )
                else:
                    selected = None
                if selected is not None and selected.selection != operation.selection:
                    raise ValueError("committed withdrawal differs")
            elif self.selector.pending.exists():
                pending = SelectionOperation.model_validate_json(
                    read_private(self.selector.pending)
                )
                if (
                    pending.selection != operation.selection
                    or pending.verify_runtime != (operation.request.action == "select")
                ):
                    raise ValueError("different selection needs recovery")
                await self._stop_service()
                await self.selector.recover()
            elif current != operation.selection:
                if await self._preview(operation.request) != operation.selection:
                    raise ValueError("reviewed selection changed")
                result = operation.model_copy(update={"state": "applying"})
                self._save(result)
                await self._stop_service()
                request = operation.request
                if request.action in ("activate", "select"):
                    assert request.runtime_digest is not None
                    selected = await self.selector.activate(
                        request.runtime_digest,
                        expected_revision=request.expected_revision,
                        operation_id=request.operation_id,
                        rollback=request.rollback,
                        accept_permissions=request.accept_permissions,
                        enabled=request.action == "activate",
                    )
                else:
                    selected = self.selector.disable(
                        expected_revision=request.expected_revision,
                        operation_id=request.operation_id,
                    )
                if selected.selection != operation.selection:
                    raise ValueError("committed selection differs")
            if operation.withdraws_operation_id is not None:
                previous = LifecycleOperation.model_validate_json(
                    read_private(
                        self.records / (operation.withdraws_operation_id + ".json")
                    )
                )
                self._save(
                    previous.model_copy(
                        update={
                            "state": "complete"
                            if previous.selection.revision <= operation.request.expected_revision
                            else "superseded",
                            "error_code": None
                            if previous.selection.revision <= operation.request.expected_revision
                            else previous.error_code,
                        }
                    )
                )
            result = operation.model_copy(
                update={"state": "complete", "error_code": None}
            )
            self._save(result)
            self._clear_pending()
            self._start_service()
        except (OSError, ValueError) as error:
            code = (
                "ownership_busy"
                if isinstance(error, BlockingIOError)
                else "local_io_failed"
                if isinstance(error, OSError)
                else "validation_failed"
            )
            # If publication may have happened, keep the intent for boot/local
            # recovery. Otherwise retain a failed record while allowing a new
            # corrected request, including disable of an invalid installation.
            interrupted = (
                operation.withdraws_operation_id is not None
                or self.selector.pending.exists()
                or self.selector.current() == operation.selection
            )
            result = operation.model_copy(
                update={
                    "state": "recovery_required" if interrupted else "failed",
                    "error_code": code,
                }
            )
            self._save(result)
            if not interrupted:
                self._clear_pending()

    async def close(self) -> None:
        """Finish accepted local operations and reap the runtime before releasing control."""
        self.closed = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await finish_runtime_work(self.close_task)

    async def _close(self) -> None:
        async with self.guard:
            try:
                if self.work is not None:
                    await finish_runtime_work(self.work)
            finally:
                try:
                    await finish_runtime_work(asyncio.create_task(self._stop_service()))
                finally:
                    if self.lock is not None:
                        self.lock.close()
                        self.lock = None
