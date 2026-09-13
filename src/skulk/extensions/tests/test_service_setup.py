"""Fixed system-service contracts and resumable owner-local setup without root."""

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Literal, cast

import pytest

from skulk.extensions import service_registration, service_setup
from skulk.extensions.runtime_artifacts import QualifiedHost
from skulk.extensions.runtime_attachment import HostSettings
from skulk.extensions.runtime_files import (
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.service_registration import ServiceLayout
from skulk.extensions.service_setup import (
    ServiceConnection,
    ServiceReadinessPendingError,
    SetupOperation,
    setup_service,
)
from skulk.extensions.service_snapshot import ServiceSnapshot
from skulk.extensions.tests.test_service_snapshot import staged


@pytest.mark.parametrize(
    ("state", "process_id", "stop_code", "accepted"),
    [
        ("inactive", 0, 5, True),
        ("failed", 0, 0, True),
        ("active", 12, 0, False),
        ("deactivating", 0, 5, False),
        ("inactive", 12, 0, False),
        ("inactive", 0, 1, False),
    ],
)
def test_systemd_stop_requires_confirmed_quiescence(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    process_id: int,
    stop_code: int,
    accepted: bool,
) -> None:
    """An unloaded unit can be repaired only after confirming no active process."""
    layout = service_registration.ServiceLayout(
        "ubuntu-24.04-x86_64", 1001, 1001, "owner", "owners"
    )
    monkeypatch.setattr("skulk.extensions.service_registration.os.geteuid", lambda: 0)

    def existing(_layout: service_registration.ServiceLayout) -> bytes:
        return b"verified fixed unit"

    monkeypatch.setattr(
        "skulk.extensions.service_registration._existing",
        existing,
    )

    def run(
        arguments: tuple[str, ...], **_options: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert arguments[0] == "/usr/bin/systemctl"
        if arguments[1] == "stop":
            return subprocess.CompletedProcess(arguments, stop_code)
        assert arguments[1] == "show"
        return subprocess.CompletedProcess(
            arguments, 0, f"ActiveState={state}\nMainPID={process_id}\n".encode()
        )

    monkeypatch.setattr(service_registration.subprocess, "run", run)
    if accepted:
        service_registration.register(layout, "stop")
    else:
        with pytest.raises(ValueError):
            service_registration.register(layout, "stop")


@pytest.mark.parametrize("target", ["macos-arm64", "ubuntu-24.04-x86_64"])
def test_fixed_system_definitions_and_foreign_unit_refusal(
    tmp_path: Path,
    target: Literal["macos-arm64", "ubuntu-24.04-x86_64"],
) -> None:
    """Only a nonroot system job with the exact bootstrap contract can be replaced."""
    layout = ServiceLayout(target, 1001, 1001, "owner", "owners")
    python = Path("/opt/python with space/$cash%percent/bin/python")
    definition = layout.definition(python)
    layout.verify_existing(definition)
    assert "LaunchAgents" not in str(layout.unit) and "/user/" not in str(layout.unit)
    if target == "macos-arm64":
        payload = cast(dict[str, object], plistlib.loads(definition))
        assert payload["UserName"] == "owner"
        assert payload["ProgramArguments"] == [
            str(python),
            "-I",
            "-S",
            "-B",
            str(layout.root / "service-bootstrap.py"),
            "--root",
            str(layout.root),
        ]
        assert payload["RunAtLoad"] is True and payload["KeepAlive"] is True
        payload["UserName"] = "root"
        altered = plistlib.dumps(payload, sort_keys=True)
        if sys.platform == "darwin":
            path = tmp_path / "service.plist"
            path.write_bytes(definition)
            check = subprocess.run(
                ["/usr/bin/plutil", "-lint", str(path)], capture_output=True, timeout=10
            )
            assert check.returncode == 0
    else:
        assert (
            b"User=1001\n" in definition
            and b"WantedBy=multi-user.target\n" in definition
        )
        assert b"$$cash%%percent" in definition
        assert b"KillMode=mixed\n" in definition
        assert b"WorkingDirectory=/var/lib/skulk-plugin-services/1001\n" in definition
        previous = definition.replace(
            b"WorkingDirectory=/var/lib/skulk-plugin-services/1001\n",
            b'WorkingDirectory="/var/lib/skulk-plugin-services/1001"\n',
        )
        layout.verify_existing(previous)
        with pytest.raises(ValueError):
            layout.verify_existing(previous.replace(b"User=1001", b"User=0"))
        assert (
            b"PrivateTmp=true" not in definition
        )  # API and manager share their local sockets.
        altered = definition.replace(b"User=1001", b"User=0")
    with pytest.raises(ValueError):
        layout.verify_existing(altered)
    with pytest.raises(ValueError):
        layout.verify_existing(b"")
    with pytest.raises(ValueError):
        ServiceLayout(target, 0, 0, "root", "root")


def test_registration_helper_is_standalone_and_refuses_unprivileged_mutation(
    tmp_path: Path,
) -> None:
    """The elevated entrypoint uses no package/site initialization and requires root."""
    marker = tmp_path / "site-executed"
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()"
    )
    helper = str(Path(service_registration.__file__))
    command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B", helper]
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [*command, "--help"], capture_output=True, env=environment, timeout=10
    )
    assert result.returncode == 0 and b"--uid" in result.stdout
    refused = subprocess.run(
        [*command, "prepare", "--uid", str(os.getuid())],
        capture_output=True,
        env=environment,
        timeout=10,
    )
    assert refused.returncode == 1 and b"registration failed" in refused.stderr
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["registration", "readiness"])
async def test_setup_resumes_same_snapshot_and_preserves_latest_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["registration", "readiness"],
) -> None:
    """Registration or readiness failure preserves staging and avoids healthy restarts."""
    root, config, unit = (
        tmp_path / "service",
        tmp_path / "config",
        tmp_path / "system.service",
    )
    config.mkdir(mode=0o755)
    layout = ServiceLayout("macos-arm64", os.getuid(), os.getgid(), "owner", "owners")

    def service_root(self: ServiceLayout) -> Path:
        return root

    def service_unit(self: ServiceLayout) -> Path:
        return unit

    def fixture_layout(uid: int) -> ServiceLayout:
        return layout

    monkeypatch.setattr(ServiceLayout, "root", property(service_root))
    monkeypatch.setattr(ServiceLayout, "unit", property(service_unit))
    monkeypatch.setattr("skulk.extensions.service_setup.local_layout", fixture_layout)
    monkeypatch.setattr(
        "skulk.extensions.service_setup.service_source_identity", lambda: "c" * 64
    )
    monkeypatch.setattr("skulk.extensions.service_setup.SKULK_CONFIG_HOME", config)
    monkeypatch.setattr(
        "skulk.extensions.service_setup.measure_host",
        lambda: QualifiedHost("macos-arm64", "3.13.13", "1.5.2", "b" * 64),
    )
    stages = 0
    actions: list[str] = []
    fail = True

    async def stage(path: Path) -> ServiceSnapshot:
        nonlocal stages
        stages += 1
        snapshot = staged(path)
        # A live API may renew its transport while the new core copy is prepared.
        write_private(
            root / "host.json",
            HostSettings(transport_node_id="latest-live-transport")
            .model_dump_json()
            .encode(),
        )
        return snapshot

    async def elevate(actual: ServiceLayout, action: str) -> None:
        assert actual is layout
        actions.append(action)
        if action == "prepare":
            private_directory(root)
        if action == "install":
            if fail and failure == "registration":
                raise ValueError("synthetic OS registration failure")
            unit.write_bytes(layout.definition(Path(sys.executable).resolve()))

    async def observe(path: Path) -> bool:
        return unit.exists() and not fail

    def registered_copy(
        actual: ServiceLayout, snapshot: ServiceSnapshot, base: Path
    ) -> bool:
        # Only the root-owned unit check is substituted; stage/select still verify
        # the real runtime copy. A registered service must match the fixed unit.
        return unit.exists() and unit.read_bytes() == layout.definition(base)

    monkeypatch.setattr("skulk.extensions.service_setup.stage_service_runtime", stage)
    monkeypatch.setattr("skulk.extensions.service_setup._elevate", elevate)
    monkeypatch.setattr("skulk.extensions.service_setup._observe", observe)
    monkeypatch.setattr(service_setup, "_READINESS_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(service_setup, "_ready_installation", registered_copy)
    expected_error = ValueError if failure == "registration" else ServiceReadinessPendingError
    with pytest.raises(expected_error):
        await setup_service()
    interrupted = SetupOperation.model_validate_json(read_private(root / "setup.json"))
    assert interrupted.phase == ("selected" if failure == "registration" else "registered")
    settings = HostSettings.model_validate_json(read_private(root / "host.json"))
    assert settings.transport_node_id == "latest-live-transport"
    connection = ServiceConnection.model_validate_json(
        read_private(config / "managed-service/connection.json")
    )
    assert settings.profile_id == connection.profile_id == interrupted.profile_id
    assert config.stat().st_mode & 0o777 == 0o755
    assert (config / "managed-service").stat().st_mode & 0o777 == 0o700
    fail = False
    completed = await setup_service()
    assert completed.phase == "ready"
    assert completed.operation_id == interrupted.operation_id
    assert completed.snapshot == interrupted.snapshot and stages == 1
    expected_actions = ["prepare", "stop", "install"]
    if failure == "registration":
        expected_actions += ["prepare", "stop", "install"]
    assert actions == expected_actions
    assert read_private(root / "host.json") == settings.model_dump_json().encode()

    assert await setup_service() == completed
    assert actions == expected_actions
    # Management availability alone cannot bless a tampered registration.
    unit.write_bytes(b"foreign service")
    fail = True
    with pytest.raises(expected_error):
        await setup_service()
    assert actions == [*expected_actions, "prepare", "stop", "install"]
    unit.write_bytes(layout.definition(Path(sys.executable).resolve()))
    fail = False
    assert await setup_service() == completed
    monkeypatch.setattr(
        "skulk.extensions.service_setup.SKULK_CONFIG_HOME", tmp_path / "other-profile"
    )
    with pytest.raises(ValueError, match="another Skulk configuration"):
        await setup_service()

    # A different Python/dependency environment requests a new setup generation,
    # retaining the prior completed operation and the same logical profile.
    monkeypatch.setattr("skulk.extensions.service_setup.SKULK_CONFIG_HOME", config)
    monkeypatch.setattr(
        "skulk.extensions.service_setup.service_source_identity", lambda: "d" * 64
    )

    async def new_source(path: Path) -> ServiceSnapshot:
        raise ValueError("new source requires fresh staging")

    monkeypatch.setattr(
        "skulk.extensions.service_setup.stage_service_runtime", new_source
    )
    with pytest.raises(ValueError, match="fresh staging"):
        await setup_service()
    next_operation = SetupOperation.model_validate_json(
        read_private(root / "setup.json")
    )
    assert next_operation.operation_id != completed.operation_id
    assert next_operation.profile_id == completed.profile_id
    assert next_operation.phase == "preparing"
    retained = SetupOperation.model_validate_json(
        read_private(root / "setup-operations" / (completed.operation_id + ".json"))
    )
    assert retained == completed

    # A broken preparation must not require the owner to reinstall the broken
    # build merely to finish it before applying a correction.
    monkeypatch.setattr(
        "skulk.extensions.service_setup.service_source_identity", lambda: "e" * 64
    )
    actions_before = list(actions)
    with pytest.raises(ValueError, match="fresh staging"):
        await setup_service()
    repaired = SetupOperation.model_validate_json(read_private(root / "setup.json"))
    assert repaired.operation_id != next_operation.operation_id
    assert repaired.profile_id == completed.profile_id
    assert repaired.phase == "preparing"
    assert actions == [*actions_before, "prepare"]
    assert (
        SetupOperation.model_validate_json(
            read_private(
                root / "setup-operations" / (next_operation.operation_id + ".json")
            )
        )
        == next_operation
    )


def test_pending_readiness_cli_identifies_retained_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Late readiness reports registration and a safe recovery action, not reinstall."""
    identifier = "a" * 32

    async def pending() -> SetupOperation:
        raise ServiceReadinessPendingError(identifier)

    monkeypatch.setattr(service_setup, "setup_service", pending)
    monkeypatch.setattr(sys, "argv", ["skulk-plugin-service", "setup"])
    with pytest.raises(SystemExit, match="1"):
        service_setup.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "operation_id": identifier,
        "phase": "registered",
        "error_code": "service_readiness_pending",
    }
    assert "skulk-plugin-service status" in output.err
    assert "without restarting" in output.err
    assert "command incomplete" not in output.err
