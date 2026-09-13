"""Local fixture validation, including opt-in real signed connector traffic."""

import asyncio
import base64
import hashlib
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID

import hypercorn.asyncio as hypercorn_asyncio
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypercorn.config import Config
from hypercorn.typing import ASGIFramework
from pydantic import TypeAdapter, ValidationError

import bench.operator_workload_fixture as fixture_module
from bench.operator_fixture_app import (
    generated_chat_chunks,
    generated_responses,
    generated_speech_chunks,
)
from bench.operator_fixture_client import request_fixture, request_fixture_bytes
from bench.operator_fixture_observer import FixtureObserver, ObservationEvent
from bench.operator_workload_fixture import (
    FixtureLeaseExpiredError,
    FixtureSettings,
    PublicFixtureIngress,
    copy_verified_fixture_binary,
    fixture_lease,
    isolated_fixture,
    validate_private_fixture_origin,
    validate_public_fixture_origin,
    verify_binary,
)
from skulk.operator.pairing import pairing_signature_message


def test_generated_models_use_canonical_task_vocabulary() -> None:
    """Released clients gate chat and speech using the canonical task values."""
    models = cast(list[dict[str, object]], generated_responses()["/v1/models"]["data"])
    assert models[0]["tasks"] == ["TextGeneration"]
    assert models[1]["tasks"] == ["TextToSpeech"]


