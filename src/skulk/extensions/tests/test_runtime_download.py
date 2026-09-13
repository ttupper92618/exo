"""Signed release downloads, durable acceptance and refusal before execution."""

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from skulk.extensions.runtime_artifacts import RuntimeTrust
from skulk.extensions.runtime_download import (
    InstallRequest,
    ReleaseSource,
    RuntimeDownloads,
    SourceUpdate,
)
from skulk.extensions.runtime_files import RuntimeLock, read_private, write_private
from skulk.extensions.runtime_selection import RuntimeSelector
from skulk.extensions.tests.test_runtime_install import artifacts


def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RuntimeDownloads, bytes, Path, list[str]]:
    """Supply an authenticated mock HTTPS origin and real signed offline artifacts."""
    source = tmp_path / "source"
    metadata, trust, host = artifacts(source)
    root = tmp_path / "installation"
    write_private(root / "publisher-trust.json", trust.model_dump_json().encode())
    write_private(
        root / "release-source.json",
        ReleaseSource(
            revision=1,
            base_url="https://releases.example.test/private/",
            metadata_filename="runtime.json",
            credential_reference="a" * 32,
        )
        .model_dump_json()
        .encode(),
    )
    write_private(root / "feed-credentials" / ("a" * 32), b"fixture-private-token")
    monkeypatch.setattr("skulk.extensions.runtime_install.measure_host", lambda: host)
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "releases.example.test"
        assert request.headers["Authorization"] == "Bearer fixture-private-token"
        calls.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(
            200,
            content=metadata if name == "runtime.json" else read_private(source / name),
        )

    return (
        RuntimeDownloads(root, transport=httpx.MockTransport(respond)),
        metadata,
        source,
        calls,
    )


async def test_review_and_owned_staging_survive_request_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review precedes artifacts; accepted staging installs exact bytes and never enables."""
    downloads, _, _, calls = prepared(tmp_path, monkeypatch)
    before = await downloads.inspect()
    assert calls == ["/private/runtime.json"]
    assert before.version == "1.0.0" and before.permissions == (
        "local synthetic operation",
    )
    assert "fixture-private-token" not in before.model_dump_json()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=before.runtime_digest,
        expected_source_revision=1,
    )
    accepted = await downloads.submit(request)
    assert accepted.state == "accepted"
    assert (await downloads.submit(request)).request == request
    assert downloads.work is not None
    await downloads.work
    staged = downloads.operation(request.operation_id)
    assert staged.state == "staged" and staged.downloaded_bytes == before.artifact_bytes
    assert len(calls) == 3
    assert not (downloads.root / "selected-runtime.json").exists()
    async with downloads.installer.locked_generation(before.runtime_digest) as (
        verified,
        _,
    ):
        assert verified.digest == before.runtime_digest
    await downloads.close()
    restarted = RuntimeDownloads(downloads.root)
    assert restarted.current() == staged
    assert await restarted.submit(request) == staged
    assert restarted.work is None
    await restarted.close()


async def test_staging_waits_for_short_lived_installer_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service verification lock delays accepted staging without requiring replay."""
    downloads, _, _, calls = prepared(tmp_path, monkeypatch)
    review = await downloads.inspect()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=review.runtime_digest,
        expected_source_revision=1,
    )
    await downloads.submit(request)
    lock = RuntimeLock(downloads.installer.installer)
    try:
        async with asyncio.timeout(5):
            while downloads.operation(request.operation_id).state == "accepted":
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        waiting = downloads.operation(request.operation_id)
        assert waiting.state == "staging"
        assert waiting.downloaded_bytes == review.artifact_bytes
        assert downloads.work is not None and not downloads.work.done()
        with pytest.raises(LookupError):
            downloads.installer.operation(request.operation_id)
        assert await downloads.submit(request) == waiting
    finally:
        lock.close()
        assert downloads.work is not None
        await downloads.work
        await downloads.close()
    staged = downloads.operation(request.operation_id)
    assert staged.state == "staged" and staged.attempt == 0
    assert len(calls) == 3
    assert not (downloads.root / "selected-runtime.json").exists()


