"""Local setup resolves an installed ID and executes only verified publisher bytes."""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from typing import Literal, NoReturn

import pytest

from skulk.extensions.local_setup import manage_installed_plugin, setup_installed_plugin
from skulk.extensions.runtime_attachment import HostSettings, ServiceConnection
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_selection import RuntimeSelector
from skulk.extensions.tests.test_runtime_install import artifacts


class ExecutedError(Exception):
    """Stop the test at the process replacement boundary after checking arguments."""


@pytest.mark.parametrize("action", ["setup", "manage"])
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "disabled",
        "missing_entry",
        "damaged",
        "history",
        "profile",
        "contention",
        "busy",
        "selection_changed",
        "cancelled",
    ],
)
async def test_local_setup_verifies_before_process_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    action: Literal["setup", "manage"],
) -> None:
    """Real offline fixture staging covers selection, integrity and retained authority."""
    command = setup_installed_plugin if action == "setup" else manage_installed_plugin
    manager = tmp_path / "service"
    private_directory(manager)
    root = manager / "installations/managed.example"
    source = tmp_path / "source"
    metadata, trust, host = artifacts(
        source,
        setup_source=None if fault == "missing_entry" else "print('setup')\n",
        management_source=None if fault == "missing_entry" else "print('manage')\n",
    )
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    monkeypatch.setattr(
        "skulk.extensions.local_setup.SKULK_CONFIG_HOME", tmp_path / "config"
    )
    profile = "a" * 32
    write_private(
        manager / "host.json",
        HostSettings(profile_id=profile, transport_node_id="local")
        .model_dump_json()
        .encode(),
    )
    write_private(
        tmp_path / "config/managed-service/connection.json",
        ServiceConnection(manager_root=str(manager), profile_id=profile)
        .model_dump_json()
        .encode(),
    )
    selector = RuntimeSelector(root)
    write_private(root / "publisher-trust.json", trust.model_dump_json().encode())
    staged = await selector.installer.stage(metadata, source)
    await selector.activate(staged.runtime_digest, expected_revision=0)
    if fault == "disabled":
        selector.disable(expected_revision=1)
    elif fault == "damaged":
        write_private(
            root / "generations" / staged.runtime_digest / "artifacts/bundle.pyz",
            b"damaged",
        )
    elif fault == "history":
        with sqlite3.connect(selector.installer.database) as database:
            database.execute("DELETE FROM trust_floor")
    elif fault == "profile":
        write_private(
            manager / "host.json",
            HostSettings(profile_id="b" * 32, transport_node_id="local")
            .model_dump_json()
            .encode(),
        )
    executed = False

    def replace_process(
        path: str, arguments: tuple[str, ...], environment: dict[str, str]
    ) -> NoReturn:
        nonlocal executed
        executed = True
        assert path == str(
            root / "generations" / staged.runtime_digest / "runtime/bin/python"
        )
        assert arguments[1:4] == ("-I", "-B", "-c")
        assert f"run_module('__{action}__'" in arguments[4]
        assert arguments[-2:] == ("--example-input", "$(inert)")
        assert "PYTHONPATH" not in environment
        with pytest.raises(BlockingIOError):
            RuntimeLock(selector.installer.installer)
        raise ExecutedError

    monkeypatch.setattr(os, "execve", replace_process)
    held = None
    if fault in ("contention", "busy", "selection_changed", "cancelled"):
        held = RuntimeLock(selector.installer.installer)
        monkeypatch.setattr("skulk.extensions.runtime_install._OWNERSHIP_TIMEOUT", 0.25)
        if fault == "contention":
            asyncio.get_running_loop().call_later(0.05, held.close)
        elif fault == "selection_changed":
            selected = selector.current()
            assert selected is not None
            # Publish a different selection after discovery, while ownership is busy.
            replacement = (
                selected.model_copy(update={"revision": selected.revision + 1})
                .model_dump_json()
                .encode()
            )
            asyncio.get_running_loop().call_later(
                0.05, write_private, root / "runtime-selection.json", replacement
            )
            asyncio.get_running_loop().call_later(0.06, held.close)
    if fault in ("none", "disabled", "contention"):
        with pytest.raises(ExecutedError):
            await command("managed.example", ("--example-input", "$(inert)"))
        assert executed
    elif fault == "busy":
        with pytest.raises(TimeoutError):
            await command("managed.example", ())
        assert not executed
        with pytest.raises(BlockingIOError):
            RuntimeLock(selector.installer.installer)
    elif fault == "cancelled":
        task = asyncio.create_task(command("managed.example", ()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not executed
        with pytest.raises(BlockingIOError):
            RuntimeLock(selector.installer.installer)
    else:
        with pytest.raises((OSError, ValueError)):
            await command("managed.example", ())
        assert not executed
    if held is not None:
        held.close()
    RuntimeLock(selector.installer.installer).close()
    if fault == "history":
        with sqlite3.connect(selector.installer.database) as database:
            assert database.execute("SELECT COUNT(*) FROM trust_floor").fetchone() == (
                0,
            )
    assert read_private(root / "runtime-selection.json")


@pytest.mark.parametrize(
    "identifier", ["../other", "/tmp/foreign", "managed.a/../../b", "python"]
)
async def test_local_setup_refuses_paths_before_discovery(identifier: str) -> None:
    """Only a managed installation identity is accepted, never a path or command."""
    with pytest.raises(ValueError):
        await setup_installed_plugin(identifier, ())


@pytest.mark.parametrize("action", ["setup", "manage"])
async def test_setup_exec_keeps_terminal_io_and_generation_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: Literal["setup", "manage"]
) -> None:
    """Execute a real signed offline entrypoint and check inherited lock ownership."""
    manager = tmp_path / "service"
    private_directory(manager)
    root = manager / "installations/managed.example"
    config = tmp_path / "config"
    source = tmp_path / "source"
    entrypoint = """import fcntl,sys
from pathlib import Path
root=Path(sys.prefix).parents[2]
with (root/'installer/installer.lock').open('rb') as stream:
    try: fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: pass
    else: raise RuntimeError('generation fence lost on exec')
print('setup:'+input(),flush=True)
"""
    metadata, trust, host = artifacts(
        source, setup_source=entrypoint, management_source=entrypoint
    )
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    selector = RuntimeSelector(root)
    write_private(root / "publisher-trust.json", trust.model_dump_json().encode())
    staged = await selector.installer.stage(metadata, source)
    await selector.activate(staged.runtime_digest, expected_revision=0)
    write_private(
        manager / "host.json",
        HostSettings(profile_id="a" * 32, transport_node_id="local")
        .model_dump_json()
        .encode(),
    )
    write_private(
        config / "managed-service/connection.json",
        ServiceConnection(manager_root=str(manager), profile_id="a" * 32)
        .model_dump_json()
        .encode(),
    )
    script = """import asyncio,sys
from pathlib import Path
from skulk.extensions import local_setup,runtime_install,service_setup
from skulk.extensions.runtime_artifacts import QualifiedHost
local_setup.SKULK_CONFIG_HOME=Path(sys.argv[1])
runtime_install.measure_host=lambda:QualifiedHost('macos-arm64','3.13.13','1.5.2','a'*64)
sys.argv=['skulk-plugin-service',sys.argv[2]+'-plugin','managed.example']
service_setup.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(config),
        action,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(20):
            output, errors = await process.communicate(b"ordinary input\n")
        assert process.returncode == 0, errors.decode()
        assert output == b"setup:ordinary input\n"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    RuntimeLock(selector.installer.installer).close()