async def test_readiness_allows_explicit_extended_startup_but_rejects_dead_guardian() -> None:
    """Public setup retries readiness without accepting an exited relay owner."""
    requests = 0

    async def ready(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.Response(status=204 if requests >= 3 else 503)

    app = web.Application()
    app.router.add_get("/readyz", ready)
    runner = web.AppRunner(app)
    await runner.setup()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = cast(tuple[str, int], listener.getsockname())[1]
    site = web.SockSite(runner, listener)
    await site.start()
    guardian = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        with pytest.raises(RuntimeError, match="did not become ready"):
            await fixture_module.wait_fixture_relay(port, guardian, attempts=2)
        await fixture_module.wait_fixture_relay(port, guardian, attempts=3)
        assert requests == 3
        guardian.terminate()
        await guardian.wait()
        with pytest.raises(RuntimeError, match="exited before readiness"):
            await fixture_module.wait_fixture_relay(port, guardian, attempts=2400)
        assert requests == 3
    finally:
        if guardian.returncode is None:
            guardian.kill()
            await guardian.wait()
        await runner.cleanup()


class _ChatDelta(TypedDict, total=False):
    content: str


class _ChatChoice(TypedDict):
    index: int
    delta: _ChatDelta
    finish_reason: str | None


class _ChatChunk(TypedDict):
    id: str
    object: str
    model: str
    choices: list[_ChatChoice]


async def test_generated_chat_contains_required_stream_identity() -> None:
    """All SSE chunks, including the terminal chunk, satisfy client identity gates."""
    chunks = [chunk async for chunk in generated_chat_chunks()]
    assert chunks[-1] == b"data: [DONE]\n\n"
    content: list[str] = []
    finish_reason: str | None = None
    adapter = TypeAdapter(_ChatChunk)
    for chunk in chunks[:-1]:
        payload = adapter.validate_json(
            chunk.removeprefix(b"data: ").strip(), strict=True
        )
        assert payload["id"] == "fixture-generated-chat"
        assert payload["object"] == "chat.completion.chunk"
        assert payload["model"] == "fixture/generated-chat"
        assert payload["choices"][0]["index"] == 0
        content.append(payload["choices"][0]["delta"].get("content", ""))
        finish_reason = payload["choices"][0]["finish_reason"]
    assert "".join(content) == "Synthetic fixture response. No model was run."
    assert finish_reason == "stop"


async def test_generated_speech_is_bounded_frame_aligned_silence() -> None:
    """Synthetic speech is three seconds of mono PCM16 at 24 kHz, not inference."""
    chunks = [chunk async for chunk in generated_speech_chunks()]
    assert len(chunks) == 30
    assert all(chunk == bytes(4800) for chunk in chunks)
    assert sum(map(len, chunks)) == 24000 * 2 * 3


def test_private_fixture_origin_rejects_public_and_ambiguous_targets() -> None:
    """The opt-in pilot cannot advertise public, cleartext, or credential URLs."""
    assert validate_private_fixture_origin("wss://fixture.tail-test.ts.net:8443")
    for origin in (
        "ws://fixture.tail-test.ts.net:8443",
        "wss://example.com:8443",
        "wss://fixture.tail-test.ts.net",
        "wss://user@fixture.tail-test.ts.net:8443",
        "wss://fixture.tail-test.ts.net:8443/",
        "wss://fixture.tail-test.ts.net:8443?x=y",
        "wss://fixture.tail-test.ts.net:8443#fragment",
        "wss://fixture.tail-test.ts.net:0",
        "wss://fixture.tail-test.ts.net:65536",
        "wss://fixture.tail-test.ts.net.evil:8443",
        "wss://fixture.tail-test.ts.net:8443\n",
    ):
        with pytest.raises(ValueError):
            validate_private_fixture_origin(origin)


@pytest.mark.parametrize("valid_origin", [False, True])
async def test_ingress_is_closed_on_startup_failure(
    tmp_path: Path, valid_origin: bool
) -> None:
    """Origin rejection and partial provisioning both exit their owned ingress."""
    from collections.abc import AsyncIterator

    binary = tmp_path / "relay"
    binary.write_bytes(b"#!/bin/sh\nexit 1\n")
    binary.chmod(0o700)
    settings = FixtureSettings(
        relay_binary=binary,
        relay_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    closed = False

    @asynccontextmanager
    async def ingress(port: int) -> AsyncIterator[str]:
        nonlocal closed
        assert 0 < port < 65536
        try:
            yield (
                "wss://fixture.tail-test.ts.net:8443"
                if valid_origin
                else "wss://production.example:443"
            )
        finally:
            closed = True

    expected = RuntimeError if valid_origin else ValueError
    message = "provisioning failed" if valid_origin else "tailnet WSS"
    with pytest.raises(expected, match=message):
        async with isolated_fixture(settings, private_ingress=ingress):
            pytest.fail("invalid ingress must not reach fixture readiness")
    assert closed


def test_public_fixture_origin_is_bound_to_its_run() -> None:
    """Public opt-in cannot reuse an arbitrary relay or ambiguous URL."""
    run_id = "a" * 32
    origin = f"wss://rehearsal-{run_id}.example.com"
    assert validate_public_fixture_origin(origin, run_id) == origin
    for invalid in (
        origin.replace("wss://", "ws://"),
        origin + "/",
        origin + ":443",
        origin + "?query",
        origin + "#fragment",
        origin + "\n",
        origin.replace("example.com", "test.ts.net"),
        origin.replace("example.com", "-bad.example.com"),
        origin.replace(run_id, "b" * 32),
        "wss://production.example.com",
        origin.replace("wss://", "wss://user@"),
    ):
        with pytest.raises(ValueError):
            validate_public_fixture_origin(invalid, run_id)
    for invalid_id in ("", "a" * 31, "A" * 32, "g" * 32):
        with pytest.raises(ValueError, match="run identifier"):
            validate_public_fixture_origin(origin, invalid_id)


@pytest.mark.parametrize("changed_origin", [False, True])
async def test_public_ingress_closes_after_partial_startup(
    tmp_path: Path, changed_origin: bool
) -> None:
    """A changed advertised origin and failed provisioning both reap ingress."""
    from collections.abc import AsyncIterator

    binary = tmp_path / "relay"
    binary.write_bytes(b"#!/bin/sh\nexit 1\n")
    binary.chmod(0o700)
    settings = FixtureSettings(
        relay_binary=binary,
        relay_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    origin = f"wss://rehearsal-{'a' * 32}.example.com"
    closed = False

    @asynccontextmanager
    async def ingress(port: int) -> AsyncIterator[str]:
        nonlocal closed
        assert 0 < port < 65536
        try:
            yield "wss://production.example.com" if changed_origin else origin
        finally:
            closed = True

    expected = ValueError if changed_origin else RuntimeError
    message = "origin changed" if changed_origin else "provisioning failed"
    with pytest.raises(expected, match=message):
        async with isolated_fixture(
            settings,
            public_ingress=PublicFixtureIngress("a" * 32, origin, ingress),
        ):
            pytest.fail("partial startup must not become readiness")
    assert closed


@pytest.mark.parametrize("both_ingresses", [False, True])
async def test_public_ingress_preflight_precedes_effects(
    tmp_path: Path, both_ingresses: bool
) -> None:
    """Conflicting ingress and excessive leases fail before opening resources."""
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def ingress(port: int) -> AsyncIterator[str]:
        pytest.fail("invalid configuration must not open ingress")
        yield str(port)

    settings = FixtureSettings(
        relay_binary=tmp_path / "nonexistent",
        relay_sha256="0" * 64,
        lifetime_seconds=3600 if both_ingresses else 3601,
    )
    with pytest.raises(ValueError, match="one ingress and a one-hour lease"):
        async with isolated_fixture(
            settings,
            private_ingress=ingress if both_ingresses else None,
            public_ingress=PublicFixtureIngress(
                "a" * 32, f"wss://rehearsal-{'a' * 32}.example.com", ingress
            ),
        ):
            pytest.fail("invalid configuration must not start")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


async def test_only_the_session_deadline_is_normal_expiry() -> None:
    """Startup timeouts and teardown timeouts cannot become a successful expiry."""
    with pytest.raises(FixtureLeaseExpiredError):
        async with fixture_lease(0.01):
            await asyncio.sleep(1)
    with pytest.raises(TimeoutError, match="startup failed"):
        async with fixture_lease(1):
            raise TimeoutError("startup failed")
    with pytest.raises(TimeoutError, match="cleanup failed"):
        async with fixture_lease(0.01):
            try:
                await asyncio.sleep(1)
            finally:
                raise TimeoutError("cleanup failed")


async def test_external_cancellation_is_not_reported_as_expiry() -> None:
    """Caller cancellation retains its identity instead of forging a deadline."""

    async def wait() -> None:
        async with fixture_lease(10):
            await asyncio.sleep(10)

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_fixture_refuses_unpinned_binary_and_unbounded_lifetime(tmp_path: Path) -> None:
    """Digest validation precedes provisioning; no hosted target parameter exists."""
    binary = tmp_path / "relay"
    binary.write_bytes(b"not a relay")
    binary.chmod(0o700)
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_binary(FixtureSettings(relay_binary=binary, relay_sha256="0" * 64))
    for duration in (0, 59, 7201):
        with pytest.raises(ValidationError):
            FixtureSettings(
                relay_binary=binary, relay_sha256="0" * 64, lifetime_seconds=duration
            )
    with pytest.raises(ValidationError):
        FixtureSettings.model_validate(
            {
                "relay_binary": binary,
                "relay_sha256": "0" * 64,
                "relay_url": "wss://example.invalid",
            }
        )


def test_fixture_executes_copied_verified_bytes_after_source_replacement(
    tmp_path: Path,
) -> None:
    """A build at the selected pathname cannot change this fixture's executable."""
    source = tmp_path / "source"
    source.write_bytes(b"selected-binary")
    source.chmod(0o700)
    settings = FixtureSettings(
        relay_binary=source,
        relay_sha256=hashlib.sha256(b"selected-binary").hexdigest(),
    )
    destination = copy_verified_fixture_binary(settings, tmp_path / "owned-copy")
    source.write_bytes(b"new-build")
    assert destination.read_bytes() == b"selected-binary"
    assert destination.stat().st_mode & 0o777 == 0o500
    with pytest.raises(ValueError, match="digest mismatch"):
        copy_verified_fixture_binary(settings, tmp_path / "rejected-copy")
    assert (tmp_path / "rejected-copy").stat().st_mode & 0o111 == 0


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize(
    "close_after_response_body,abort_after_response_body",
    [(False, False), (True, False), (True, True)],
)
async def test_real_fixture_pairs_reads_and_cleans_up(
    observed: bool,
    close_after_response_body: bool,
    abort_after_response_body: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real Rust relay and Python auth, not released-app capacity.

    Uses a no-retry, loopback-only TLS-over-WebSocket protocol test adapter.
    """
    configured = os.environ.get("SKULK_PAIRED_RELAY_BINARY")
    if configured is None:
        pytest.skip("requires an explicitly selected local relay binary")
    binary = Path(configured).resolve(strict=True)
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    settings = FixtureSettings(
        relay_binary=binary, relay_sha256=digest, lifetime_seconds=60
    )
    events: list[ObservationEvent] = []
    observer = FixtureObserver(events.append) if observed else None
    listener_started = False
    original_serve = cast(Callable[..., Awaitable[None]], hypercorn_asyncio.serve)

    async def delayed_serve(
        app: ASGIFramework,
        config: Config,
        *,
        shutdown_trigger: Callable[[], Awaitable[object]],
    ) -> None:
        nonlocal listener_started
        await asyncio.sleep(0.5)
        listener_started = True
        await original_serve(app, config, shutdown_trigger=shutdown_trigger)

    monkeypatch.setattr(fixture_module, "_serve", delayed_serve)
    async with isolated_fixture(settings, observer=observer) as fixture:
        assert listener_started
        if observer is not None:
            observer.begin("cold-launch")
        directory = fixture.directory
        relay_port = fixture.relay_port
        gateway_port = fixture.configuration.operator_api_port
        assert (directory / "pairing.txt").stat().st_mode & 0o777 == 0o600
        package = fixture.pairing_service.create_session()
        remote = package.remote_access
        assert remote is not None
        assert (await request_fixture(remote, "GET", "/state")).status == 401
        key = Ed25519PrivateKey.generate()
        challenge = await request_fixture(
            remote,
            "POST",
            "/v1/auth/pairing-sessions/challenge",
            body={
                "nonce": package.nonce,
                "deviceName": "Synthetic protocol test",
                "devicePublicKey": _base64url(
                    key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                ),
            },
        )
        assert challenge.status == 200
        proof = key.sign(
            pairing_signature_message(
                cluster_id=UUID(str(package.cluster_id)),
                nonce=package.nonce,
                challenge=str(challenge.body["challenge"]),
            )
        )
        exchange = await request_fixture(
            remote,
            "POST",
            "/v1/auth/pairing-sessions/exchange",
            body={
                "nonce": package.nonce,
                "signature": _base64url(proof),
            },
        )
        assert exchange.status == 200
        token = str(exchange.body["accessToken"])
        for path, expected in generated_responses().items():
            response = await request_fixture(
                remote,
                "GET",
                path,
                bearer=token,
                close_after_response_body=close_after_response_body,
            )
            assert response.status == 200
            assert response.body == expected
        chat_response = await request_fixture_bytes(
            remote,
            "POST",
            "/v1/chat/completions",
            bearer=token,
            body={"model": "fixture/generated-chat", "messages": [], "stream": True},
            close_after_response_body=close_after_response_body,
            abort_after_response_body=abort_after_response_body,
        )
        assert chat_response.status == 200
        assert chat_response.body == b"".join(
            [chunk async for chunk in generated_chat_chunks()]
        )
        speech_response = await request_fixture_bytes(
            remote,
            "POST",
            "/v1/audio/speech",
            bearer=token,
            body={
                "model": "fixture/generated-speech",
                "input": "synthetic",
                "stream": True,
            },
            close_after_response_body=close_after_response_body,
            abort_after_response_body=abort_after_response_body,
        )
        assert speech_response.status == 200
        assert speech_response.body == bytes(144000)
        # The fixture cannot forward an arbitrary cluster mutation.
        assert (
            await request_fixture(
                remote, "POST", "/place_instance", body={}, bearer=token
            )
        ).status == 404
        if observer is not None:
            async with asyncio.timeout(3):
                while not observer.idle():
                    await asyncio.sleep(0.01)
            observer.end()
            assert (
                sum(event["type"] == "connection-open" for event in events)
                == len(generated_responses()) + 6
            )
            assert sum(event.get("outcome") == "failed" for event in events) == 2
            assert (
                sum(event.get("outcome") == "completed" for event in events)
                == len(generated_responses()) + 4
            )
    assert not directory.exists()
    # Two independent connection attempts after the context exits verify both
    # listeners are gone; this is local lifecycle evidence, not cloud inventory.
    for _ in range(2):
        for port in (relay_port, gateway_port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                assert connection.connect_ex(("127.0.0.1", port)) != 0
        await asyncio.sleep(0.05)