@pytest.mark.parametrize("failure", ["timeout", "shutdown", "expired"])
async def test_staging_ownership_wait_refuses_without_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Waiting is bounded and cancellable, and trust is checked after acquiring ownership."""
    downloads, _, _, calls = prepared(tmp_path, monkeypatch)
    review = await downloads.inspect()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=review.runtime_digest,
        expected_source_revision=1,
    )
    if failure == "timeout":
        monkeypatch.setattr("skulk.extensions.runtime_install._OWNERSHIP_TIMEOUT", 0.1)
    await downloads.submit(request)
    lock = RuntimeLock(downloads.installer.installer)
    try:
        async with asyncio.timeout(5):
            while downloads.operation(request.operation_id).state == "accepted":
                await asyncio.sleep(0.01)
        assert downloads.operation(request.operation_id).state == "staging"
        assert downloads.work is not None
        if failure == "shutdown":
            await downloads.close()
        elif failure == "expired":
            path = downloads.root / "publisher-trust.json"
            trust = RuntimeTrust.model_validate_json(read_private(path))
            write_private(
                path,
                trust.model_copy(update={"expires_at": 1}).model_dump_json().encode(),
            )
            lock.close()
        async with asyncio.timeout(5):
            await downloads.work
        failed = downloads.operation(request.operation_id)
        assert failed.state == "recovery_required"
        assert failed.error_code == "installation_failed"
        assert failed.downloaded_bytes == review.artifact_bytes
        with pytest.raises(LookupError):
            downloads.installer.operation(request.operation_id)
        assert not (downloads.root / "generations").exists()
        assert not (downloads.root / "selected-runtime.json").exists()
    finally:
        lock.close()
        await downloads.close()
    restarted = RuntimeDownloads(downloads.root)
    try:
        assert await restarted.submit(request) == failed
        assert restarted.work is None and len(calls) == 3
    finally:
        await restarted.close()


@pytest.mark.parametrize("failure", ["redirect", "truncated", "tampered", "oversized"])
async def test_bad_artifacts_never_stage_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Reject signed-byte failures without activating or replaying a retained operation."""
    downloads, _, source, calls = prepared(tmp_path, monkeypatch)
    reviewed = await downloads.inspect()

    def bad(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if failure == "redirect":
            return httpx.Response(
                302, headers={"Location": "https://untrusted.example.test/stolen"}
            )
        original = read_private(source / "bundle.pyz")
        body = (
            original[:-1]
            if failure == "truncated"
            else (original + b"x" if failure == "oversized" else b"x" * len(original))
        )
        return httpx.Response(200, content=body)

    downloads.transport = httpx.MockTransport(bad)
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=reviewed.runtime_digest,
        expected_source_revision=1,
    )
    await downloads.submit(request)
    assert downloads.work is not None
    await downloads.work
    failed = downloads.current()
    assert failed is not None and failed.state == "recovery_required"
    assert failed.error_code == "download_failed"
    assert not (downloads.root / "generations").exists()
    count = len(calls)
    assert await downloads.submit(request) == failed
    assert len(calls) == count == 2
    assert "fixture-private-token" not in failed.model_dump_json()
    await downloads.close()


async def test_missing_credential_and_untrusted_metadata_refuse_before_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No vault fallback, redirect or artifact I/O precedes source/signature validation."""
    downloads, metadata, _, calls = prepared(tmp_path, monkeypatch)
    credential = downloads.root / "feed-credentials" / ("a" * 32)
    credential.unlink()
    with pytest.raises(FileNotFoundError):
        await downloads.inspect()
    assert not calls
    write_private(credential, b"fixture-private-token")
    downloads.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, content=metadata.replace(b'"fixture"', b'"unknown"')
        )
    )
    with pytest.raises(ValueError):
        await downloads.inspect()
    assert not (downloads.directory / "reviews").exists()
    await downloads.close()


async def test_interruption_and_revision_conflict_do_not_restart_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation leaves retained intent and a new manager does not repeat downloads."""
    downloads, _, _, calls = prepared(tmp_path, monkeypatch)
    review = await downloads.inspect()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=review.runtime_digest,
        expected_source_revision=2,
    )
    with pytest.raises(ValueError, match="revision"):
        await downloads.submit(request)
    assert len(calls) == 1
    request = request.model_copy(update={"expected_source_revision": 1})
    started = asyncio.Event()

    async def hang(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled download returned")

    downloads.transport = httpx.MockTransport(hang)
    await downloads.submit(request)
    await started.wait()
    await downloads.close()
    restarted = RuntimeDownloads(downloads.root)
    prior = restarted.current()
    assert prior is not None and prior.state == "recovery_required"
    assert await restarted.submit(request) == prior and restarted.work is None
    with pytest.raises(ValueError, match="identity"):
        await restarted.submit(request.model_copy(update={"runtime_digest": "c" * 64}))
    await restarted.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://releases.example.test/",
        "https://secret@releases.example.test/",
        "https://releases.example.test/?token=secret",
        "https://releases.example.test/#fragment",
        "https://releases.example.test/missing-slash",
    ],
)
def test_source_endpoint_is_unambiguous(url: str) -> None:
    """Sources use one explicit HTTPS directory without URL credentials or redirects."""
    with pytest.raises(ValueError):
        ReleaseSource(revision=1, base_url=url, metadata_filename="runtime.json")


