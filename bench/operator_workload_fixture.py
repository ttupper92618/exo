"""Opt-in loopback fixture for observing unchanged released operator clients.

Creates only fresh local authority and relay state, with a bounded lifetime.
The physical-device workload recorder is a separate integration; this command
does not claim capacity qualification or export captured user data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import ssl
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, cast, final
from urllib.parse import urlsplit

import aiohttp
import hypercorn.asyncio as hypercorn_asyncio
import qrcode
from hypercorn.config import Config
from hypercorn.typing import ASGIFramework
from pydantic import BaseModel, ConfigDict, Field
from qrcode.constants import ERROR_CORRECT_L

from bench.operator_fixture_app import create_fixture_app
from bench.operator_fixture_observer import FixtureObserver
from bench.operator_fixture_proxy import observation_proxy
from skulk.operator.authority import EncryptedAuthorityStore
from skulk.operator.key_provider import LocalFileAuthorityKeyProvider
from skulk.operator.pairing import OperatorPairingService
from skulk.operator.relay import (
    OperatorGatewayConnector,
    OperatorRelayConfiguration,
    OperatorRelayConfigurationRepository,
    OperatorRelayProvisioning,
)


class _HypercornServe(Protocol):
    def __call__(
        self,
        app: ASGIFramework,
        config: Config,
        *,
        shutdown_trigger: Callable[[], Awaitable[object]] | None = None,
        mode: Literal["asgi", "wsgi"] | None = None,
    ) -> Coroutine[object, object, None]: ...


_serve = cast(_HypercornServe, hypercorn_asyncio.serve)


class FixtureSettings(BaseModel):
    """Local-only fixture inputs; no remote URL or existing authority is accepted."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    relay_binary: Path = Field(description="Explicit local on-demand relay executable.")
    relay_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="Expected executable digest."
    )
    lifetime_seconds: int = Field(
        default=600, ge=60, le=7200, description="Whole fixture lease, including setup."
    )


@dataclass(frozen=True)
class RunningFixture:
    """Ephemeral local handles; never serialize this object as campaign evidence."""

    directory: Path
    relay_port: int
    configuration: OperatorRelayConfiguration
    pairing_service: OperatorPairingService


class FixtureLeaseExpiredError(Exception):
    """The whole-session deadline cancelled an otherwise clean fixture."""


class PrivateFixtureIngress(Protocol):
    """Own an expiring, device-allowlisted private TLS bridge to a generated port.

    Implementations must bind their upstream to the supplied loopback relay port,
    restrict callers before forwarding, enforce bounded memory and traffic, and
    independently expire on parent death. Exit must verify bridge removal. The
    yielded origin is advertised for both app and gateway roles; no authority or
    inner TLS validation is bypassed. This hook is not exposed by the CLI.
    """

    def __call__(self, relay_port: int, /) -> AbstractAsyncContextManager[str]:
        """Start private ingress for this generated port, yielding its WSS origin."""
        ...


@final
@dataclass(frozen=True)
class PublicFixtureIngress:
    """Explicit owned test ingress, separate from the private pilot contract.

    `run_id` is a fresh 128-bit lowercase hex identifier and `origin` must name
    its dedicated rehearsal host. `open` must expose only its supplied generated
    carrier port, enforce bounded bytes/connections, independently expire on
    controller death, and verify tunnel cleanup on exit. These are injected
    effect obligations, not properties attested by hostname validation.
    No existing cluster configuration or authority is accepted.
    """

    run_id: str
    origin: str
    open: Callable[[int], AbstractAsyncContextManager[str]]


def validate_public_fixture_origin(origin: str, run_id: str) -> str:
    """Require a canonical WSS origin bound to a dedicated rehearsal hostname.

    This is a fail-closed target naming gate, not proof of DNS ownership or
    provider cleanup. No arbitrary existing relay URL, path, explicit port,
    credential, fragment, cleartext or private-pilot hostname is accepted.
    """
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise ValueError("invalid public fixture run identifier")
    prefix = f"wss://rehearsal-{run_id}."
    suffix = origin.removeprefix(prefix)
    if (
        not origin.startswith(prefix)
        or re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", suffix
        ) is None
        or len(origin) > 259
        or suffix.endswith(".ts.net")
    ):
        raise ValueError("public fixture requires its dedicated rehearsal WSS origin")
    return origin


