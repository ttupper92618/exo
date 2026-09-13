"""Standalone standard-library helper for explicit local OS service registration.

Run this file directly with isolated base Python and site initialization disabled.
It never imports Skulk or a plugin as root and accepts no service commands, unit
contents, executable paths or storage destinations from its caller.
"""

import argparse
import contextlib
import grp
import json
import os
import platform
import plistlib
import pwd
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, final
from uuid import uuid4

ServicePlatform = Literal["macos-arm64", "ubuntu-24.04-x86_64"]


def service_platform() -> ServicePlatform:
    """Require a release-qualified local OS and processor architecture."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "macos-arm64"
    if (
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and platform.freedesktop_os_release().get("ID") == "ubuntu"
        and platform.freedesktop_os_release().get("VERSION_ID") == "24.04"
    ):
        return "ubuntu-24.04-x86_64"
    raise ValueError(
        "plugin services require Apple Silicon macOS or Ubuntu 24.04 x86_64"
    )


@final
@dataclass(frozen=True)
class ServiceLayout:
    """Fixed per-owner service identity and durable system-owned parent directory."""

    platform: ServicePlatform
    user_id: int
    group_id: int
    username: str
    groupname: str

    def __post_init__(self) -> None:
        if not 0 < self.user_id < 2147483648 or not 0 <= self.group_id < 2147483648:
            raise ValueError("service owner must be nonroot")
        if any(
            not name or len(name) > 128 or any(ord(c) < 32 for c in name)
            for name in (self.username, self.groupname)
        ):
            raise ValueError("invalid OS account identity")

    @property
    def root(self) -> Path:
        """Return a stable service-owned leaf outside Git and login-session storage."""
        parent = (
            Path("/Library/Application Support/SkulkPluginServices")
            if self.platform == "macos-arm64"
            else Path("/var/lib/skulk-plugin-services")
        )
        return parent / str(self.user_id)

    @property
    def label(self) -> str:
        """Return the reserved per-owner system service name."""
        return f"foundation.foxlight.skulk.plugins.u{self.user_id}"

    @property
    def unit(self) -> Path:
        """Return the fixed system registration file, never a login-agent location."""
        if self.platform == "macos-arm64":
            return Path("/Library/LaunchDaemons") / (self.label + ".plist")
        return Path("/etc/systemd/system") / (self.label + ".service")

    def definition(self, python: Path) -> bytes:
        """Render an exact nonroot OS definition with a fixed verified bootstrap."""
        if not python.is_absolute() or any(ord(c) < 32 for c in str(python)):
            raise ValueError("invalid base interpreter path")
        arguments = [
            str(python),
            "-I",
            "-S",
            "-B",
            str(self.root / "service-bootstrap.py"),
            "--root",
            str(self.root),
        ]
        if self.platform == "macos-arm64":
            return plistlib.dumps(
                {
                    "Label": self.label,
                    "UserName": self.username,
                    "GroupName": self.groupname,
                    "ProgramArguments": arguments,
                    "WorkingDirectory": str(self.root),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "ThrottleInterval": 10,
                    "ExitTimeOut": 180,
                    "Umask": 0o077,
                    "ProcessType": "Background",
                    "StandardOutPath": "/dev/null",
                    "StandardErrorPath": "/dev/null",
                },
                sort_keys=True,
            )
        command = " ".join(_systemd_argument(value) for value in arguments)
        return (
            f"# skulk-plugin-service-v1 {json.dumps(str(python))}\n"
            "[Unit]\nDescription=Skulk managed plugin service\nAfter=local-fs.target\n"
            "StartLimitIntervalSec=0\n\n[Service]\nType=exec\n"
            f"User={self.user_id}\nGroup={self.group_id}\n"
            # WorkingDirectory takes a path, not ExecStart's quoted argument list.
            # This root is fixed ASCII with only a numeric owner suffix.
            f"WorkingDirectory={self.root}\nExecStart={command}\n"
            "Restart=always\nRestartSec=10\nTimeoutStopSec=180\nKillMode=mixed\n"
            "UMask=0077\nNoNewPrivileges=true\nLimitCORE=0\n"
            "StandardOutput=null\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n"
        ).encode()

    def verify_existing(self, content: bytes) -> None:
        """Refuse an unrelated unit occupying the reserved name before any OS action."""
        if len(content) > 65536:
            raise ValueError("service definition exceeds bound")
        if self.platform == "macos-arm64":
            payload = cast(object, plistlib.loads(content))
            if not isinstance(payload, dict):
                raise ValueError("invalid existing service")
            arguments: object = cast(dict[str, object], payload).get("ProgramArguments")
            if (
                not isinstance(arguments, list)
                or not arguments
                or not isinstance(arguments[0], str)
            ):
                raise ValueError("invalid existing service arguments")
            python = arguments[0]
        else:
            lines = content.decode().splitlines()
            if not lines:
                raise ValueError("existing service definition is empty")
            first = lines[0]
            prefix = "# skulk-plugin-service-v1 "
            if not first.startswith(prefix):
                raise ValueError("existing service is not managed by local setup")
            value = cast(object, json.loads(first[len(prefix) :]))
            if not isinstance(value, str):
                raise ValueError("invalid existing interpreter identity")
            python = value
        expected = self.definition(Path(python))
        if self.platform == "ubuntu-24.04-x86_64":
            # Permit repair of only the exact prior generated unit. systemd
            # rejects its quoted WorkingDirectory before starting any process.
            previous = expected.replace(
                f"WorkingDirectory={self.root}\n".encode(),
                f'WorkingDirectory="{self.root}"\n'.encode(),
                1,
            )
            if content == previous:
                return
        if content != expected:
            raise ValueError("existing service definition differs from fixed contract")


def _systemd_argument(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        + '"'
    )


def local_layout(user_id: int) -> ServiceLayout:
    """Resolve a local nonroot account using the OS database, not caller-supplied names."""
    account = pwd.getpwuid(user_id)
    return ServiceLayout(
        service_platform(),
        user_id,
        account.pw_gid,
        account.pw_name,
        grp.getgrgid(account.pw_gid).gr_name,
    )


def _directory(path: Path) -> int:
    # Walk from / using directory descriptors; never follow an owner-controlled
    # link while the explicit setup helper has elevated filesystem authority.
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            created = False
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            if created:
                # Explicitly set traversal regardless of the invoking owner's
                # restrictive umask; runtime leaves remain owner-only below.
                os.fchmod(child, 0o755)
                os.fsync(child)
                os.fsync(descriptor)
            info = os.fstat(child)
            if info.st_uid != 0 or info.st_mode & 0o022:
                os.close(child)
                raise ValueError("system service parent is not root protected")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _existing(layout: ServiceLayout) -> bytes | None:
    parent = _directory(layout.unit.parent)
    try:
        try:
            descriptor = os.open(
                layout.unit.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise ValueError("system service definition is not root protected")
            content = source.read(65537)
        layout.verify_existing(content)
        return content
    finally:
        os.close(parent)


def _execute(arguments: tuple[str, ...], allowed: tuple[int, ...] = (0,)) -> None:
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=195,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if result.returncode not in allowed:
        raise ValueError("OS service operation failed; inspect system service status")


def _stop_systemd(unit: str) -> None:
    # A rejected generated unit reports "not loaded" (5), even though it has no
    # process to stop. Independently confirm quiescence before any replacement.
    _execute(("/usr/bin/systemctl", "stop", unit), (0, 5))
    result = subprocess.run(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=ActiveState",
            "--property=MainPID",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if result.returncode != 0 or set(result.stdout.splitlines()) not in (
        {b"ActiveState=inactive", b"MainPID=0"},
        {b"ActiveState=failed", b"MainPID=0"},
    ):
        raise ValueError("system service has not stopped")


def register(
    layout: ServiceLayout, action: Literal["prepare", "stop", "install"]
) -> None:
    """Perform one fixed root setup action; runtime and provider work stay nonroot."""
    if os.geteuid() != 0:
        raise ValueError("service registration requires explicit local elevation")
    if action == "prepare":
        parent = _directory(layout.root.parent)
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(layout.root.name, 0o700, dir_fd=parent)
            child = os.open(
                layout.root.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            try:
                info = os.fstat(child)
                if info.st_uid not in (0, layout.user_id) or info.st_mode & 0o077:
                    raise ValueError("existing service state ownership differs")
                if info.st_uid == 0 and os.listdir(child):
                    raise ValueError(
                        "refusing to adopt existing root-owned service data"
                    )
                os.fchown(child, layout.user_id, layout.group_id)
                os.fchmod(child, 0o700)
                os.fsync(child)
            finally:
                os.close(child)
            os.fsync(parent)
        finally:
            os.close(parent)
        return
    existing = _existing(layout)
    if action == "stop":
        if existing is None:
            return
        if layout.platform == "macos-arm64":
            _execute(
                ("/bin/launchctl", "bootout", "system/" + layout.label), (0, 3, 113)
            )
        else:
            _stop_systemd(layout.unit.name)
        return
    definition = layout.definition(Path(sys.executable).resolve(strict=True))
    parent = _directory(layout.unit.parent)
    temporary = "." + layout.unit.name + "." + uuid4().hex
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o644)
            target.write(definition)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, layout.unit.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent)
    if layout.platform == "macos-arm64":
        _execute(("/bin/launchctl", "enable", "system/" + layout.label))
        # Setup stops the old service before activating a new copied runtime.
        _execute(("/bin/launchctl", "bootstrap", "system", str(layout.unit)))
    else:
        _execute(("/usr/bin/systemctl", "daemon-reload"))
        _execute(("/usr/bin/systemctl", "enable", "--now", layout.unit.name))


def main() -> None:
    """Register only the fixed nonroot service for an explicitly selected local UID."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "stop", "install"))
    parser.add_argument("--uid", type=int, required=True)
    arguments = parser.parse_args()
    try:
        register(
            local_layout(cast(int, arguments.uid)),
            cast(Literal["prepare", "stop", "install"], arguments.action),
        )
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print(
            "Local plugin service registration failed; inspect owner and system service status.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