async def test_source_rotation_preserves_old_credentials_and_fences_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source changes cannot silently forward a retained bearer to a new origin."""
    downloads, _, _, _ = prepared(tmp_path, monkeypatch)
    trust = RuntimeTrust.model_validate_json(
        read_private(downloads.root / "publisher-trust.json")
    )
    update = SourceUpdate(
        expected_revision=1,
        base_url="https://different.example.test/",
        metadata_filename="runtime.json",
        trust=trust,
    )
    with pytest.raises(ValueError, match="credential replacement"):
        await downloads.configure(update)
    assert downloads.source().revision == 1
    rotated = await downloads.configure(
        update.model_copy(update={"token": SecretStr("replacement-secret")})
    )
    assert rotated.revision == 2 and rotated.credential_ready
    assert rotated.credential_reference != "a" * 32
    assert "replacement-secret" not in rotated.model_dump_json()
    assert (
        read_private(downloads.root / "feed-credentials" / ("a" * 32))
        == b"fixture-private-token"
    )
    assert rotated.credential_reference is not None
    assert (
        read_private(downloads.root / "feed-credentials" / rotated.credential_reference)
        == b"replacement-secret"
    )
    with pytest.raises(ValueError, match="revision"):
        await downloads.configure(update)
    await downloads.close()


async def test_partial_source_setup_retains_trust_and_never_removes_revocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential-only updates preserve destination and all existing trust decisions."""
    downloads, _, _, _ = prepared(tmp_path, monkeypatch)
    original = downloads.source()
    path = downloads.root / "publisher-trust.json"
    trust = RuntimeTrust.model_validate_json(read_private(path))
    revoked = trust.model_copy(
        update={"revoked_publishers": ("retired",), "revoked_artifacts": ("d" * 64,)}
    )
    write_private(path, revoked.model_dump_json().encode())
    result = await downloads.configure(
        SourceUpdate(expected_revision=1, token=SecretStr("rotated-token"))
    )
    assert result.revision == 2 and result.credential_ready
    assert downloads.source().base_url == original.base_url
    assert downloads.source().metadata_filename == original.metadata_filename
    assert RuntimeTrust.model_validate_json(read_private(path)) == revoked
    await downloads.configure(
        SourceUpdate(
            expected_revision=2,
            trust=trust.model_copy(update={"revision": trust.revision + 1}),
        )
    )
    renewed = RuntimeTrust.model_validate_json(read_private(path))
    assert renewed.revoked_publishers == revoked.revoked_publishers
    assert renewed.revoked_artifacts == revoked.revoked_artifacts
    assert renewed.revision == trust.revision + 1
    (downloads.root / "release-source.json").unlink()
    assert downloads.source_status().trust_revision == renewed.revision
    with pytest.raises(ValueError, match="initial source setup"):
        await downloads.configure(
            SourceUpdate(expected_revision=0, token=SecretStr("not-written"))
        )
    assert not (downloads.root / "release-source.json").exists()
    assert len(tuple((downloads.root / "feed-credentials").iterdir())) == 2
    await downloads.close()


