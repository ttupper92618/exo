"""Late local setup and live manager membership without API process restart."""

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from skulk.extensions import CapabilityDescriptor, LoadedExtensions
from skulk.extensions.managed import ManagedConnection, ManagedOwner
from skulk.extensions.managed_attachment import ManagedAttachment
from skulk.extensions.managed_services import ManagedServices
from skulk.extensions.runtime_artifacts import QualifiedHost
from skulk.extensions.runtime_attachment import HostSettings, ServiceConnection
from skulk.extensions.runtime_controller import LifecycleRequest
from skulk.extensions.runtime_download import ReleaseSource
from skulk.extensions.runtime_files import RuntimeLock, read_private, write_private
from skulk.extensions.runtime_manager import (
    InstallationRequest,
    InventoryRequest,
    ReleaseRequest,
    RuntimeManager,
    SubmitRequest,
    manager_request,
)
from skulk.extensions.tests.test_managed import Dynamic
from skulk.extensions.tests.test_runtime_install import artifacts
from skulk.extensions.tests.test_runtime_service import OWNER_SOURCE, running
from skulk.extensions.tests.test_steward_tools import context

PROFILE = "1" * 32
HOST = QualifiedHost("macos-arm64", "3.13.13", "1.5.2", "a" * 64)


def manager_fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeManager:
    """Create an isolated real local manager, without any private SDK or provider call."""
    write_private(
        root / "host.json",
        HostSettings(transport_node_id="old-peer", profile_id=PROFILE)
        .model_dump_json()
        .encode(),
    )
    monkeypatch.setattr("skulk.extensions.runtime_manager.measure_host", lambda: HOST)
    monkeypatch.setattr(
        "skulk.extensions.managed_attachment.measure_host", lambda: HOST
    )
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: HOST)
    return RuntimeManager(root)


def connect(path: Path, root: Path) -> None:
    """Publish the exact generated local setup connection atomically."""
    write_private(
        path,
        ServiceConnection(manager_root=str(root), profile_id=PROFILE)
        .model_dump_json()
        .encode(),
    )


