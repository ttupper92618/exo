"""Guided terminal installation against real manager IPC and signed offline wheels."""

import asyncio
import getpass
import io
import json
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from skulk.extensions.runtime_artifacts import RuntimeTrust
from skulk.extensions.runtime_attachment import HostSettings, ServiceConnection
from skulk.extensions.runtime_download import RuntimeDownloads
from skulk.extensions.runtime_files import (
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_manager import (
    InstallationRequest,
    InstallSubmission,
    ManagerRequest,
    RuntimeManager,
    SourceRegistration,
    SubmitRequest,
    manager_request,
)
from skulk.extensions.terminal_install import TerminalInstaller
from skulk.extensions.tests.test_runtime_install import artifacts
from skulk.extensions.tests.test_runtime_service import OWNER_SOURCE


@dataclass
class Journey:
    """External HTTPS/terminal boundaries around the real manager and installer."""

    manager: RuntimeManager
    trust: RuntimeTrust
    fail_response: str | None = None
    fail_download: bool = False
    requests: list[ManagerRequest] = field(default_factory=list)
    output: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    async def request(self, request: ManagerRequest) -> dict[str, JsonValue]:
        """Lose a chosen accepted response without cancelling its manager task."""
        self.requests.append(request)
        response = await manager_request(self.manager.root, request)
        if request.action == self.fail_response:
            self.fail_response = None
            raise OSError("external connection lost")
        return response

    def fields(self, *decisions: str) -> Iterator[str]:
        """Provide external trust/source inputs, without any internal identifiers."""
        return iter(
            [
                "https://releases.example.test/",
                "",
                "fixture",
                self.trust.publishers["fixture"],
                datetime.fromtimestamp(self.trust.expires_at, UTC).isoformat(),
                *decisions,
            ]
        )

    def terminal(self, answers: Iterator[str]) -> TerminalInstaller:
        """Retain displayed output while never reflecting hidden credential input."""

        def prompt(_question: str) -> str:
            return next(answers)

        return TerminalInstaller(
            self.request,
            prompt,
            lambda _question: "hidden-test-feed-token",
            self.output.append,
            lambda _seconds: asyncio.sleep(0.01),
        )

    @property
    def identifier(self) -> str:
        """Read the generated identity from the first registration request."""
        request = self.requests[0]
        assert isinstance(request, InstallationRequest)
        return request.plugin_id


@asynccontextmanager
async def journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Journey]:
    """Use real private sockets, dependency installation and supervised processes."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source, owner_source=OWNER_SOURCE)
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    root = tmp_path / "manager"
    private_directory(root)
    write_private(
        root / "host.json",
        HostSettings(transport_node_id="fixture-peer").model_dump_json().encode(),
    )
    manager = RuntimeManager(root)
    fixture = Journey(manager, trust)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer hidden-test-feed-token"
        fixture.urls.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[-1]
        if fixture.fail_download and name != "release.json":
            return httpx.Response(503)
        return httpx.Response(
            200,
            content=metadata if name == "release.json" else read_private(source / name),
        )

    def downloads(root: Path) -> RuntimeDownloads:
        return RuntimeDownloads(root, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("skulk.extensions.runtime_manager.RuntimeDownloads", downloads)
    await manager.start()
    try:
        yield fixture
    finally:
        await manager.close()


async def test_generated_identity_trust_permissions_and_real_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External inputs reach verified staging and activation without JSON or paths."""
    async with journey(tmp_path, monkeypatch) as fixture:
        identifier = await fixture.terminal(fixture.fields("y", "y", "y")).run()
        assert identifier.startswith("managed.") and len(identifier) == 40
        selection = fixture.manager.controllers[identifier].selector.current()
        assert selection is not None and selection.enabled
        assert selection.revision == 1
        assert len(fixture.urls) == 3
        assert "hidden-test-feed-token" not in "\n".join(fixture.output)
        assert fixture.output[0].endswith(identifier)
        assert any("local synthetic operation" in line for line in fixture.output)
        assert not any("RunPod" in line for line in fixture.output)
        await fixture.terminal(iter(())).run(identifier)
        assert sum(isinstance(r, InstallSubmission) for r in fixture.requests) == 1
        assert sum(isinstance(r, SubmitRequest) for r in fixture.requests) == 1
        assert sum(isinstance(r, SourceRegistration) for r in fixture.requests) == 1


