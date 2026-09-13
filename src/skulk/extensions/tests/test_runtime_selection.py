"""Stopped-owner activation, explicit rollback and interrupted local recovery."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skulk.extensions.runtime_files import RuntimeLock, read_private, write_private
from skulk.extensions.runtime_selection import RuntimeSelector, SelectionOperation
from skulk.extensions.tests.test_runtime_install import artifacts


async def test_selection_preserves_state_and_requires_explicit_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrades, rollback, revocation and duplicate IDs never discard logical state."""
    key = Ed25519PrivateKey.generate()
    first, trust, host = artifacts(tmp_path / "first", signing_key=key)
    second, _, _ = artifacts(
        tmp_path / "second",
        signing_key=key,
        sequence=2,
        permissions=("local synthetic operation", "another operation"),
    )
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    selector = RuntimeSelector(tmp_path / "installed")
    write_private(
        selector.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    write_private(selector.root / "identity", b"retained identity")
    write_private(selector.root / "receipts", b"retained cleanup obligation")
    stage_one = await selector.installer.stage(first, tmp_path / "first")
    stage_two = await selector.installer.stage(second, tmp_path / "second")
    owner = RuntimeLock(selector.root, "supervisor.lock")
    try:
        with pytest.raises(BlockingIOError):
            await selector.activate(stage_one.runtime_digest, expected_revision=0)
    finally:
        owner.close()
    assert selector.current() is None
    activated = await selector.activate(stage_one.runtime_digest, expected_revision=0)
    assert activated.state == "complete"
    with pytest.raises(ValueError, match="permissions"):
        await selector.activate(stage_two.runtime_digest, expected_revision=1)
    upgraded = await selector.activate(
        stage_two.runtime_digest,
        expected_revision=1,
        accept_permissions=True,
    )
    with pytest.raises(ValueError, match="rollback"):
        await selector.activate(stage_one.runtime_digest, expected_revision=2)
    rolled_back = await selector.activate(
        stage_one.runtime_digest,
        expected_revision=2,
        rollback=True,
    )
    assert rolled_back.selection.highest_sequence == 2
    assert rolled_back.selection.sequence == 1
    with pytest.raises(ValueError, match="rollback"):
        await selector.activate(stage_one.runtime_digest, expected_revision=3)
    assert (
        await selector.activate(
            stage_two.runtime_digest,
            expected_revision=1,
            operation_id=upgraded.operation_id,
            accept_permissions=True,
        )
        == upgraded
    )
    assert selector.current() == rolled_back.selection
    with pytest.raises(ValueError, match="revision"):
        selector.disable(expected_revision=2)
    revoked = trust.model_copy(
        update={"revision": 2, "revoked_artifacts": (stage_one.runtime_digest,)}
    )
    write_private(
        selector.root / "publisher-trust.json", revoked.model_dump_json().encode()
    )
    disabled = selector.disable(expected_revision=3)
    assert not disabled.selection.enabled
    legacy = disabled.model_dump_json()
    assert "verify_runtime" not in legacy
    assert SelectionOperation.model_validate_json(legacy) == disabled
    assert selector.current() == disabled.selection
    assert read_private(selector.root / "identity") == b"retained identity"
    assert read_private(selector.root / "receipts") == b"retained cleanup obligation"
    assert (selector.root / "generations" / stage_one.runtime_digest).is_dir()


@pytest.mark.parametrize("fault", ["before_publish", "after_publish"])
async def test_interrupted_selection_recovers_exact_local_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Recover across the atomic publication boundary without repeating acquisition."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    selector = RuntimeSelector(tmp_path / "installed")
    write_private(
        selector.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    staged = await selector.installer.stage(metadata, source)
    identifier = "a" * 32

    def fail_write(path: Path, value: bytes) -> None:
        if path == selector.root / "runtime-selection.json":
            raise OSError("synthetic disk failure")
        write_private(path, value)

    def fail_unlink(path: Path) -> None:
        raise OSError("synthetic directory sync failure")

    with monkeypatch.context() as failure:
        if fault == "before_publish":
            failure.setattr(
                "skulk.extensions.runtime_selection.write_private", fail_write
            )
        else:
            failure.setattr(
                "skulk.extensions.runtime_selection._remove_private", fail_unlink
            )
        with pytest.raises(OSError):
            await selector.activate(
                staged.runtime_digest, expected_revision=0, operation_id=identifier
            )
    assert selector.operation(identifier).state == "recovery_required"
    assert (selector.current() is None) == (fault == "before_publish")
    with pytest.raises(ValueError, match="recovery"):
        await selector.activate(
            staged.runtime_digest, expected_revision=0, operation_id=identifier
        )
    recovered = await selector.recover()
    assert recovered.state == "complete"
    assert recovered.operation_id == identifier
    assert recovered.selection.revision == 1
    assert selector.current() == recovered.selection
    assert not selector.pending.exists()


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_disable_withdraws_revoked_interrupted_initial_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool, enabled: bool
) -> None:
    """An explicit stopped-state withdrawal never has to execute revoked code."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    selector = RuntimeSelector(tmp_path / "installed")
    write_private(selector.root / "publisher-trust.json", trust.model_dump_json().encode())
    write_private(selector.root / "receipts", b"synthetic outstanding cleanup")
    staged = await selector.installer.stage(metadata, source)
    original_id, disable_id = "d" * 32, "e" * 32

    def fail_selection(path: Path, content: bytes) -> None:
        if path == selector.root / "runtime-selection.json":
            if published:
                write_private(path, content)
            raise OSError("interrupted publication")
        write_private(path, content)

    with monkeypatch.context() as failure:
        failure.setattr("skulk.extensions.runtime_selection.write_private", fail_selection)
        with pytest.raises(OSError):
            await selector.activate(staged.runtime_digest, expected_revision=0, operation_id=original_id, enabled=enabled)
    revoked = trust.model_copy(update={"revision": 2, "revoked_artifacts": (staged.runtime_digest,)})
    write_private(selector.root / "publisher-trust.json", revoked.model_dump_json().encode())
    with pytest.raises(ValueError, match="revocation"):
        await selector.recover()
    expected_revision = 1 if published else 0
    with pytest.raises(ValueError, match="identity"):
        selector.disable(expected_revision=expected_revision, operation_id=original_id)
    with pytest.raises(ValueError, match="revision"):
        selector.disable(expected_revision=expected_revision + 1, operation_id=disable_id)
    owner = RuntimeLock(selector.root, "supervisor.lock")
    try:
        with pytest.raises(BlockingIOError):
            selector.disable(expected_revision=expected_revision, operation_id=disable_id)
    finally:
        owner.close()
    # A second filesystem interruption must retain the new disable intent, not
    # resurrect the revoked activation when the process restarts.
    with monkeypatch.context() as failure:
        failure.setattr("skulk.extensions.runtime_selection.write_private", fail_selection)
        with pytest.raises(OSError):
            selector.disable(expected_revision=expected_revision, operation_id=disable_id)
    pending = SelectionOperation.model_validate_json(read_private(selector.pending))
    assert pending.operation_id == disable_id
    assert pending.withdraws_operation_id == original_id
    assert not pending.selection.enabled
    recovered = await selector.recover()
    assert recovered.state == "complete"
    assert not recovered.selection.enabled
    assert selector.operation(original_id).state == ("complete" if published else "superseded")
    assert selector.disable(expected_revision=expected_revision, operation_id=disable_id) == recovered
    assert read_private(selector.root / "receipts") == b"synthetic outstanding cleanup"
    assert (selector.root / "generations" / staged.runtime_digest).is_dir()
    assert not selector.pending.exists()
