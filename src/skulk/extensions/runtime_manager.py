"""Fixed owner-local manager socket for installed plugin lifecycle operations."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import Annotated, Literal, cast, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from skulk.extensions.runtime_artifacts import measure_host
from skulk.extensions.runtime_attachment import (
    AttachmentJournal,
    AttachmentRequest,
    HostSettings,
    OwnerBinding,
    finish_attachment,
    recover_attachment,
)
from skulk.extensions.runtime_controller import (
    LifecycleOperation,
    LifecycleRequest,
    RuntimeController,
)
from skulk.extensions.runtime_download import (
    InstallRequest,
    RuntimeDownloads,
    SourceUpdate,
)
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_install import finish_runtime_work
from skulk.extensions.runtime_service import RuntimeServiceStatus

PluginIdentifier = Annotated[
    str, Field(pattern=r"^managed\.[a-z0-9][a-z0-9._-]{0,80}$")
]
_PLUGIN_ID: TypeAdapter[str] = TypeAdapter(PluginIdentifier)
_OBJECT = TypeAdapter(dict[str, JsonValue])


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class InventoryRequest(_Request):
    """Read registered installations even when their runtime is unavailable."""

    action: Literal["list"] = "list"


class InstallationRequest(_Request):
    """Register an empty installation or inspect its current desired state."""

    action: Literal["register", "get"]
    plugin_id: PluginIdentifier = Field(
        description="Stable local installation ID; never a path."
    )


class SubmitRequest(_Request):
    """Submit exact local lifecycle intent to its independently owned controller."""

    action: Literal["submit"] = "submit"
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")
    request: LifecycleRequest = Field(
        description="Revision-fenced nonbillable lifecycle request."
    )


class OperationRequest(_Request):
    """Read or explicitly recover one retained local operation."""

    action: Literal["operation", "recover"] = "operation"
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$", description="Previously submitted operation ID."
    )


class ReleaseRequest(_Request):
    """Inspect the configured signed release or read current installation progress."""

    action: Literal["inspect_release", "install_status", "source_status"]
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")


class InstallSubmission(_Request):
    """Download and stage an exact reviewed runtime without activating it."""

    action: Literal["install"] = "install"
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")
    request: InstallRequest = Field(description="Immutable reviewed release intent.")


class SourceRegistration(_Request):
    """Direct owner source/trust provisioning, never an ordinary remote management grant."""

    action: Literal["configure_source"] = "configure_source"
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")
    request: SourceUpdate = Field(
        description="Owner-reviewed source, trust and write-only credential."
    )


class InstallRecoveryRequest(_Request):
    """Explicitly recover one original installation using the reviewed current source."""

    action: Literal["recover_install"] = "recover_install"
    plugin_id: PluginIdentifier = Field(description="Exact registered installation.")
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$", description="Original installation operation ID."
    )
    expected_source_revision: int = Field(
        ge=1,
        description="Reviewed current source revision, including any credential rotation.",
    )


type ManagerRequest = (
    InventoryRequest
    | InstallationRequest
    | SubmitRequest
    | OperationRequest
    | AttachmentRequest
    | ReleaseRequest
    | InstallSubmission
    | SourceRegistration
    | InstallRecoveryRequest
)
MANAGER_REQUEST: TypeAdapter[ManagerRequest] = TypeAdapter(ManagerRequest)


def manager_socket(root: Path) -> Path:
    """Derive a protected short Unix socket address from the local service root."""
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    directory = Path("/tmp") / f"skulk-manager-{os.getuid()}-{digest}"
    private_directory(directory)
    return directory / "manager.sock"


@final
class RuntimeManager:
    """Supervise bounded local installations independently of Skulk's API lifetime."""

    def __init__(self, root: Path) -> None:
        """Read only the fixed local host binding and prepare owner-only storage."""
        if os.geteuid() == 0:
            raise ValueError("plugin manager must run without root")
        private_directory(root)
        self.root = root.resolve()
        self.settings = HostSettings.model_validate_json(
            read_private(self.root / "host.json")
        )
        self.installations = self.root / "installations"
        private_directory(self.installations)
        self.path = manager_socket(self.root)
        self.controllers: dict[str, RuntimeController] = {}
        self.downloads: dict[str, RuntimeDownloads] = {}
        self.errors: dict[str, str] = {}
        self.server: asyncio.Server | None = None
        self.lock: RuntimeLock | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.boot: asyncio.Task[None] | None = None
        self.guard = asyncio.Lock()
        self.closed = False
        self.close_task: asyncio.Task[None] | None = None

    def _identifiers(self) -> list[str]:
        names = sorted(path.name for path in self.installations.iterdir())
        if len(names) > 16:
            raise ValueError("installation count exceeds bound")
        return [_PLUGIN_ID.validate_python(name, strict=True) for name in names]

    async def start(self) -> None:
        """Expose management before starting possibly broken installed runtimes."""
        if self.lock is not None or self.closed:
            raise ValueError("manager lifetime already used")
        self.lock = RuntimeLock(self.root, "manager.lock")
        try:
            self.path.unlink(missing_ok=True)
            self.server = await asyncio.start_unix_server(
                self.accept, path=self.path, limit=16385
            )
            self.path.chmod(0o600)
            self.boot = asyncio.create_task(self._restore())
        except BaseException:
            await self.close()
            raise

    async def _restore(self) -> None:
        async with self.guard:
            await self._restore_locked()

    async def _restore_locked(self) -> None:
        try:
            self.settings = await asyncio.to_thread(recover_attachment, self.root)
        except (OSError, ValueError):
            for identifier in self._identifiers():
                self.errors[identifier] = "attachment_recovery_required"
            return
        for identifier in self._identifiers():
            await self._load(identifier)

    async def _attach(self, request: AttachmentRequest) -> dict[str, JsonValue]:
        async with self.guard:
            if self.settings.profile_id != request.profile_id:
                raise ValueError("attachment profile differs")
            host = await asyncio.to_thread(measure_host)
            if request.skulk_build_sha256 != host.skulk_build_sha256:
                raise ValueError("live Skulk build differs from manager")
            # The bridge holds this fence for its whole API lifetime. Two local
            # Skulk profiles must not alternately attach this manager to themselves.
            try:
                lock = RuntimeLock(self.root, "attachment.lock")
            except BlockingIOError:
                pass
            else:
                lock.close()
                raise ValueError("attachment requires a live local bridge")
            recovered = await asyncio.to_thread(recover_attachment, self.root)
            self.settings = recovered
            if self.settings.transport_node_id != request.transport_node_id:
                identifiers = self._identifiers()
                # Validate all existing bindings before disturbing a healthy owner.
                for identifier in identifiers:
                    binding = OwnerBinding.model_validate_json(
                        read_private(self.installations / identifier / "owner.json")
                    )
                    if binding != self.settings.owner_binding():
                        raise ValueError("installation transport identity differs")
                results = await asyncio.gather(
                    *(controller.close() for controller in self.controllers.values()),
                    return_exceptions=True,
                )
                self.controllers.clear()
                if any(isinstance(result, BaseException) for result in results):
                    # A completed close task can retain its original failure.
                    # Keep disk/process fences authoritative so a later attempt
                    # can recover after the surviving owner actually exits.
                    for identifier in identifiers:
                        self.errors[identifier] = "attachment_recovery_required"
                    raise ValueError("attachment could not stop all owners")
                journal = AttachmentJournal(
                    previous=self.settings,
                    current=self.settings.model_copy(
                        update={"transport_node_id": request.transport_node_id}
                    ),
                    installations=tuple(identifiers),
                    state="pending",
                )
                try:
                    self.settings = await asyncio.to_thread(
                        finish_attachment, self.root, journal
                    )
                finally:
                    await self._restore_locked()
            else:
                for identifier in self._identifiers():
                    if identifier not in self.controllers:
                        await self._load(identifier)
            return {
                "transport_node_id": self.settings.transport_node_id,
                "skulk_build_sha256": host.skulk_build_sha256,
            }

    async def _load(self, identifier: str) -> None:
        root = self.installations / identifier
        controller: RuntimeController | None = None
        try:
            private_directory(root)
            # A stale or copied binding must never be silently reattached to a
            # different host by a manager restart or repeated register request.
            binding = OwnerBinding.model_validate_json(
                read_private(root / "owner.json")
            )
            if binding != self.settings.owner_binding():
                raise ValueError("installation transport identity differs")
            controller = RuntimeController(root)
            await controller.start()
            if identifier not in self.downloads:
                self.downloads[identifier] = RuntimeDownloads(root)
            self.controllers[identifier] = controller
            self.errors.pop(identifier, None)
        except (OSError, ValueError):
            self.errors[identifier] = "installation_unavailable"
            if controller is not None:
                with contextlib.suppress(OSError, ValueError):
                    await controller.close()

    def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Bound local clients before parsing any request bytes."""
        if self.closed or len(self.tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one bounded typed operation, exposing only sanitized failures."""
        try:
            async with asyncio.timeout(30):
                raw = await reader.readline()
                if len(raw) > 16384:
                    raise ValueError("manager request exceeds bound")
                request = MANAGER_REQUEST.validate_json(raw)
                result = await self.dispatch(request)
                payload = json.dumps({"result": result}).encode() + b"\n"
                if len(payload) > 262144:
                    raise ValueError("manager response exceeds bound")
                writer.write(payload)
                await writer.drain()
        except (OSError, ValueError, TimeoutError):
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(1):
                    writer.write(b'{"error":"manager_operation_refused"}\n')
                    await writer.drain()
        finally:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                # A client that does not consume a response must not hold the
                # manager's shutdown after the request deadline has expired.
                writer.transport.abort()

    def _summary(self, identifier: str) -> dict[str, JsonValue]:
        controller = self.controllers.get(identifier)
        if controller is None:
            return {
                "plugin_id": identifier,
                "error_code": self.errors.get(identifier, "initializing"),
                "service": None,
                "stale": True,
            }
        try:
            selection = controller.selector.current()
            try:
                status = RuntimeServiceStatus.model_validate_json(
                    read_private(controller.root / "service-status.json")
                )
            except FileNotFoundError:
                status = None
            task = controller.service_task
            failed = (
                task is not None
                and task.done()
                and (task.cancelled() or task.exception() is not None)
            )
            stale = (
                status is None
                or controller.service is None
                or status.service_instance != controller.service.instance
                or time.time() - status.observed_at > 90
                or failed
            )
            operation = None
            try:
                if controller.pending.exists():
                    pending = LifecycleOperation.model_validate_json(
                        read_private(controller.pending)
                    )
                    operation = controller.operation(pending.request.operation_id)
                elif selection is not None:
                    operation = controller.operation(selection.operation_id)
            except FileNotFoundError:
                # Older selections created directly by the local selector do not
                # have a controller operation. Never invent an operation to replay.
                pass
            return {
                "plugin_id": identifier,
                "operation_id": operation.request.operation_id if operation else None,
                "operation_state": operation.state if operation else None,
                "error_code": "service_unavailable" if failed else None,
                "selected_digest": selection.runtime_digest if selection else None,
                "selection_revision": selection.revision if selection else 0,
                "enabled": selection.enabled if selection else False,
                "uninstalled": controller.is_uninstalled(selection),
                "service": status.model_dump(mode="json") if status else None,
                "stale": stale,
            }
        except (OSError, ValueError):
            return {
                "plugin_id": identifier,
                "error_code": "installation_unavailable",
                "service": None,
                "stale": True,
            }

    async def dispatch(self, request: ManagerRequest) -> dict[str, JsonValue]:
        """Execute the same fixed operations for terminal and authenticated API callers."""
        if self.closed:
            raise ValueError("manager is closing")
        if isinstance(request, InventoryRequest):
            return {
                "installations": [
                    self._summary(identifier) for identifier in self._identifiers()
                ]
            }
        if isinstance(request, AttachmentRequest):
            return await finish_runtime_work(asyncio.create_task(self._attach(request)))
        if isinstance(request, ReleaseRequest) and request.action == "inspect_release":
            async with self.guard:
                downloads = self.downloads.get(request.plugin_id)
                if request.plugin_id not in self.controllers or downloads is None:
                    raise ValueError("installation is unavailable")
            # A slow release server must not block inventory, attachment renewal
            # or another installation's lifecycle behind the manager-wide lock.
            return (await downloads.inspect()).model_dump(mode="json")
        async with self.guard:
            return await self._dispatch_installation(request)

    async def _dispatch_installation(
        self,
        request: InstallationRequest
        | SubmitRequest
        | OperationRequest
        | ReleaseRequest
        | InstallSubmission
        | SourceRegistration
        | InstallRecoveryRequest,
    ) -> dict[str, JsonValue]:
        identifier = request.plugin_id
        try:
            attachment = AttachmentJournal.model_validate_json(
                read_private(self.root / "attachment.json")
            )
        except FileNotFoundError:
            attachment = None
        if attachment is not None and attachment.state == "pending":
            # Registration must not introduce an owner outside the recorded
            # membership while a partially written attachment is unresolved.
            raise ValueError("attachment recovery is required")
        if isinstance(request, InstallationRequest) and request.action == "register":
            identifiers = self._identifiers()
            if identifier not in identifiers:
                if len(identifiers) >= 16:
                    raise ValueError("installation count exceeds bound")
                root = self.installations / identifier
                private_directory(root)
                write_private(
                    root / "owner.json",
                    self.settings.owner_binding().model_dump_json().encode(),
                )
            if identifier not in self.controllers:
                await self._load(identifier)
            return self._summary(identifier)
        controller = self.controllers.get(identifier)
        if controller is None:
            raise ValueError("installation is unavailable")
        if isinstance(request, InstallRecoveryRequest):
            return (
                await self.downloads[identifier].recover(
                    request.operation_id, request.expected_source_revision
                )
            ).model_dump(mode="json")
        if isinstance(request, SourceRegistration):
            return (
                await self.downloads[identifier].configure(request.request)
            ).model_dump(mode="json")
        if isinstance(request, ReleaseRequest):
            downloads = self.downloads[identifier]
            if request.action == "source_status":
                return downloads.source_status().model_dump(mode="json")
            if request.action == "inspect_release":
                raise ValueError("release inspection requires independent dispatch")
            operation = downloads.current()
            return {
                "operation": operation.model_dump(mode="json") if operation else None
            }
        if isinstance(request, InstallSubmission):
            return (
                await self.downloads[identifier].submit(request.request)
            ).model_dump(mode="json")
        if isinstance(request, InstallationRequest):
            selection = controller.selector.current()
            return {
                "installation": self._summary(identifier),
                "selection": selection.model_dump(mode="json") if selection else None,
            }
        if isinstance(request, SubmitRequest):
            return (await controller.submit(request.request)).model_dump(mode="json")
        operation = (
            await controller.recover(request.operation_id)
            if request.action == "recover"
            else controller.operation(request.operation_id)
        )
        return operation.model_dump(mode="json")

    async def close(self) -> None:
        """Stop new clients, finish accepted work and close every owned runtime."""
        self.closed = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await finish_runtime_work(self.close_task)

    async def _close(self) -> None:
        if self.server is not None:
            self.server.close()
        try:
            if self.tasks:
                await finish_runtime_work(asyncio.create_task(self._finish_clients()))
            if self.boot is not None:
                await finish_runtime_work(self.boot)
        finally:
            try:
                results = await asyncio.gather(
                    *(controller.close() for controller in self.controllers.values()),
                    *(downloads.close() for downloads in self.downloads.values()),
                    return_exceptions=True,
                )
                if any(isinstance(result, BaseException) for result in results):
                    raise ValueError("one or more runtime controllers failed to close")
            finally:
                try:
                    if self.server is not None:
                        await self.server.wait_closed()
                    if self.lock is not None:
                        self.path.unlink(missing_ok=True)
                finally:
                    # A failed socket unlink is diagnosable on the next start;
                    # it must not leak manager ownership after all children close.
                    if self.lock is not None:
                        self.lock.close()
                        self.lock = None

    async def _finish_clients(self) -> None:
        await asyncio.gather(*self.tasks)


