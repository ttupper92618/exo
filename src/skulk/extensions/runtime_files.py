"""Protected local files and exclusive ownership for managed plugin runtimes."""

import fcntl
import os
import stat
from pathlib import Path
from typing import final
from uuid import uuid4


def private_directory(path: Path) -> None:
    """Create or validate an owner-only directory without accepting a symlink."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("runtime directory must be owner-only")


def read_private(path: Path, limit: int = 131072) -> bytes:
    """Read bounded bytes from an owner-only regular file, never a symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("runtime file must be owner-only")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(limit + 1)
        if len(content) > limit:
            raise ValueError("runtime file exceeds bound")
        return content
    finally:
        os.close(descriptor)


def write_private(path: Path, content: bytes) -> None:
    """Atomically replace a private file and sync its contents and directory."""
    private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@final
class RuntimeLock:
    """Hold a nonblocking file lock until all owned runtime work has finished."""

    def __init__(self, root: Path, name: str = "installer.lock") -> None:
        """Fence this service-owned directory using a fixed local lock name."""
        if name not in {
            "installer.lock",
            "supervisor.lock",
            "service.lock",
            "manager.lock",
            "attachment.lock",
            "setup.lock",
        }:
            raise ValueError("unknown runtime lock")
        private_directory(root)
        self.descriptor = os.open(
            root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("runtime lock must be owner-only")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.descriptor)
            raise

    def close(self) -> None:
        """Release exclusive ownership after child and disk work has completed."""
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
