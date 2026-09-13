"""Pre-site bootstrap integrity, stopped activation and incomplete service copying."""

import hashlib
import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from skulk.extensions import service_bootstrap
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.service_snapshot import (
    ServiceSnapshot,
    activate_service_runtime,
    stage_service_runtime,
)


def staged(root: Path) -> ServiceSnapshot:
    """Build a minimal actual venv with a fixed manager fixture and complete seal."""
    private_directory(root)
    private_directory(root / "core-runtimes")
    generation = root / "core-runtimes" / ("a" * 32)
    private_directory(generation)
    runtime = generation / "runtime"
    private_directory(runtime)
    venv.EnvBuilder(symlinks=True, with_pip=False).create(runtime)
    site = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    package = site / "skulk/extensions"
    package.mkdir(parents=True)
    (site / "skulk/__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "runtime_manager.py").write_text("print('verified-manager')\n")
    bootstrap = Path(service_bootstrap.__file__).read_bytes()
    write_private(generation / "bootstrap.py", bootstrap)
    base = Path(sys.executable).resolve(strict=True)
    metadata = {
        "protocol": 1,
        "generation": "a" * 32,
        "base_python": str(base),
        "base_sha256": service_bootstrap.digest_file(base),
        "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
        "entries": service_bootstrap.runtime_tree(runtime, base),
    }
    payload = json.dumps(metadata, sort_keys=True).encode()
    write_private(generation / "snapshot.json", payload)
    result = ServiceSnapshot(
        generation="a" * 32,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        skulk_build_sha256="b" * 64,
        copied_files=3,
        copied_bytes=32,
    )
    write_private(generation / "staged.json", result.model_dump_json().encode())
    return result


def test_bootstrap_uses_fixed_verified_copy_and_activation_requires_stopped_owner(
    tmp_path: Path,
) -> None:
    """A sealed runtime runs without source PYTHONPATH; a live manager fences selection."""
    snapshot = staged(tmp_path)
    owner = RuntimeLock(tmp_path, "manager.lock")
    try:
        with pytest.raises(BlockingIOError):
            activate_service_runtime(tmp_path, snapshot)
        assert not (tmp_path / "core-runtime.json").exists()
    finally:
        owner.close()
    activate_service_runtime(tmp_path, snapshot)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(tmp_path / "service-bootstrap.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert (
        result.returncode == 0
        and result.stdout == b"verified-manager\n"
        and not result.stderr
    )


@pytest.mark.parametrize("fault", ["file", "pth", "link", "manifest", "base"])
def test_bootstrap_refuses_tampering_before_site_startup(
    tmp_path: Path, fault: str
) -> None:
    """A changed dependency or startup hook cannot execute before verification."""
    snapshot = staged(tmp_path)
    activate_service_runtime(tmp_path, snapshot)
    generation = tmp_path / "core-runtimes" / snapshot.generation
    runtime = generation / "runtime"
    site = (
        runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    marker = tmp_path / "unverified-executed"
    if fault == "file":
        (site / "skulk/extensions/runtime_manager.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
        )
    elif fault == "pth":
        (site / "unverified.pth").write_text(
            f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
        )
    elif fault == "link":
        (site / "external").symlink_to(tmp_path)
    elif fault == "manifest":
        (generation / "snapshot.json").write_bytes(b"{}")
    else:
        metadata = service_bootstrap.document(
            read_private(generation / "snapshot.json", 67108864)
        )
        metadata["base_sha256"] = "0" * 64
        raw = json.dumps(metadata).encode()
        write_private(generation / "snapshot.json", raw)
        write_private(
            tmp_path / "core-runtime.json",
            json.dumps(
                {
                    "generation": snapshot.generation,
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                }
            ).encode(),
        )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(tmp_path / "service-bootstrap.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 1 and not result.stdout
    assert (
        b"unavailable" in result.stderr and str(tmp_path).encode() not in result.stderr
    )
    assert not marker.exists()


async def test_interrupted_copy_does_not_publish_over_existing_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed local copy retains its incomplete generation and the current pointer."""
    private_directory(tmp_path)
    write_private(tmp_path / "core-runtime.json", b"retained current selection")
    monkeypatch.setattr("skulk.extensions.service_snapshot._sources", lambda: ())

    def disk_failure(*_: object) -> None:
        raise OSError("synthetic copy disk failure")

    monkeypatch.setattr("skulk.extensions.service_snapshot._copy", disk_failure)
    with pytest.raises(OSError, match="synthetic"):
        await stage_service_runtime(tmp_path)
    assert read_private(tmp_path / "core-runtime.json") == b"retained current selection"
    generations = list((tmp_path / "core-runtimes").iterdir())
    assert len(generations) == 1 and not (generations[0] / "staged.json").exists()
    RuntimeLock(tmp_path).close()


async def test_source_editable_dependency_is_not_silently_borrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown editable startup paths fail setup before copying or activating code."""
    environment = tmp_path / "source"
    environment.mkdir()
    (environment / "third_party.pth").write_text("/a/development/checkout\n")

    def environment_path(name: str) -> str:
        return str(environment)

    monkeypatch.setattr(
        "skulk.extensions.service_snapshot.sysconfig.get_path", environment_path
    )
    monkeypatch.setattr(
        "skulk.extensions.service_snapshot._distributions", lambda: {"skulk": "1.5.2"}
    )
    with pytest.raises(ValueError, match="path extension"):
        await stage_service_runtime(tmp_path / "service")
    assert not (tmp_path / "service/core-runtime.json").exists()