@pytest.mark.parametrize(
    "lost_action", ["register", "configure_source", "install", "submit"]
)
async def test_lost_accepted_response_resumes_without_repeating_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_action: str
) -> None:
    """A reconnect discovers the original operation after its reply is lost."""
    async with journey(tmp_path, monkeypatch) as fixture:
        fixture.fail_response = lost_action
        answers = fixture.fields("y", "y", "y")
        with pytest.raises(OSError, match="connection lost"):
            await fixture.terminal(answers).run()
        identifier = fixture.identifier
        downloads = fixture.manager.downloads[identifier]
        if downloads.work is not None:
            await downloads.work
        controller = fixture.manager.controllers[identifier]
        await fixture.terminal(answers).run(identifier)
        assert sum(isinstance(r, InstallSubmission) for r in fixture.requests) == 1
        assert sum(isinstance(r, SubmitRequest) for r in fixture.requests) == 1
        assert sum(isinstance(r, SourceRegistration) for r in fixture.requests) == 1
        selection = controller.selector.current()
        assert selection is not None and selection.enabled and selection.revision == 1


@pytest.mark.parametrize("decline", ["trust", "install", "activate"])
async def test_distinct_owner_decisions_do_not_imply_later_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decline: str
) -> None:
    """Declining trust, download or owner execution leaves subsequent effects absent."""
    decisions = {
        "trust": ("n",),
        "install": ("y", "n"),
        "activate": ("y", "y", "n"),
    }
    async with journey(tmp_path, monkeypatch) as fixture:
        identifier = await fixture.terminal(fixture.fields(*decisions[decline])).run()
        assert fixture.manager.controllers[identifier].selector.current() is None
        assert not any(isinstance(r, SubmitRequest) for r in fixture.requests)
        if decline == "trust":
            assert not any(isinstance(r, SourceRegistration) for r in fixture.requests)
            assert not fixture.urls
        if decline != "activate":
            assert not any(isinstance(r, InstallSubmission) for r in fixture.requests)


async def test_download_failure_requires_explicit_same_operation_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure never starts another download until the owner approves recovery."""
    async with journey(tmp_path, monkeypatch) as fixture:
        fixture.fail_download = True
        with pytest.raises(ValueError, match="explicit recovery"):
            await fixture.terminal(fixture.fields("y", "y")).run()
        identifier = fixture.identifier
        downloads = fixture.manager.downloads[identifier]
        original = downloads.current()
        assert original is not None and original.state == "recovery_required"
        before = list(fixture.urls)
        await fixture.terminal(iter(["n"])).run(identifier)
        assert fixture.urls == before
        assert downloads.current() == original
        fixture.fail_download = False
        await fixture.terminal(iter(["y", "y"])).run(identifier)
        recovered = downloads.current()
        assert recovered is not None and recovered.state == "staged"
        assert recovered.request == original.request and recovered.attempt == 1
        assert sum(isinstance(r, InstallSubmission) for r in fixture.requests) == 1


async def test_invalid_source_credential_and_unknown_identifier_are_not_disclosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation rejects unsafe inputs without displaying credential contents."""
    async with journey(tmp_path, monkeypatch) as fixture:
        terminal = fixture.terminal(fixture.fields("y"))
        terminal = TerminalInstaller(
            terminal.request,
            terminal.prompt,
            lambda _: "secret\ninvalid",
            terminal.output,
        )
        with pytest.raises(ValueError):
            await terminal.run()
        assert not any(isinstance(r, SourceRegistration) for r in fixture.requests)
        assert "secret" not in json.dumps(fixture.output)
        with pytest.raises(ValueError):
            await fixture.terminal(iter(())).run("../../foreign")


def test_credential_prompt_refuses_echo_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable secure terminal cannot silently turn a secret prompt into input."""
    from skulk.extensions.service_setup import read_hidden_credential

    read = False

    def fallback(_prompt: str) -> str:
        nonlocal read
        warnings.warn("cannot disable echo", getpass.GetPassWarning, stacklevel=2)
        read = True
        return "must-not-be-read"

    monkeypatch.setattr("skulk.extensions.service_setup.getpass.getpass", fallback)
    with pytest.raises(getpass.GetPassWarning):
        read_hidden_credential("Credential: ")
    assert not read


def test_cli_lost_response_directs_owner_to_generated_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entrypoint must not recommend a new installation after an uncertain reply."""
    from skulk.extensions import service_setup

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    connection = ServiceConnection(manager_root=str(tmp_path), profile_id="a" * 32)
    monkeypatch.setattr(
        service_setup.sys, "argv", ["skulk-plugin-service", "install-plugin"]
    )
    monkeypatch.setattr(service_setup.sys, "stdin", Terminal())

    def read_connection(_path: Path, _limit: int) -> bytes:
        return connection.model_dump_json().encode()

    monkeypatch.setattr(service_setup, "read_private", read_connection)

    async def lost(_root: Path, _request: ManagerRequest) -> dict[str, JsonValue]:
        raise OSError("sensitive connection details")

    monkeypatch.setattr(service_setup, "manager_request", lost)
    with pytest.raises(SystemExit) as stopped:
        service_setup.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert "Resume: skulk-plugin-service install-plugin managed." in captured.out
    assert "printed resume command with its installation ID" in captured.err
    assert "Rerun the same local command" not in captured.err
    assert "sensitive connection details" not in captured.err