def validate_private_fixture_origin(origin: str) -> str:
    """Accept only an explicit HTTPS-capable tailnet origin for the pilot bridge.

    This validates syntax, not Serve access policy. The injected ingress owner
    must separately prohibit Funnel and allowlist the selected devices. Arbitrary
    public relay URLs, credentials, paths, fragments, and cleartext are rejected.
    """
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "wss"
        or parsed.hostname is None
        or not parsed.hostname.endswith(".ts.net")
        or len(parsed.hostname.split(".")) != 4
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or any(character.isspace() for character in origin)
        or origin != f"wss://{parsed.hostname}:{parsed.port}"
    ):
        raise ValueError(
            "fixture private ingress requires an explicit tailnet WSS origin"
        )
    return origin


@asynccontextmanager
async def fixture_lease(lifetime_seconds: float) -> AsyncIterator[None]:
    """Translate only this lease's cancellation, never inner or cleanup timeouts.

    An inner TimeoutError must retain its failure status even if cleanup runs
    past the session deadline. Catch the lease cancellation before asyncio
    converts it to the indistinguishable builtin TimeoutError.
    """
    async with asyncio.timeout(lifetime_seconds) as lease:
        try:
            yield
        except asyncio.CancelledError:
            if lease.expired():
                raise FixtureLeaseExpiredError from None
            raise


@final
class _Tls13Configuration(Config):
    def create_ssl_context(self) -> ssl.SSLContext | None:
        """Require pinned inner TLS 1.3, including in this synthetic listener."""
        context = super().create_ssl_context()
        if context is not None:
            context.minimum_version = ssl.TLSVersion.TLSv1_3
        return context


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], listener.getsockname())[1]


def verify_binary(settings: FixtureSettings) -> Path:
    """Verify the exact executable before creating authority, files, or sockets."""
    binary = settings.relay_binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("fixture relay must be an executable file")
    with binary.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != settings.relay_sha256:
        raise ValueError("fixture relay digest mismatch")
    return binary


def copy_verified_fixture_binary(settings: FixtureSettings, destination: Path) -> Path:
    """Bind relay execution to the verified bytes in a protected temporary file.

    Reads `settings.relay_binary` with a 512-MiB bound, hashes the bytes while
    copying, and compares `settings.relay_sha256` before granting execute mode.
    `destination` must be a new file inside the fixture-owned private directory.
    Returns that path for both provisioning and service launch. A caller must
    remove the temporary directory after errors as well as normal completion.
    """
    digest = hashlib.sha256()
    copied = 0
    with settings.relay_binary.open("rb") as source, destination.open("xb") as target:
        destination.chmod(0o600)
        while chunk := source.read(1024**2):
            copied += len(chunk)
            if copied > 512 * 1024**2:
                raise ValueError("fixture relay binary exceeds limit")
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != settings.relay_sha256:
        raise ValueError("fixture relay digest mismatch")
    destination.chmod(0o500)
    return destination


def _private_file(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)


