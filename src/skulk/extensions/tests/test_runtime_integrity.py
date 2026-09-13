"""Installed-file mutations are refused without importing the private runtime."""

import os
import sys
from pathlib import Path

import pytest

from skulk.extensions.runtime_files import private_directory, write_private
from skulk.extensions.runtime_install import RuntimeInstaller
from skulk.extensions.runtime_integrity import seal_runtime, verify_installed_runtime
from skulk.extensions.tests.test_runtime_install import artifacts


@pytest.mark.parametrize(
    "fault", ["changed", "added", "missing", "mode", "link", "identity"]
)
def test_installed_runtime_seal_detects_mutation(tmp_path: Path, fault: str) -> None:
    """Seal complete directory membership, including executable startup code."""
    private_directory(tmp_path)
    runtime = tmp_path / "runtime"
    private_directory(runtime)
    write_private(runtime / "library.py", b"VALUE=7\n")
    private_directory(runtime / "bin")
    (runtime / "bin/python").symlink_to(Path(sys.executable).resolve())
    seal_runtime(tmp_path, "a" * 64)
    verify_installed_runtime(tmp_path, "a" * 64)
    if fault == "changed":
        write_private(runtime / "library.py", b"VALUE=8\n")
    elif fault == "added":
        write_private(runtime / "startup.pth", b"import unexpected\n")
    elif fault == "missing":
        (runtime / "library.py").unlink()
    elif fault == "mode":
        (runtime / "library.py").chmod(0o666)
    elif fault == "link":
        (runtime / "bin/python").unlink()
        (runtime / "bin/python").symlink_to("/bin/sh")
    with pytest.raises(ValueError):
        verify_installed_runtime(tmp_path, ("b" if fault == "identity" else "a") * 64)


async def test_cached_stage_refuses_startup_injection_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly added .pth file never executes during cached-stage verification."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    installer = RuntimeInstaller(tmp_path / "installation")
    write_private(
        installer.root / "publisher-trust.json", trust.model_dump_json().encode()
    )
    operation = await installer.stage(metadata, source)
    generation = installer.root / "generations" / operation.runtime_digest
    verify_installed_runtime(generation, operation.runtime_digest)
    marker = tmp_path / "unexpected-startup"
    site = (
        generation
        / "runtime/lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    injection = site / "unexpected.pth"
    injection.write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    os.chmod(injection, 0o600)
    with pytest.raises(ValueError, match="integrity differs"):
        await installer.stage(metadata, source, operation_id=operation.operation_id)
    assert not marker.exists()
    assert installer.operation(operation.operation_id).state == "recovery_required"
