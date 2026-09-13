"""Bounded installed-runtime integrity checks performed before Python startup."""

import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from skulk.extensions.runtime_artifacts import Digest, canonical_json
from skulk.extensions.runtime_files import read_private, write_private


class _Entry(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    kind: Literal["file", "directory", "link"]
    mode: int = Field(ge=0, le=0o777)
    digest: Digest | None = None
    target: str | None = Field(default=None, max_length=4096)


class _Seal(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    runtime_digest: Digest
    entries: dict[str, _Entry] = Field(min_length=1, max_length=20000)


def _inventory(directory: Path) -> dict[str, _Entry]:
    root_info = directory.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or root_info.st_mode & 0o022
    ):
        raise ValueError("installed runtime directory is unsafe")
    entries: dict[str, _Entry] = {
        ".": _Entry(kind="directory", mode=stat.S_IMODE(root_info.st_mode))
    }
    total = 0
    # Do not follow directory links. Only the conventional lib64 alias and
    # interpreter aliases made by venv are accepted, never links into a checkout.
    for path in sorted(directory.rglob("*")):
        if len(entries) >= 20000:
            raise ValueError("installed runtime file count exceeds bound")
        relative = path.relative_to(directory).as_posix()
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise ValueError("installed runtime ownership differs")
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if relative == "lib64" and target == "lib":
                entry = _Entry(kind="link", mode=mode, target=target)
            elif relative in {
                "bin/python",
                "bin/python3",
                f"bin/python{sys.version_info.major}.{sys.version_info.minor}",
            }:
                resolved = path.resolve(strict=True)
                if resolved != Path(sys.executable).resolve(strict=True):
                    raise ValueError("installed interpreter is not the qualified base")
                with resolved.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                entry = _Entry(
                    kind="link", mode=mode, target=str(resolved), digest=digest
                )
            else:
                raise ValueError("installed runtime contains an unsupported link")
        elif mode & 0o022:
            raise ValueError("installed runtime is writable by another user")
        elif stat.S_ISDIR(info.st_mode):
            entry = _Entry(kind="directory", mode=mode)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > 536870912:
                raise ValueError("installed runtime size exceeds bound")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                    raise ValueError("installed runtime changed during verification")
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            entry = _Entry(kind="file", mode=mode, digest=digest)
        else:
            raise ValueError("installed runtime contains an unsupported member")
        entries[relative] = entry
    return entries


def seal_runtime(generation: Path, runtime_digest: str) -> None:
    """Persist the exact installed files after trusted offline installation.

    Call only while holding the installer lock, before publishing staged.json.
    This protected local seal binds installation outputs to verified artifacts;
    it is not a replacement for publisher signatures or a same-user sandbox.
    Runtime processes must disable bytecode writes to keep the seal immutable.
    """
    seal = _Seal(
        runtime_digest=runtime_digest, entries=_inventory(generation / "runtime")
    )
    write_private(
        generation / "installed-files.json",
        canonical_json(seal.model_dump(mode="json")),
    )


def verify_installed_runtime(generation: Path, runtime_digest: str) -> None:
    """Refuse changed, missing or additional files before executing private Python.

    Includes executable bytes, bytecode, startup files, permissions, directory
    membership and the resolved qualified base interpreter. No installed code
    is imported to perform this verification.
    """
    seal = _Seal.model_validate_json(
        read_private(generation / "installed-files.json", 8388608)
    )
    if seal.runtime_digest != runtime_digest or seal.entries != _inventory(
        generation / "runtime"
    ):
        raise ValueError("installed runtime integrity differs")