def _private_qr(path: Path, value: str) -> None:
    code = qrcode.QRCode(border=4, error_correction=ERROR_CORRECT_L)
    code.add_data(value)
    code.make(fit=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        code.make_image().save(output)


async def wait_fixture_relay(
    port: int, guardian: asyncio.subprocess.Process, *, attempts: int = 100
) -> None:
    """Probe local route readiness while the owning guardian remains alive.

    The caller owns the whole-session deadline. Public fixture startup supplies
    more bounded attempts inside its additional 120-second deadline; local and
    private callers preserve the original retry count.
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
        for _ in range(attempts):
            if guardian.returncode is not None:
                raise RuntimeError("fixture relay exited before readiness")
            try:
                async with client.get(
                    f"http://127.0.0.1:{port}/readyz", allow_redirects=False
                ) as response:
                    if response.status == 204:
                        return
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError("fixture relay did not become ready")


async def _stop_guardian(guardian: asyncio.subprocess.Process) -> None:
    if guardian.stdin is not None:
        guardian.stdin.close()
    # The independent guardian owns termination and escalation of its relay.
    # Do not kill it before it has had time to reap that child.
    await asyncio.wait_for(guardian.wait(), timeout=8)


async def wait_fixture_gateway(
    port: int,
    configuration: OperatorRelayConfiguration,
    listener: asyncio.Task[None],
) -> None:
    """Prove the local TLS listener is ready before publishing pairing readiness.

    `port` is the generated backend listener, bypassing observation so readiness
    probes never enter measured app flows. `configuration` pins its certificate
    and hostname; `listener` must remain alive. Returns after a verified TLS 1.3
    handshake or raises within five seconds. Connections are always closed.
    """
    context = ssl.create_default_context(cafile=str(configuration.certificate_path))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    async with asyncio.timeout(5):
        while True:
            if listener.done():
                listener.result()
                raise RuntimeError("fixture gateway exited before readiness")
            try:
                _, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    port,
                    ssl=context,
                    server_hostname=configuration.gateway_server_name,
                    ssl_handshake_timeout=1,
                )
            except (ConnectionError, TimeoutError):
                await asyncio.sleep(0.05)
                continue
            try:
                if listener.done():
                    listener.result()
                    raise RuntimeError("fixture gateway exited before readiness")
                return
            finally:
                writer.close()
                await writer.wait_closed()


async def _watch_guardian(guardian: asyncio.subprocess.Process) -> None:
    await guardian.wait()
    raise RuntimeError("fixture relay lease ended unexpectedly")


@asynccontextmanager
async def isolated_fixture(
    settings: FixtureSettings,
    *,
    observer: FixtureObserver | None = None,
    private_ingress: PrivateFixtureIngress | None = None,
    public_ingress: PublicFixtureIngress | None = None,
) -> AsyncIterator[RunningFixture]:
    """Start real local relay/gateway/auth with synthetic API bodies, then reap all.

    The context cancels at its whole-session deadline. Temporary keys are never
    printed and are removed after the gateway and relay stop. A separate relay
    watchdog also observes parent death and expiry. No production URL is accepted.
    Optional `observer` adds a bounded opaque TCP bridge before the local TLS
    listener and fixed-category ASGI counters; it never changes released apps.
    Optional `private_ingress` owns an expiring private TLS bridge to the generated
    loopback relay. All advertised roles use that same origin. Default behavior
    and CLI remain loopback-only. Ingress must be removed even if provisioning
    fails, and cannot accept existing authority or a production relay target.
    Optional `public_ingress` is mutually exclusive with the private hook and
    requires a dedicated run-bound hostname, a lease no longer than one hour,
    and independently owned bounded ingress/cleanup. Neither CLI enables it.
    """
    if public_ingress is not None:
        if private_ingress is not None or settings.lifetime_seconds > 3600:
            raise ValueError("public fixture requires one ingress and a one-hour lease")
        validate_public_fixture_origin(public_ingress.origin, public_ingress.run_id)
    verify_binary(settings)
    deadline = asyncio.get_running_loop().time() + settings.lifetime_seconds
    async with fixture_lease(settings.lifetime_seconds), AsyncExitStack() as stack:
        with TemporaryDirectory(prefix="skulk-operator-fixture-") as temporary:
            directory = Path(temporary)
            binary = copy_verified_fixture_binary(settings, directory / "relay-binary")
            relay_port, gateway_port = _available_port(), _available_port()
            while gateway_port == relay_port:
                gateway_port = _available_port()
            backend_port = gateway_port
            if observer is not None:
                gateway_port = await stack.enter_async_context(
                    observation_proxy(observer, backend_port)
                )
            relay_path = directory / "relay.json"
            provisioning_path = directory / "provisioning.json"
            relay_origin = f"ws://127.0.0.1:{relay_port}"
            if private_ingress is not None:
                relay_origin = validate_private_fixture_origin(
                    await stack.enter_async_context(private_ingress(relay_port))
                )
            elif public_ingress is not None:
                opened_origin = await stack.enter_async_context(
                    public_ingress.open(relay_port)
                )
                if opened_origin != public_ingress.origin:
                    raise ValueError("public fixture ingress origin changed")
                relay_origin = opened_origin
            provisioning_process = await asyncio.create_subprocess_exec(
                str(binary),
                "provision-on-demand",
                relay_origin,
                str(relay_path),
                str(provisioning_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                if await asyncio.wait_for(provisioning_process.wait(), 10) != 0:
                    raise RuntimeError("fixture provisioning failed")
            finally:
                if provisioning_process.returncode is None:
                    provisioning_process.kill()
                    await provisioning_process.wait()
            document = cast(dict[str, object], json.loads(relay_path.read_text()))
            document["bind"] = f"127.0.0.1:{relay_port}"
            relay_path.write_text(json.dumps(document))
            provider = LocalFileAuthorityKeyProvider(directory / "authority-key.bin")
            authority = EncryptedAuthorityStore(
                provider, directory / "authority.sqlite3"
            )
            repository = OperatorRelayConfigurationRepository(
                authority,
                certificate_path=directory / "tls.pem",
                private_key_path=directory / "tls-key.pem",
            )
            service = OperatorPairingService(
                authority,
                provider,
                relay_repository=repository,
            )
            configuration = service.configure_relay(
                OperatorRelayProvisioning.model_validate_json(
                    provisioning_path.read_text()
                ),
                operator_api_port=gateway_port,
                cluster_name="Fixture",
            )
            configuration.server_ssl_context()
            server = _Tls13Configuration()
            server.bind = [f"127.0.0.1:{backend_port}"]
            server.certfile = str(configuration.certificate_path)
            server.keyfile = str(configuration.private_key_path)
            server.accesslog = None
            server.errorlog = None
            server.alpn_protocols = ["http/1.1"]
            server.graceful_timeout = 2
            server.shutdown_timeout = 2
            server.read_timeout = 20
            server.ssl_handshake_timeout = 10
            shutdown = asyncio.Event()
            guardian = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "bench.operator_fixture_lease",
                str(binary),
                str(relay_path),
                str(max(0.001, deadline - asyncio.get_running_loop().time())),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.TaskGroup() as group:
                    monitor = group.create_task(_watch_guardian(guardian))
                    listener = group.create_task(
                        _serve(
                            cast(
                                ASGIFramework,
                                create_fixture_app(service)
                                if observer is None
                                else observer.wrap(create_fixture_app(service)),
                            ),
                            server,
                            shutdown_trigger=shutdown.wait,
                        )
                    )
                    connector = group.create_task(
                        OperatorGatewayConnector(
                            configuration,
                            next_connector_generation=service.reserve_relay_connector_generation,
                        ).run()
                    )
                    try:
                        await wait_fixture_gateway(
                            backend_port, configuration, listener
                        )
                        if public_ingress is None:
                            await wait_fixture_relay(relay_port, guardian)
                        else:
                            # The hook starts before the carrier exists. Its
                            # fresh tunnel can still be provisioning while the
                            # gateway retries; do not apply the local-only burst
                            # deadline to this explicitly public test path.
                            public_deadline = min(
                                deadline, asyncio.get_running_loop().time() + 120
                            )
                            async with asyncio.timeout_at(public_deadline):
                                await wait_fixture_relay(relay_port, guardian, attempts=2400)
                        invitation = service.create_invitation(
                            lifetime=timedelta(seconds=settings.lifetime_seconds),
                            max_pairings=4,
                        )
                        _private_file(directory / "pairing.txt", invitation.as_url())
                        _private_qr(directory / "pairing.png", invitation.as_url())
                        yield RunningFixture(
                            directory, relay_port, configuration, service
                        )
                    finally:
                        shutdown.set()
                        connector.cancel()
                        monitor.cancel()
                        await asyncio.wait_for(listener, timeout=5)
            finally:
                await asyncio.shield(_stop_guardian(guardian))


async def _run(settings: FixtureSettings) -> None:
    try:
        async with isolated_fixture(settings) as fixture:
            print(
                json.dumps(
                    {
                        "schema": "operator-fixture-ready.v1",
                        "synthetic": True,
                        "pairingFile": str(fixture.directory / "pairing.txt"),
                        "pairingQr": str(fixture.directory / "pairing.png"),
                        "relayPort": fixture.relay_port,
                        "lifetimeSeconds": settings.lifetime_seconds,
                        "capacityQualified": False,
                    }
                ),
                flush=True,
            )
            await asyncio.Event().wait()
    except FixtureLeaseExpiredError:
        print(
            '{"schema":"operator-fixture-stopped.v1","reason":"lease-expired"}',
            flush=True,
        )


def main(arguments: Sequence[str] | None = None) -> None:
    """Run a local, expiring fixture pinned to an explicit relay binary digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-binary", type=Path, required=True)
    parser.add_argument("--relay-sha256", required=True)
    parser.add_argument("--lifetime-seconds", type=int, default=600)
    options = vars(parser.parse_args(arguments))
    settings = FixtureSettings.model_validate(options)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