async def test_late_setup_activation_outage_and_shutdown_preserve_independent_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Membership changes reach existing Fabric/configuration lookup without a restart."""
    root, path = (
        tmp_path / "manager",
        tmp_path / "config/managed-service/connection.json",
    )
    manager = manager_fixture(root, monkeypatch)
    await manager.start()
    services = ManagedServices(path)
    registry = LoadedExtensions([], managed_services=services).with_builtin_extensions(
        []
    )
    host_context = replace(context(), skulk_version="1.5.2")
    registry.run_startup_hooks(host_context)
    descriptor = CapabilityDescriptor(
        id="fixture",
        version="1.0.0",
        title="Fixture",
        description="Inert fixture",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )

    async def description(
        self: ManagedOwner, message: dict[str, JsonValue], *, timeout: float = 30
    ) -> dict[str, JsonValue]:
        return {
            "transport_node_id": "test-node",
            "nodes": [
                {
                    "node_id": "durable-node",
                    "bundle_id": "fixture",
                    "version": "1.0.0",
                    "status": "ready",
                    "configurable": True,
                    "descriptors": [descriptor.model_dump(mode="json")],
                }
            ],
        }

    monkeypatch.setattr(ManagedOwner, "_request", description)
    try:
        assert not registry.configuration_providers
        with pytest.raises(FileNotFoundError):
            await services.refresh()
        connect(path, root)
        await services.request(
            InstallationRequest(action="register", plugin_id="managed.fixture")
        )
        await services.refresh()
        assert registry.names == ["managed.fixture"]
        owner = services.owners["managed.fixture"]
        assert registry.configuration_providers["managed.fixture"] is owner
        await owner.refresh()
        assert owner.manager_enabled is None
        assert (
            not registry.capability_descriptors
        )  # Empty registration is not admission.
        controller = manager.controllers["managed.fixture"]
        metadata, trust, _ = artifacts(tmp_path / "source", owner_source=OWNER_SOURCE)
        write_private(
            controller.root / "publisher-trust.json", trust.model_dump_json().encode()
        )
        staged = await controller.selector.installer.stage(
            metadata, tmp_path / "source"
        )
        await services.request(
            SubmitRequest(
                plugin_id="managed.fixture",
                request=LifecycleRequest(
                    operation_id="2" * 32,
                    action="activate",
                    expected_revision=0,
                    runtime_digest=staged.runtime_digest,
                ),
            )
        )
        assert controller.work is not None
        await controller.work
        assert controller.service is not None
        await running(controller.service)
        await services.refresh()
        await owner.refresh()
        assert registry.capability_descriptors == (descriptor,)
        assert registry.call_handler(descriptor.qualified_id) is not None
        assert owner.manager_enabled is True
        replacement = Dynamic("replacement")
        replacement.snapshot = (descriptor,)
        competing = LoadedExtensions([replacement], managed_services=services)
        assert not competing.capability_descriptors
        await manager.close()
        with pytest.raises((OSError, ValueError)):
            await services.refresh()
        assert owner.manager_enabled is None
        assert not competing.capability_descriptors
        owner.available = (
            True  # Even a late successful child observation cannot restore admission.
        )
        assert not registry.capability_descriptors
        manager = RuntimeManager(root)
        await manager.start()
        assert manager.boot is not None
        await manager.boot
        controller = manager.controllers["managed.fixture"]
        assert controller.service is not None
        await running(controller.service)
        await services.refresh()
        await owner.refresh()
        assert services.owners["managed.fixture"] is owner
        assert owner.manager_enabled is True
        assert registry.capability_descriptors == (descriptor,)
        cached_nodes = owner.nodes
        await services.request(
            SubmitRequest(
                plugin_id="managed.fixture",
                request=LifecycleRequest(
                    operation_id="3" * 32,
                    action="disable",
                    expected_revision=1,
                ),
            )
        )
        assert controller.work is not None
        await controller.work
        await services.refresh()
        assert owner.manager_enabled is False
        assert owner.nodes == cached_nodes
        assert registry.configuration_providers["managed.fixture"] is owner
        assert not registry.capability_descriptors
        assert competing.capability_descriptors == (descriptor,)
        entry = competing.call_handler(descriptor.qualified_id)
        assert entry is not None and entry[1] is replacement
        selection_path = controller.root / "runtime-selection.json"
        original_selection = read_private(selection_path)
        for missing in (False, True):
            if missing:
                selection_path.unlink()
            else:
                write_private(selection_path, b"invalid selection")
            try:
                await services.refresh()
                assert owner.manager_enabled is None
                assert owner.nodes == cached_nodes
                assert not competing.capability_descriptors
                assert competing.call_handler(descriptor.qualified_id) is None
            finally:
                write_private(selection_path, original_selection)
            await services.refresh()
            assert owner.manager_enabled is False
            assert competing.capability_descriptors == (descriptor,)
        # A lost manager observation cannot authorize transferring ownership.
        await manager.close()
        with pytest.raises((OSError, ValueError)):
            await services.refresh()
        assert owner.manager_enabled is None
        assert not competing.capability_descriptors
        manager = RuntimeManager(root)
        await manager.start()
        assert manager.boot is not None
        await manager.boot
        await services.refresh()
        assert owner.manager_enabled is False
        assert competing.capability_descriptors == (descriptor,)
        await registry.run_shutdown_hooks()
        assert not registry.capability_descriptors
        assert "result" in await manager_request(root, InventoryRequest())
        with pytest.raises(BlockingIOError):
            RuntimeLock(root, "manager.lock")
        RuntimeLock(root, "attachment.lock").close()
        assert (
            HostSettings.model_validate_json(
                read_private(root / "host.json")
            ).transport_node_id
            == "test-node"
        )
    finally:
        await registry.run_shutdown_hooks()
        await manager.close()


async def test_shared_adapter_concurrent_shutdown_releases_attachment_once(
    tmp_path: Path,
) -> None:
    """Legacy and manager registry shutdown cannot underflow their shared bridge."""
    attachment = ManagedAttachment(tmp_path, PROFILE)
    owner = ManagedOwner(
        ManagedConnection(plugin_id="managed.fixture", state_root=str(tmp_path)),
        attachment=attachment,
    )
    attachment.retain("test-node")
    attachment.lock = RuntimeLock(tmp_path, "attachment.lock")

    async def observer() -> None:
        await asyncio.Event().wait()

    owner.poll_task = asyncio.create_task(observer())
    await asyncio.gather(owner.on_stop(), owner.on_stop())
    assert attachment.users == 0
    assert attachment.lock is None
    RuntimeLock(tmp_path, "attachment.lock").close()


async def test_slow_release_inspection_does_not_block_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both API attachment and manager inventory remain responsive during source HTTPS."""
    root, path = tmp_path / "manager", tmp_path / "connection.json"
    manager = manager_fixture(root, monkeypatch)
    await manager.start()
    connect(path, root)
    services = ManagedServices(path)
    services.on_start(replace(context(), skulk_version="1.5.2"))
    started, release = asyncio.Event(), asyncio.Event()
    operation: asyncio.Task[dict[str, JsonValue]] | None = None
    try:
        await services.request(
            InstallationRequest(action="register", plugin_id="managed.fixture")
        )
        downloads = manager.downloads["managed.fixture"]
        metadata, trust, _ = artifacts(tmp_path / "source")
        write_private(
            downloads.root / "publisher-trust.json", trust.model_dump_json().encode()
        )
        write_private(
            downloads.root / "release-source.json",
            ReleaseSource(
                revision=1,
                base_url="https://release.example.test/",
                metadata_filename="runtime.json",
            )
            .model_dump_json()
            .encode(),
        )

        async def delayed(_: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, content=metadata)

        downloads.transport = httpx.MockTransport(delayed)
        operation = asyncio.create_task(
            services.request(
                ReleaseRequest(action="inspect_release", plugin_id="managed.fixture")
            )
        )
        async with asyncio.timeout(5):
            await started.wait()
            assert services.attachment is not None
            services.attachment.observed = 0
            inventory = await services.refresh()
            assert len(inventory.installations) == 1
            assert not operation.done()
        release.set()
        assert "runtime_digest" in await operation
    finally:
        release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await services.on_stop()
        await manager.close()
