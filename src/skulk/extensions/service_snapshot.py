"""Copy the exact existing Skulk runtime into stable, verified service storage."""

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import stat
import sys
import sysconfig
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import final
from uuid import uuid4

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from skulk.extensions import service_bootstrap
from skulk.extensions.runtime_artifacts import Digest, measure_host
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_install import finish_runtime_work
from skulk.utils.dashboard_path import find_resources

_MAXIMUM_FILES = 200000
_MAXIMUM_BYTES = 17179869184


class ServiceSnapshot(BaseModel):
    """An immutable staged core service copy, independent of plugin generations."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    generation: str = Field(
        pattern=r"^[a-f0-9]{32}$",
        description="Locally generated immutable service-runtime ID.",
    )
    manifest_sha256: Digest = Field(
        description="Digest of the complete protected copy manifest."
    )
    skulk_build_sha256: Digest = Field(
        description="Exact qualified Skulk code and native binding identity."
    )
    copied_files: int = Field(
        ge=1, le=200000, description="Number of copied source and dependency files."
    )
    copied_bytes: int = Field(
        ge=1, le=17179869184, description="Total copied source and dependency bytes."
    )


@final
@dataclass(frozen=True)
class _SourceFile:
    relative: str
    source: Path
    digest: str
    size: int
    executable: bool


def _distributions() -> dict[str, str]:
    values: dict[str, str] = {}
    for distribution in importlib.metadata.distributions(
        path=[sysconfig.get_path("purelib")]
    ):
        name = str(canonicalize_name(distribution.metadata["Name"]))
        if name in values:
            raise ValueError("duplicate installed distribution")
        values[name] = distribution.version
    if not values or len(values) > 1024:
        raise ValueError("installed dependency inventory exceeds bound")
    return dict(sorted(values.items()))


def _sources() -> tuple[_SourceFile, ...]:
    if sys.prefix == sys.base_prefix or sysconfig.get_path(
        "purelib"
    ) != sysconfig.get_path("platlib"):
        raise ValueError(
            "service setup requires an isolated supported Skulk environment"
        )
    site_packages = Path(sysconfig.get_path("purelib"))
    site_prefix = (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/"
    )
    roots = [(site_packages, site_prefix, True)]
    for package in ("skulk", "skulk_pyo3_bindings"):
        specification = importlib.util.find_spec(package)
        if specification is None or specification.origin is None:
            raise ValueError("installed core package is unavailable")
        roots.append(
            (Path(specification.origin).parent, site_prefix + package + "/", False)
        )
    # Core imports need bundled declarative resources even without a dashboard.
    # Put them beside the new runtime's lib directory, where the normal resource
    # finder locates them without an environment override or checkout dependency.
    roots.append((find_resources(), "resources/", False))
    result: list[_SourceFile] = []
    total = 0
    for root, prefix, primary in roots:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if primary and relative.parts[0] in {"skulk", "skulk_pyo3_bindings"}:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("installed dependency has an unresolved link")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise ValueError("installed dependency is not a protected regular file")
            if primary and relative == Path("skulk.pth"):
                # The effective imported core is copied explicitly above. Never
                # retain the editable installation's checkout redirection.
                continue
            if primary and relative.parent == Path("."):
                if (
                    path.name in {"sitecustomize.py", "usercustomize.py"}
                    or path.suffix == ".egg-link"
                ):
                    raise ValueError("unsupported installed startup customization")
                if path.suffix == ".pth":
                    if path.name not in {"_virtualenv.pth", "distutils-precedence.pth"}:
                        raise ValueError("unsupported installed path extension")
                    # This new standard-library venv does not need the old
                    # installer's startup shims or their hidden import hooks.
                    continue
            total += info.st_size
            if len(result) >= _MAXIMUM_FILES or total > _MAXIMUM_BYTES:
                raise ValueError("core service copy exceeds bound")
            result.append(
                _SourceFile(
                    prefix + relative.as_posix(),
                    path,
                    service_bootstrap.digest_file(path),
                    info.st_size,
                    bool(info.st_mode & 0o100),
                )
            )
    names = [item.relative for item in result]
    if len(set(names)) != len(names):
        raise ValueError("core service source identity is ambiguous")
    return tuple(sorted(result, key=lambda item: item.relative))


def _copy(source: tuple[_SourceFile, ...], destination: Path) -> None:
    for item in source:
        target = destination / item.relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(item.source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as original, target.open("xb") as copied:
            info = os.fstat(original.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item.size:
                raise ValueError("core dependency changed during copy")
            shutil.copyfileobj(original, copied, length=1048576)
            copied.flush()
            os.fsync(copied.fileno())
        target.chmod(0o700 if item.executable else 0o600)
        if service_bootstrap.digest_file(target) != item.digest:
            raise ValueError("core dependency bytes changed during copy")


_QUALIFY = """import importlib.metadata,json
from packaging.utils import canonicalize_name
from skulk.extensions.runtime_artifacts import measure_host
host=measure_host()
print(json.dumps({'platform':host.platform,'python_version':host.python_version,
'skulk_version':host.skulk_version,'skulk_build_sha256':host.skulk_build_sha256,
'inventory':{str(canonicalize_name(d.metadata['Name'])):d.version
for d in importlib.metadata.distributions()}},sort_keys=True))
"""


async def _qualify(runtime: Path, lock: RuntimeLock) -> bytes:
    process = await asyncio.create_subprocess_exec(
        str(runtime / "bin/python"),
        "-I",
        "-B",
        "-c",
        _QUALIFY,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        pass_fds=(lock.descriptor,),
        cwd=runtime,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SKULK_HOME": str(runtime.parent / "qualification-state"),
        },
    )
    output = bytearray()
    try:
        assert process.stdout is not None
        async with asyncio.timeout(60):
            while block := await process.stdout.read(16384):
                output.extend(block)
                if len(output) > 262144:
                    raise ValueError("service qualification output exceeds bound")
            await process.wait()
        if process.returncode != 0:
            raise ValueError("copied service runtime did not qualify")
        return bytes(output)
    except (OSError, ValueError, TimeoutError):
        write_private(
            runtime.parent / "qualification-evidence.log", bytes(output[:262144])
        )
        raise
    finally:
        if process.returncode is None:
            process.kill()
        await finish_runtime_work(asyncio.create_task(process.wait()))


def service_source_identity() -> str:
    """Fingerprint the current core, dependency inventory and exact base interpreter.

    Local setup uses this identity to distinguish an unchanged service copy from
    an explicitly requested setup using a new Python or dependency environment.
    Runtime activation still verifies the complete copied tree independently.
    """
    host = measure_host()
    base = Path(sys.executable).resolve(strict=True)
    identity = {
        "platform": host.platform,
        "python_version": host.python_version,
        "skulk_version": host.skulk_version,
        "skulk_build_sha256": host.skulk_build_sha256,
        "inventory": _distributions(),
        "base_python": str(base),
        "base_sha256": service_bootstrap.digest_file(base),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


async def stage_service_runtime(root: Path) -> ServiceSnapshot:
    """Stage and qualify an exact copy without altering the live Skulk environment.

    Dependencies are copied locally, never resolved or installed over the source.
    The effective core replaces editable .pth indirection. Completed copies retain
    a full tree seal; interrupted copies remain retained and are never activated.
    Cancellation waits for owned copy and qualification work to finish.
    """
    if os.geteuid() == 0:
        raise ValueError("service runtime preparation must run without root")
    private_directory(root)
    lock = RuntimeLock(root)
    try:

        async def prepare() -> ServiceSnapshot:
            host = await asyncio.to_thread(measure_host)
            inventory = _distributions()
            source = await asyncio.to_thread(_sources)
            generation_id = uuid4().hex
            parent = root / "core-runtimes"
            private_directory(parent)
            generation = parent / generation_id
            private_directory(generation)
            runtime = generation / "runtime"
            private_directory(runtime)
            await asyncio.to_thread(
                venv.EnvBuilder(symlinks=True, with_pip=False).create, runtime
            )
            await asyncio.to_thread(_copy, source, runtime)
            bootstrap = Path(service_bootstrap.__file__).read_bytes()
            write_private(generation / "bootstrap.py", bootstrap)
            raw = await _qualify(runtime, lock)
            expected = {
                "platform": host.platform,
                "python_version": host.python_version,
                "skulk_version": host.skulk_version,
                "skulk_build_sha256": host.skulk_build_sha256,
                "inventory": inventory,
            }
            if service_bootstrap.document(raw) != expected:
                raise ValueError("copied service identity differs")
            if (
                await asyncio.to_thread(measure_host) != host
                or _distributions() != inventory
                or await asyncio.to_thread(_sources) != source
            ):
                raise ValueError(
                    "source environment changed during service preparation"
                )
            base = Path(sys.executable).resolve(strict=True)
            entries = await asyncio.to_thread(
                service_bootstrap.runtime_tree, runtime, base
            )
            metadata = {
                "protocol": 1,
                "generation": generation_id,
                "base_python": str(base),
                "base_sha256": service_bootstrap.digest_file(base),
                "bootstrap_sha256": hashlib.sha256(bootstrap).hexdigest(),
                "qualified_host": expected,
                "entries": entries,
            }
            payload = json.dumps(
                metadata, sort_keys=True, separators=(",", ":")
            ).encode()
            if len(payload) > 67108864:
                raise ValueError("service manifest exceeds bound")
            write_private(generation / "snapshot.json", payload)
            result = ServiceSnapshot(
                generation=generation_id,
                manifest_sha256=hashlib.sha256(payload).hexdigest(),
                skulk_build_sha256=host.skulk_build_sha256,
                copied_files=len(source),
                copied_bytes=sum(item.size for item in source),
            )
            write_private(generation / "staged.json", result.model_dump_json().encode())
            return result

        return await finish_runtime_work(asyncio.create_task(prepare()))
    finally:
        lock.close()


def activate_service_runtime(root: Path, snapshot: ServiceSnapshot) -> None:
    """Select a staged service copy only while the generic manager is stopped.

    This owner-local setup step changes only the generic manager runtime. Plugin
    selections, credentials and independently supervised cleanup stay untouched.
    """
    installer = RuntimeLock(root)
    try:
        owner = RuntimeLock(root, "manager.lock")
        try:
            generation = root / "core-runtimes" / snapshot.generation
            if (
                ServiceSnapshot.model_validate_json(
                    read_private(generation / "staged.json")
                )
                != snapshot
            ):
                raise ValueError("staged service identity differs")
            raw = read_private(generation / "snapshot.json", 67108864)
            if hashlib.sha256(raw).hexdigest() != snapshot.manifest_sha256:
                raise ValueError("service manifest integrity differs")
            metadata = service_bootstrap.document(raw)
            base = Path(sys.executable).resolve(strict=True)
            if (
                metadata.get("base_python") != str(base)
                or metadata.get("base_sha256") != service_bootstrap.digest_file(base)
                or metadata.get("entries")
                != service_bootstrap.runtime_tree(generation / "runtime", base)
            ):
                raise ValueError("staged service runtime differs")
            bootstrap = read_private(generation / "bootstrap.py", 65536)
            if hashlib.sha256(bootstrap).hexdigest() != metadata.get(
                "bootstrap_sha256"
            ):
                raise ValueError("service bootstrap differs")
            write_private(root / "service-bootstrap.py", bootstrap)
            write_private(
                root / "core-runtime.json",
                json.dumps(
                    {
                        "generation": snapshot.generation,
                        "manifest_sha256": snapshot.manifest_sha256,
                    },
                    sort_keys=True,
                ).encode(),
            )
        finally:
            owner.close()
    finally:
        installer.close()
