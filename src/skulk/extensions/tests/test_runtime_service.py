"""Actual isolated generic owner launch, trust refusal and bounded process lifetime."""

import asyncio
from pathlib import Path

import pytest

from skulk.extensions.runtime_files import RuntimeLock, read_private, write_private
from skulk.extensions.runtime_selection import RuntimeSelector
from skulk.extensions.runtime_service import RuntimeService, RuntimeServiceStatus
from skulk.extensions.tests.test_runtime_install import artifacts

OWNER_SOURCE = """import argparse,fcntl,os,signal,time
p=argparse.ArgumentParser()
p.add_argument('--root');p.add_argument('--lifetime-fd',type=int)
a=p.parse_args()
f=os.open(a.root+'/supervisor.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
signal.signal(signal.SIGTERM,lambda *args:None)
print('sensitive fixture output',flush=True)
os.read(a.lifetime_fd,1)
os.close(f)
"""


async def installed(
    root: Path, monkeypatch: pytest.MonkeyPatch, source: str = OWNER_SOURCE
) -> RuntimeSelector:
    """Stage a real signed fixture in an empty offline runtime and select it."""
    metadata, trust, host = artifacts(root / "source", owner_source=source)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    selector = RuntimeSelector(root / "installed")
    write_private(
        selector.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    staged = await selector.installer.stage(metadata, root / "source")
    await selector.activate(staged.runtime_digest, expected_revision=0)
    return selector


def status(root: Path) -> RuntimeServiceStatus:
    """Read the protected observation without exposing any child output."""
    return RuntimeServiceStatus.model_validate_json(
        read_private(root / "service-status.json")
    )


async def running(service: RuntimeService) -> None:
    """Wait for actual fixture output, proving entrypoint and ownership acquisition."""
    async with asyncio.timeout(10):
        while service.output_bytes == 0:
            await asyncio.sleep(0.01)
    assert status(service.root).state == "running"


async def test_service_owns_fixed_runtime_and_closes_lifetime_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output is discarded, duplicate launch is fenced and pipe EOF ends the owner."""
    selector = await installed(tmp_path, monkeypatch)
    stopped = asyncio.Event()
    service = RuntimeService(selector.root)
    task = asyncio.create_task(service.serve(stopped))
    try:
        await running(service)
        with pytest.raises(BlockingIOError):
            await RuntimeService(selector.root).serve(asyncio.Event())
        with pytest.raises(BlockingIOError):
            RuntimeLock(selector.root, "supervisor.lock")
        assert b"sensitive" not in read_private(selector.root / "service-status.json")
    finally:
        stopped.set()
        async with asyncio.timeout(10):
            assert await task
    observation = status(selector.root)
    assert observation.state == "stopped" and observation.active_digest is None
    assert observation.output_bytes > 0
    assert service.process is not None and service.process.returncode == 0
    RuntimeLock(selector.root, "service.lock").close()
    RuntimeLock(selector.root, "supervisor.lock").close()


async def test_service_cancellation_reaps_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation cannot return before the fixed owner has relinquished ownership."""
    selector = await installed(tmp_path, monkeypatch)
    service = RuntimeService(selector.root)
    task = asyncio.create_task(service.serve(asyncio.Event()))
    await running(service)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(10):
            await task
    assert service.process is not None and service.process.returncode == 0
    RuntimeLock(selector.root, "supervisor.lock").close()
    RuntimeLock(selector.root, "service.lock").close()


async def test_service_refuses_tampered_runtime_and_disabled_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No private interpreter executes after integrity failure; disable stays usable."""
    selector = await installed(tmp_path, monkeypatch)
    selection = selector.current()
    assert selection is not None
    generation = selector.root / "generations" / selection.runtime_digest
    (generation / "runtime/unsealed.py").write_bytes(b"raise Exception('never run')")
    service = RuntimeService(selector.root)
    assert not await service.serve(asyncio.Event())
    assert service.process is None
    assert status(selector.root).error_code == "verification_failed"
    selector.disable(expected_revision=1)
    assert await RuntimeService(selector.root).serve(asyncio.Event())
    assert status(selector.root).state == "stopped"


async def test_owner_exit_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a zero exit is unexpected without a stop request and ends this lifetime."""
    selector = await installed(tmp_path, monkeypatch, "print('exit fixture')\n")
    service = RuntimeService(selector.root)
    assert not await service.serve(asyncio.Event())
    assert status(selector.root).error_code == "owner_exited"
    assert service.process is not None and service.process.returncode == 0


async def test_running_service_revalidates_trust_and_stops_revoked_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observed running process is withdrawn when current release trust changes."""
    from skulk.extensions.runtime_artifacts import RuntimeTrust

    selector = await installed(tmp_path, monkeypatch)
    monkeypatch.setattr("skulk.extensions.runtime_service._CHECK_SECONDS", 0.05)
    service = RuntimeService(selector.root)
    task = asyncio.create_task(service.serve(asyncio.Event()))
    try:
        await running(service)
        selection = selector.current()
        assert selection is not None
        trust = RuntimeTrust.model_validate_json(
            read_private(selector.root / "publisher-trust.json")
        )
        revoked = trust.model_copy(
            update={"revision": 2, "revoked_artifacts": (selection.runtime_digest,)}
        )
        write_private(
            selector.root / "publisher-trust.json", revoked.model_dump_json().encode()
        )
        async with asyncio.timeout(10):
            assert not await task
        assert status(selector.root).error_code == "verification_failed"
        assert service.process is not None and service.process.returncode == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owner_ignoring_shutdown_is_killed_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresponsive owner cannot hold cancellation or service ownership forever."""
    selector = await installed(
        tmp_path,
        monkeypatch,
        OWNER_SOURCE.replace("os.read(a.lifetime_fd,1)", "time.sleep(60)"),
    )
    monkeypatch.setattr("skulk.extensions.runtime_service._SHUTDOWN_SECONDS", 0.05)
    service = RuntimeService(selector.root)
    stopped = asyncio.Event()
    task = asyncio.create_task(service.serve(stopped))
    try:
        await running(service)
    finally:
        stopped.set()
        async with asyncio.timeout(5):
            assert await task
    assert service.process is not None and service.process.returncode == -9
    RuntimeLock(selector.root, "supervisor.lock").close()