async def manager_request(root: Path, request: ManagerRequest) -> dict[str, JsonValue]:
    """Call a protected local manager without retrying any failed submission."""
    path = manager_socket(root)
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("manager socket is not protected")
    async with asyncio.timeout(35):
        reader, writer = await asyncio.open_unix_connection(path, limit=262145)
        try:
            payload = request.model_dump(mode="json")
            if (
                isinstance(request, SourceRegistration)
                and request.request.token is not None
            ):
                # SecretStr redacts diagnostics by default. Only this protected
                # local wire path replaces the redaction with the supplied value.
                payload["request"]["token"] = request.request.token.get_secret_value()
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            payload = await reader.readline()
            if len(payload) > 262144:
                raise ValueError("manager response exceeds bound")
            return _OBJECT.validate_json(payload)
        finally:
            writer.close()
            await writer.wait_closed()


async def _serve(root: Path) -> None:
    manager = RuntimeManager(root)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(shutdown_signal, stopped.set)
    try:
        await manager.start()
        await stopped.wait()
    finally:
        await finish_runtime_work(asyncio.create_task(manager.close()))
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(shutdown_signal)


def main() -> None:
    """Run the fixed service or send one typed JSON request through its local socket."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "call"))
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        root = cast(Path, arguments.root)
        if cast(str, arguments.mode) == "serve":
            asyncio.run(_serve(root))
        else:
            raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise ValueError("manager request exceeds bound")
            result = asyncio.run(
                manager_request(root, MANAGER_REQUEST.validate_json(raw))
            )
            print(json.dumps(result))
            if "error" in result:
                raise SystemExit(1)
    except (OSError, ValueError, TimeoutError):
        print(
            "plugin manager unavailable; inspect local setup and service status",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
