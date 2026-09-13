"""One local owner command for independent managed-plugin system services."""

import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import stat
import sys
import time
import warnings
from pathlib import Path
from typing import Literal, Self, final
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from skulk.extensions import service_bootstrap, service_registration
from skulk.extensions.local_setup import manage_installed_plugin, setup_installed_plugin
from skulk.extensions.runtime_artifacts import Digest, measure_host
from skulk.extensions.runtime_attachment import (
    HostSettings,
    ProfileIdentifier,
    ServiceConnection,
)
from skulk.extensions.runtime_files import RuntimeLock, read_private, write_private
from skulk.extensions.runtime_install import finish_runtime_work
from skulk.extensions.runtime_manager import (
    MANAGER_REQUEST,
    InventoryRequest,
    ManagerRequest,
    manager_request,
)
from skulk.extensions.service_registration import ServiceLayout, local_layout
from skulk.extensions.service_snapshot import (
    ServiceSnapshot,
    activate_service_runtime,
    service_source_identity,
    stage_service_runtime,
)
from skulk.extensions.terminal_install import TerminalInstaller
from skulk.shared.constants import SKULK_CONFIG_HOME

_READINESS_WAIT_SECONDS = 60.0


class SetupOperation(BaseModel):
    """Retained local installation progress, independent of terminal lifetime."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    operation_id: ProfileIdentifier = Field(
        description="Generated local setup operation identity."
    )
    profile_id: ProfileIdentifier = Field(
        description="Stable generated local service profile."
    )
    skulk_build_sha256: Digest = Field(
        description="Measured source core build for this setup."
    )
    source_sha256: Digest = Field(
        description="Exact source Python/core/dependency inventory identity."
    )
    configuration_directory: str = Field(
        max_length=4096, description="Existing local Skulk configuration directory."
    )
    phase: Literal["preparing", "staged", "selected", "registered", "ready"] = Field(
        description="Last durable completed local setup stage."
    )
    snapshot: ServiceSnapshot | None = Field(
        default=None,
        description="Exact verified staged manager runtime, when available.",
    )

    @model_validator(mode="after")
    def complete_stage(self) -> Self:
        """Bind completed preparation to the exact core being installed."""
        if self.phase != "preparing" and self.snapshot is None:
            raise ValueError("setup stage is missing its runtime")
        if (
            self.snapshot is not None
            and self.snapshot.skulk_build_sha256 != self.skulk_build_sha256
        ):
            raise ValueError("setup core identity differs")
        return self


@final
class ServiceReadinessPendingError(Exception):
    """Report successful registration whose management readiness is not yet proven."""

    def __init__(self, operation_id: str) -> None:
        """Retain only the local operation identity for safe terminal diagnostics."""
        super().__init__(operation_id)
        self.operation_id = operation_id


def _save(root: Path, operation: SetupOperation) -> None:
    raw = operation.model_dump_json().encode()
    write_private(root / "setup-operations" / (operation.operation_id + ".json"), raw)
    write_private(root / "setup.json", raw)


def _outside_checkout(path: Path) -> None:
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError(
            "service interpreter and Skulk configuration must be outside Git checkouts"
        )


async def _elevate(
    layout: ServiceLayout, action: Literal["prepare", "stop", "install"]
) -> None:
    # The only elevated Python entry is a fixed standard-library-only file.
    # Provider code, dependency preparation and all state activation remain here,
    # running as the existing nonroot Skulk owner.
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo",
        str(Path(sys.executable).resolve(strict=True)),
        "-I",
        "-S",
        "-B",
        str(Path(service_registration.__file__).resolve(strict=True)),
        action,
        "--uid",
        str(layout.user_id),
    )
    if await process.wait() != 0:
        raise ValueError("explicit local service registration did not complete")


async def _observe(root: Path) -> bool:
    try:
        result = await manager_request(root, InventoryRequest())
        return "result" in result and "error" not in result
    except (OSError, ValueError, TimeoutError):
        return False


def _ready_installation(
    layout: ServiceLayout, snapshot: ServiceSnapshot, base: Path
) -> bool:
    try:
        selected = service_bootstrap.document(
            read_private(layout.root / "core-runtime.json")
        )
        if selected != {
            "generation": snapshot.generation,
            "manifest_sha256": snapshot.manifest_sha256,
        }:
            return False
        service_bootstrap.verified_runtime(layout.root)
        descriptor = os.open(layout.unit, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                return False
            return source.read(65537) == layout.definition(base)
    except (OSError, ValueError):
        return False


async def setup_service() -> SetupOperation:
    """Prepare, register and verify the owner's fixed system service without paid work.

    Rerunning resumes a retained staged runtime. A verified healthy registration
    is completed without elevation or stopping it. Preparation uses the existing
    qualified core environment read-only; local sudo only provisions its parent
    directory and installs/stops/starts the fixed nonroot OS service definition.
    """
    if os.geteuid() == 0 or os.geteuid() != os.getuid():
        raise ValueError("run setup as the existing Skulk owner, not under sudo")
    layout = local_layout(os.getuid())
    base = Path(sys.executable).resolve(strict=True)
    configuration = SKULK_CONFIG_HOME.resolve()
    _outside_checkout(base)
    _outside_checkout(configuration)
    host = await asyncio.to_thread(measure_host)
    source_identity = await asyncio.to_thread(service_source_identity)
    # First installation needs a root-owned parent before the owner can lock its
    # state. Existing healthy registrations can be verified without another sudo
    # prompt; all repair paths still validate that parent through the fixed helper.
    try:
        root_info = layout.root.lstat()
    except FileNotFoundError:
        prepared = True
    else:
        # Interrupted initial preparation may leave an empty root-owned leaf.
        # The privileged helper must adopt or reject it before owner-side locking.
        prepared = (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or bool(root_info.st_mode & 0o077)
        )
    if prepared:
        await finish_runtime_work(asyncio.create_task(_elevate(layout, "prepare")))
    lock = RuntimeLock(layout.root, "setup.lock")
    try:

        async def install() -> SetupOperation:
            try:
                operation = SetupOperation.model_validate_json(
                    read_private(layout.root / "setup.json")
                )
            except FileNotFoundError:
                operation = None
            try:
                settings = HostSettings.model_validate_json(
                    read_private(layout.root / "host.json")
                )
            except FileNotFoundError:
                settings = None
            profile_id = settings.profile_id if settings is not None else None
            if operation is not None:
                if operation.configuration_directory != str(configuration):
                    raise ValueError(
                        "this service is already bound to another Skulk configuration"
                    )
                if profile_id is not None and operation.profile_id != profile_id:
                    raise ValueError("setup profile differs from service identity")
                profile_id = operation.profile_id
                if (
                    operation.skulk_build_sha256 != host.skulk_build_sha256
                    or operation.source_sha256 != source_identity
                ):
                    # A corrected local build must be able to replace failed
                    # setup. Preserve its journal/profile and stage a complete
                    # new copy before touching any selected or running manager.
                    operation = None
            profile_id = profile_id or uuid4().hex
            connection = ServiceConnection(
                manager_root=str(layout.root), profile_id=profile_id
            )
            configuration.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = configuration.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                raise ValueError(
                    "Skulk configuration must be owned by this account and not writable by others"
                )
            # Existing Skulk configuration commonly uses 0755. Keep its permissions
            # intact and create a private child for managed-service connection data.
            connection_path = configuration / "managed-service" / "connection.json"
            try:
                previous = ServiceConnection.model_validate_json(
                    read_private(connection_path)
                )
            except FileNotFoundError:
                previous = None
            if previous is not None and previous != connection:
                raise ValueError("Skulk already has a different local service profile")
            if operation is None:
                operation = SetupOperation(
                    operation_id=uuid4().hex,
                    profile_id=profile_id,
                    skulk_build_sha256=host.skulk_build_sha256,
                    source_sha256=source_identity,
                    configuration_directory=str(configuration),
                    phase="preparing",
                )
                _save(layout.root, operation)
            # A live manager alone proves neither boot registration nor integrity
            # of its selected copy. Verify both before treating setup as complete.
            if (
                operation.phase in {"registered", "ready"}
                and operation.snapshot is not None
                and await asyncio.to_thread(
                    _ready_installation, layout, operation.snapshot, base
                )
                and await _observe(layout.root)
            ):
                write_private(connection_path, connection.model_dump_json().encode())
                if operation.phase == "registered":
                    operation = operation.model_copy(update={"phase": "ready"})
                    _save(layout.root, operation)
                return operation
            if not prepared:
                await _elevate(layout, "prepare")
            if operation.snapshot is None:
                print("Preparing verified independent manager runtime...", flush=True)
                snapshot = await stage_service_runtime(layout.root)
                if (
                    snapshot.skulk_build_sha256 != host.skulk_build_sha256
                    or await asyncio.to_thread(service_source_identity)
                    != source_identity
                ):
                    raise ValueError("source environment changed during local setup")
                operation = operation.model_copy(
                    update={"snapshot": snapshot, "phase": "staged"}
                )
                _save(layout.root, operation)
            assert operation.snapshot is not None
            print("Registering nonroot system service...", flush=True)
            await _elevate(layout, "stop")
            # The selector repeats complete seal verification with the manager
            # stopped; failed activation retains every prior generation/state file.
            await asyncio.to_thread(
                activate_service_runtime, layout.root, operation.snapshot
            )
            with_owner = RuntimeLock(layout.root, "manager.lock")
            try:
                try:
                    current = HostSettings.model_validate_json(
                        read_private(layout.root / "host.json")
                    )
                except FileNotFoundError:
                    current = HostSettings(transport_node_id="unattached." + profile_id)
                if current.profile_id not in (None, profile_id):
                    raise ValueError("service profile changed during preparation")
                write_private(
                    layout.root / "host.json",
                    current.model_copy(update={"profile_id": profile_id})
                    .model_dump_json()
                    .encode(),
                )
                write_private(connection_path, connection.model_dump_json().encode())
            finally:
                with_owner.close()
            operation = operation.model_copy(update={"phase": "selected"})
            _save(layout.root, operation)
            await _elevate(layout, "install")
            operation = operation.model_copy(update={"phase": "registered"})
            _save(layout.root, operation)
            deadline = time.monotonic() + _READINESS_WAIT_SECONDS
            while not await _observe(layout.root):
                if time.monotonic() >= deadline:
                    raise ServiceReadinessPendingError(operation.operation_id)
                await asyncio.sleep(0.5)
            operation = operation.model_copy(update={"phase": "ready"})
            _save(layout.root, operation)
            return operation

        return await finish_runtime_work(asyncio.create_task(install()))
    finally:
        lock.close()


async def service_status() -> dict[str, str | bool]:
    """Distinguish historical setup completion from current management and integrity."""
    layout = local_layout(os.getuid())
    operation = SetupOperation.model_validate_json(
        read_private(layout.root / "setup.json")
    )
    verified = operation.snapshot is not None and await asyncio.to_thread(
        _ready_installation,
        layout,
        operation.snapshot,
        Path(sys.executable).resolve(strict=True),
    )
    available = await _observe(layout.root)
    return {
        "operation_id": operation.operation_id,
        "last_setup_phase": operation.phase,
        "registered_runtime_verified": verified,
        "management_available": available,
        "error_code": (
            "registration_or_integrity_unavailable"
            if not verified
            else "service_unavailable"
            if not available
            else "none"
        ),
    }


class _ServiceArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.action: str = ""
        self.setup_arguments: list[str] = []


def read_hidden_credential(prompt: str) -> str:
    """Refuse a terminal that cannot disable echo before reading a credential."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main() -> None:
    """Run one explicit local setup command; remote management never invokes sudo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "setup",
            "status",
            "manage",
            "setup-plugin",
            "manage-plugin",
            "install-plugin",
        ),
    )
    parser.add_argument("setup_arguments", nargs=argparse.REMAINDER)
    arguments = _ServiceArguments()
    _ = parser.parse_args(namespace=arguments)
    try:
        remaining = TypeAdapter[list[str]](list[str]).validate_python(
            arguments.setup_arguments
        )
        action = TypeAdapter[str](str).validate_python(arguments.action)
        if action == "install-plugin":
            if (
                os.geteuid() == 0
                or os.geteuid() != os.getuid()
                or not sys.stdin.isatty()
                or len(remaining) > 1
            ):
                raise ValueError(
                    "guided installation requires the nonroot owner terminal"
                )
            connection = ServiceConnection.model_validate_json(
                read_private(
                    SKULK_CONFIG_HOME / "managed-service/connection.json", 8192
                )
            )

            async def install() -> None:
                async def request(value: ManagerRequest) -> dict[str, JsonValue]:
                    return await manager_request(Path(connection.manager_root), value)

                def output(message: str) -> None:
                    print(message, flush=True)

                _ = await TerminalInstaller(
                    request, input, read_hidden_credential, output
                ).run(remaining[0] if remaining else None)

            asyncio.run(install())
            return
        if action in {"setup-plugin", "manage-plugin"}:
            if not remaining:
                raise ValueError("plugin command requires an installed plugin ID")
            fields = remaining[1:]
            if fields[:1] == ["--"]:
                fields = fields[1:]
            command = (
                setup_installed_plugin
                if action == "setup-plugin"
                else manage_installed_plugin
            )
            asyncio.run(command(remaining[0], tuple(fields)))
            return
        if remaining:
            raise ValueError("this service action accepts no additional arguments")
        if action == "setup":
            operation = asyncio.run(setup_service())
            print(
                json.dumps(
                    {"operation_id": operation.operation_id, "phase": operation.phase}
                )
            )
        elif action == "manage":
            if os.geteuid() == 0:
                raise ValueError("plugin management requires the nonroot service owner")
            connection = ServiceConnection.model_validate_json(
                read_private(
                    SKULK_CONFIG_HOME / "managed-service" / "connection.json", 8192
                )
            )
            raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise ValueError("management request exceeds bound")
            request = MANAGER_REQUEST.validate_json(raw)
            print(
                json.dumps(
                    asyncio.run(manager_request(Path(connection.manager_root), request))
                )
            )
        else:
            print(json.dumps(asyncio.run(service_status())))
    except ServiceReadinessPendingError as error:
        print(
            json.dumps(
                {
                    "operation_id": error.operation_id,
                    "phase": "registered",
                    "error_code": "service_readiness_pending",
                }
            )
        )
        print(
            "System service registered; management readiness is still pending. "
            "Inspect skulk-plugin-service status. Once runtime verification and "
            "management availability pass, rerun the same setup command to finish "
            "this operation without restarting the service or requesting sudo.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (EOFError, KeyboardInterrupt):
        print(
            "Terminal closed. Accepted local operations remain recorded; inspect retained status before retrying. For guided installation, use the printed resume command.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, ValueError, TimeoutError, sqlite3.Error, getpass.GetPassWarning):
        print(
            "Guided installation incomplete. Use the printed resume command with its installation ID; inspect retained status before recovery."
            if arguments.action == "install-plugin"
            else "Plugin service command incomplete. Rerun the same local command with the qualified Skulk environment; inspect protected setup and OS service status.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
