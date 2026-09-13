"""Generic signed runtime staging with real offline pip and durable refusal evidence."""

import asyncio
import hashlib
import importlib.metadata
import io
import json
import sqlite3
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from skulk.extensions.runtime_artifacts import (
    QualifiedHost,
    RuntimeTrust,
    canonical_json,
    verified_artifacts,
    verify_runtime,
)
from skulk.extensions.runtime_files import (
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_install import RuntimeInstaller


def artifacts(
    directory: Path,
    *,
    dependency: str | None = None,
    sequence: int = 1,
    owner_entrypoint: bool = True,
    owner_source: str = "print('owner fixture')\n",
    setup_source: str | None = None,
    management_source: str | None = None,
    signing_key: Ed25519PrivateKey | None = None,
    permissions: tuple[str, ...] = ("local synthetic operation",),
) -> tuple[bytes, RuntimeTrust, QualifiedHost]:
    """Create an independently signed generic package with no private SDK metadata."""
    private_directory(directory)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        prefix = "example_dep-1.0.dist-info/"
        files = {
            "example_dep/__init__.py": "VALUE = 7\n",
            prefix
            + "METADATA": "Metadata-Version: 2.1\nName: example-dep\nVersion: 1.0\n"
            + (f"Requires-Dist: {dependency}\n" if dependency else ""),
            prefix
            + "WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        files[prefix + "RECORD"] = (
            "".join(f"{name},,\n" for name in files) + prefix + "RECORD,,\n"
        )
        for name, content in files.items():
            archive.writestr(name, content)
    wheel = buffer.getvalue()
    bundle_buffer = io.BytesIO()
    with zipfile.ZipFile(bundle_buffer, "w") as archive:
        archive.writestr(
            "__owner__.py" if owner_entrypoint else "__main__.py",
            owner_source,
        )
        if setup_source is not None:
            archive.writestr("__setup__.py", setup_source)
        if management_source is not None:
            archive.writestr("__manage__.py", management_source)
    bundle = bundle_buffer.getvalue()
    write_private(directory / "bundle.pyz", bundle)
    filename = "example_dep-1.0-py3-none-any.whl"
    write_private(directory / filename, wheel)
    now = int(time.time())
    host = QualifiedHost("macos-arm64", "3.13.13", "1.5.2", "a" * 64)
    key = signing_key or Ed25519PrivateKey.generate()
    trust = RuntimeTrust(
        revision=1,
        expires_at=now + 3600,
        publishers={"fixture": key.public_key().public_bytes_raw().hex()},
    )
    payload: dict[str, JsonValue] = {
        "protocol": 2,
        "implementation": "cpython",
        "platform": host.platform,
        "release": {
            "protocol": 1,
            "publisher": "fixture",
            "sequence": sequence,
            "created_at": now - 1,
            "expires_at": now + 3600,
            "artifact_name": "bundle.pyz",
            "artifact_size": len(bundle),
            "manifest": {
                "bundle_id": "example.plugin",
                "bundle_version": "1.0.0",
                "skulk_requires": "==1.5.2",
                "executable": "bundle.pyz",
                "executable_sha256": hashlib.sha256(bundle).hexdigest(),
                "plugin_specific_policy": {"opaque": True},
            },
            "platforms": ["darwin"],
            "python_requires": "==3.13.*",
            "skulk_build_sha256": host.skulk_build_sha256,
            "dependency_lock_sha256": hashlib.sha256(
                canonical_json({"example-dep": "1.0"})
            ).hexdigest(),
            "state_schema": "example.v1",
            "compatible_state_schemas": [],
            "permissions": list(permissions),
        },
        "wheels": [
            {
                "filename": filename,
                "sha256": hashlib.sha256(wheel).hexdigest(),
                "size": len(wheel),
            }
        ],
    }
    signed = canonical_json(
        {"runtime": payload, "signature": key.sign(canonical_json(payload)).hex()}
    )
    return signed, trust, host


@pytest.mark.parametrize(
    "fault", ["signature", "expired", "revoked", "core", "platform", "python"]
)
def test_generic_runtime_refuses_untrusted_or_incompatible(
    tmp_path: Path, fault: str
) -> None:
    """Exact authenticated claims gate every platform and build before execution."""
    metadata, trust, host = artifacts(tmp_path)
    now = int(time.time())
    if fault == "signature":
        metadata = metadata.replace(
            b"local synthetic operation", b"other synthetic operation"
        )
    elif fault == "expired":
        now += 7200
    elif fault == "revoked":
        digest = verify_runtime(metadata, trust, host, now=now).digest
        trust = trust.model_copy(update={"revoked_artifacts": (digest,)})
    elif fault == "core":
        host = replace(host, skulk_build_sha256="b" * 64)
    elif fault == "platform":
        host = replace(host, platform="ubuntu-24.04-x86_64")
    else:
        host = replace(host, python_version="3.14.0")
    with pytest.raises(ValueError):
        verify_runtime(metadata, trust, host, now=now)


@pytest.mark.parametrize(
    "dependency",
    ["missing==1.0", "example-dep>=2", "other @ https://invalid.example/other.whl"],
)
def test_complete_wheel_closure_required(tmp_path: Path, dependency: str) -> None:
    """No missing, incompatible or externally fetched wheel dependency is admitted."""
    metadata, trust, host = artifacts(tmp_path, dependency=dependency)
    runtime = verify_runtime(metadata, trust, host, now=int(time.time()))
    with pytest.raises(ValueError, match="dependency"):
        verified_artifacts(runtime, tmp_path)


def test_fixed_owner_entrypoint_is_required(tmp_path: Path) -> None:
    """A validly signed bundle still needs the generic owner's fixed entrypoint."""
    metadata, trust, host = artifacts(tmp_path, owner_entrypoint=False)
    runtime = verify_runtime(metadata, trust, host, now=int(time.time()))
    with pytest.raises(ValueError, match="owner entrypoint"):
        verified_artifacts(runtime, tmp_path)


async def test_offline_stage_and_reconnect_leave_host_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install an exact wheel in a fresh environment, then reuse only cached bytes."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    root = tmp_path / "installation"
    installer = RuntimeInstaller(root)
    write_private(root / "publisher-trust.json", trust.model_dump_json().encode())
    inventory = sorted(
        (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
    )
    operation = await installer.stage(metadata, source, operation_id="1" * 32)
    assert operation.state == "staged"
    assert installer.operation(operation.operation_id) == operation
    assert (
        await installer.stage(
            metadata, tmp_path / "no-source", operation_id=operation.operation_id
        )
    ).state == "staged"
    assert inventory == sorted(
        (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
    )
    assert not (root / "active.json").exists()
    generation = root / "generations" / operation.runtime_digest
    assert read_private(generation / "staged.json") == metadata
    write_private(generation / "artifacts" / "bundle.pyz", b"tampered")
    with pytest.raises(ValueError, match="bundle differs"):
        await installer.stage(metadata, source, operation_id=operation.operation_id)
    assert installer.operation(operation.operation_id).state == "recovery_required"


async def test_interrupted_generation_is_retained_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial generation remains stopped and keeps evidence for explicit recovery."""
    metadata, trust, host = artifacts(tmp_path / "source")
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    installer = RuntimeInstaller(tmp_path / "installation")
    write_private(
        installer.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    runtime = verify_runtime(metadata, trust, host, now=int(time.time()))
    partial = installer.root / "generations" / runtime.digest
    private_directory(installer.root / "generations")
    private_directory(partial)
    write_private(partial / "evidence", b"interrupted installation")
    operation = await installer.stage(metadata, tmp_path / "source")
    assert operation.state == "recovery_required"
    assert read_private(partial / "evidence") == b"interrupted installation"
    assert not (partial / "runtime").exists()
    assert (
        await installer.stage(
            metadata, tmp_path / "source", operation_id=operation.operation_id
        )
    ) == operation


async def test_refused_artifact_cannot_roll_back_observed_owner_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed install still retains the owner's newer trust revision."""
    metadata, trust, host = artifacts(tmp_path / "source")
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    installer = RuntimeInstaller(tmp_path / "installation")
    updated = trust.model_copy(update={"revision": 2})
    write_private(
        installer.root / "publisher-trust.json", updated.model_dump_json().encode()
    )
    with pytest.raises(ValueError, match="signature"):
        await installer.stage(
            metadata.replace(
                b"local synthetic operation", b"other synthetic operation"
            ),
            tmp_path / "source",
        )
    write_private(
        installer.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    with pytest.raises(ValueError, match="rollback"):
        await installer.stage(metadata, tmp_path / "source")
    changed = updated.model_copy(update={"expires_at": updated.expires_at + 1})
    write_private(
        installer.root / "publisher-trust.json", changed.model_dump_json().encode()
    )
    with pytest.raises(ValueError, match="equivocation"):
        await installer.stage(metadata, tmp_path / "source")


async def test_operation_conflict_does_not_rewrite_prior_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing an operation ID cannot change the operation the owner already observed."""
    metadata, trust, host = artifacts(tmp_path / "source")
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    installer = RuntimeInstaller(tmp_path / "installation")
    write_private(
        installer.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    with sqlite3.connect(installer.database) as connection:
        record = json.dumps(
            {
                "operation_id": "2" * 32,
                "runtime_digest": "b" * 64,
                "state": "staging",
                "error_code": None,
            }
        )
        connection.execute(
            "INSERT INTO operations VALUES (?,?,?)", ("2" * 32, "b" * 64, record)
        )
    before = installer.operation("2" * 32)
    with pytest.raises(ValueError, match="identity differs"):
        await installer.stage(metadata, tmp_path / "source", operation_id="2" * 32)
    assert installer.operation("2" * 32) == before


async def test_cancelled_request_retains_installer_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disconnect cannot release the installation fence while artifact work runs."""
    import threading

    from skulk.extensions import runtime_install

    metadata, trust, host = artifacts(tmp_path / "source")
    monkeypatch.setattr(runtime_install, "measure_host", lambda: host)
    entered, resume = threading.Event(), threading.Event()
    original = runtime_install.verified_artifacts

    def held_verify(
        runtime: runtime_install.VerifiedRuntime, directory: Path
    ) -> dict[str, bytes]:
        entered.set()
        assert resume.wait(10)
        return original(runtime, directory)

    monkeypatch.setattr(runtime_install, "verified_artifacts", held_verify)
    installer = RuntimeInstaller(tmp_path / "installation")
    write_private(
        installer.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    task = asyncio.create_task(
        installer.stage(metadata, tmp_path / "source", operation_id="3" * 32)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(BlockingIOError):
            await installer.stage(metadata, tmp_path / "source")
    finally:
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert installer.operation("3" * 32).state == "staged"
