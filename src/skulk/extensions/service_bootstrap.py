"""Standard-library-only verification before starting the fixed plugin manager.

Local setup copies this file beside protected service state. The OS service runs
it with isolated base Python and site initialization disabled, so a changed .pth
or package cannot execute before the service-runtime seal is checked.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import cast

_MAXIMUM_FILES = 200000
_MAXIMUM_BYTES = 17179869184
_MAXIMUM_MANIFEST = 67108864
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_GENERATION = re.compile(r"^[a-f0-9]{32}$")


def private_read(path: Path, maximum: int) -> bytes:
    """Read a bounded owner-only regular file without following a symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("service metadata is not protected")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("service metadata exceeds bound")
        return value
    finally:
        os.close(descriptor)


def private_directory(path: Path) -> None:
    """Require an existing protected directory, never create it during bootstrap."""
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("service directory is not protected")


def document(raw: bytes) -> dict[str, object]:
    """Decode one JSON object with string keys, rejecting duplicate properties."""

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate service metadata property")
            result[name] = value
        return result

    value = cast(object, json.loads(raw, object_pairs_hook=unique))
    if not isinstance(value, dict):
        raise ValueError("service metadata must be an object")
    return cast(dict[str, object], value)


def digest_file(path: Path) -> str:
    """Hash a regular file without allowing its last component to become a link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("service member is not a file")
        return hashlib.file_digest(source, "sha256").hexdigest()


def runtime_tree(root: Path, base_python: Path) -> dict[str, list[str | int]]:
    """Seal the complete bounded runtime tree and its fixed interpreter aliases.

    File bytes, directory membership and permissions are included. Only venv's
    interpreter aliases and conventional lib64 link may point outside a file;
    arbitrary links back into development environments are refused.
    """
    private_directory(root)
    entries: dict[str, list[str | int]] = {
        ".": ["directory", stat.S_IMODE(root.stat().st_mode)]
    }
    size = 0
    for path in sorted(root.rglob("*")):
        if len(entries) >= _MAXIMUM_FILES:
            raise ValueError("service runtime file count exceeds bound")
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != os.getuid():
            raise ValueError("service runtime ownership differs")
        if stat.S_ISLNK(info.st_mode):
            if relative == "lib64" and os.readlink(path) == "lib":
                entries[relative] = ["link", mode, "lib"]
            elif (
                relative
                in {
                    "bin/python",
                    "bin/python3",
                    f"bin/python{sys.version_info.major}.{sys.version_info.minor}",
                }
                and path.resolve(strict=True) == base_python
            ):
                entries[relative] = [
                    "python",
                    mode,
                    str(base_python),
                    digest_file(base_python),
                ]
            else:
                raise ValueError("service runtime contains an unsupported link")
        elif mode & 0o022:
            raise ValueError("service runtime is writable by another user")
        elif stat.S_ISDIR(info.st_mode):
            entries[relative] = ["directory", mode]
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
            if size > _MAXIMUM_BYTES:
                raise ValueError("service runtime size exceeds bound")
            entries[relative] = ["file", mode, digest_file(path)]
        else:
            raise ValueError("service runtime contains an unsupported member")
    return entries


def verified_runtime(root: Path) -> Path:
    """Verify the selected service copy before allowing Python site initialization."""
    if os.geteuid() == 0:
        raise ValueError("plugin manager must run without root")
    private_directory(root)
    pointer = document(private_read(root / "core-runtime.json", 4096))
    if set(pointer) != {"generation", "manifest_sha256"}:
        raise ValueError("invalid service selection")
    name = pointer["generation"]
    digest = pointer["manifest_sha256"]
    if (
        not isinstance(name, str)
        or not _GENERATION.fullmatch(name)
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise ValueError("invalid service selection")
    private_directory(root / "core-runtimes")
    generation = root / "core-runtimes" / name
    private_directory(generation)
    raw = private_read(generation / "snapshot.json", _MAXIMUM_MANIFEST)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("service manifest integrity differs")
    metadata = document(raw)
    if metadata.get("protocol") != 1 or metadata.get("generation") != name:
        raise ValueError("service generation differs")
    base = Path(sys.executable).resolve(strict=True)
    if metadata.get("base_python") != str(base) or metadata.get(
        "base_sha256"
    ) != digest_file(base):
        raise ValueError("service base Python differs")
    if metadata.get("bootstrap_sha256") != digest_file(root / "service-bootstrap.py"):
        raise ValueError("service bootstrap differs")
    runtime = generation / "runtime"
    if metadata.get("entries") != runtime_tree(runtime, base):
        raise ValueError("service runtime integrity differs")
    return runtime


def main() -> None:
    """Verify and replace this process with the fixed manager command, without a shell."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        root = cast(Path, arguments.root).absolute()
        runtime = verified_runtime(root)
        executable = str(runtime / "bin/python")
        os.execve(
            executable,
            [
                executable,
                "-I",
                "-B",
                "-m",
                "skulk.extensions.runtime_manager",
                "serve",
                "--root",
                str(root),
            ],
            {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "SKULK_HOME": str(root)},
        )
    except (OSError, ValueError):
        print(
            "plugin service runtime unavailable; rerun local setup or inspect protected evidence",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