async def test_recovery_uses_rotated_credential_without_changing_original_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit retries preserve failed transfer evidence and exact reviewed bytes."""
    downloads, _, source, _ = prepared(tmp_path, monkeypatch)
    review = await downloads.inspect()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=review.runtime_digest,
        expected_source_revision=1,
    )
    downloads.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"truncated")
    )
    await downloads.submit(request)
    assert downloads.work is not None
    await downloads.work
    failed = downloads.current()
    assert failed is not None and failed.state == "recovery_required"
    await downloads.configure(
        SourceUpdate(
            expected_revision=1,
            token=SecretStr("rotated-token"),
        )
    )
    requests: list[str] = []

    def restored(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer rotated-token"
        requests.append(request.url.path)
        return httpx.Response(
            200, content=read_private(source / request.url.path.rsplit("/", 1)[1])
        )

    downloads.transport = httpx.MockTransport(restored)
    with pytest.raises(ValueError, match="revision"):
        await downloads.recover(request.operation_id, 1)
    assert not requests
    accepted = await downloads.recover(request.operation_id, 2)
    assert accepted.request == request and accepted.review == failed.review
    assert accepted.attempt == 1 and accepted.attempt_source_revision == 2
    assert downloads.work is not None
    await downloads.work
    staged = downloads.current()
    assert staged is not None and staged.state == "staged"
    assert (
        read_private(
            downloads.directory
            / "artifacts"
            / request.operation_id
            / "bundle.pyz.partial"
        )
        == b"truncated"
    )
    assert (downloads.directory / "attempts" / request.operation_id / "0.json").exists()
    assert await downloads.recover(request.operation_id, 2) == staged
    assert len(requests) == 2
    await downloads.close()


async def test_interrupted_offline_stage_is_retained_before_explicit_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving installer fence blocks recovery; released partial state is archived."""
    downloads, _, _, _ = prepared(tmp_path, monkeypatch)
    review = await downloads.inspect()
    request = InstallRequest(
        operation_id="b" * 32,
        runtime_digest=review.runtime_digest,
        expected_source_revision=1,
    )

    async def failed_process(
        arguments: tuple[str, ...], directory: Path, lock: RuntimeLock, timeout: float
    ) -> bytes:
        raise OSError("injected local installation failure")

    with monkeypatch.context() as failing:
        failing.setattr("skulk.extensions.runtime_install._execute", failed_process)
        await downloads.submit(request)
        assert downloads.work is not None
        await downloads.work
    failed = downloads.current()
    assert failed is not None and failed.error_code == "installation_failed"
    generation = downloads.root / "generations" / review.runtime_digest
    assert generation.is_dir() and not (generation / "staged.json").exists()
    lock = RuntimeLock(downloads.installer.installer)
    try:
        with pytest.raises(BlockingIOError):
            await downloads.recover(request.operation_id, 1)
        assert downloads.current() == failed
    finally:
        lock.close()
    await downloads.recover(request.operation_id, 1)
    assert downloads.work is not None
    await downloads.work
    staged = downloads.current()
    assert staged is not None and staged.state == "staged" and staged.request == request
    history = tuple(
        (downloads.installer.installer / "recovery" / request.operation_id).iterdir()
    )
    assert len(history) == 1
    assert (history[0] / "generation" / "artifacts" / "bundle.pyz").is_file()
    assert (history[0] / "operation.json").is_file()
    async with downloads.installer.locked_generation(review.runtime_digest) as (
        verified,
        _,
    ):
        assert verified.digest == review.runtime_digest
    await downloads.close()


@pytest.mark.parametrize("selected", [False, True])
async def test_recovery_never_reseals_completed_or_moves_selected_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    """Explicit recovery cannot bypass immutable completion or active-selection fences."""
    downloads, metadata, source, _ = prepared(tmp_path, monkeypatch)
    staged = await downloads.installer.stage(metadata, source, operation_id="b" * 32)
    generation = downloads.root / "generations" / staged.runtime_digest
    if selected:
        selector = RuntimeSelector(downloads.root)
        await selector.activate(
            staged.runtime_digest, expected_revision=0, operation_id="c" * 32
        )
        (generation / "staged.json").unlink()
    else:
        write_private(generation / "artifacts" / "bundle.pyz", b"tampered")
    with pytest.raises(ValueError):
        await downloads.installer.stage(
            metadata, source, operation_id="b" * 32, recover=True
        )
    assert (
        generation.is_dir()
        and not (downloads.installer.installer / "recovery").exists()
    )
    if selected:
        assert RuntimeSelector(downloads.root).current() is not None
    else:
        assert read_private(generation / "artifacts" / "bundle.pyz") == b"tampered"
    await downloads.close()
