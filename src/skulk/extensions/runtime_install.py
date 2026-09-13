"""Durable offline staging owned by Skulk, independent of plugin runtime health."""

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import sqlite3
import stat
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Literal, cast, final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from skulk.extensions.runtime_artifacts import (
    Digest,
    QualifiedHost,
    RuntimeTrust,
    VerifiedRuntime,
    canonical_json,
    measure_host,
    verified_artifacts,
    verify_runtime,
)
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_integrity import seal_runtime, verify_installed_runtime

_INVENTORY = """import importlib.metadata,json,re
def name(d): return re.sub(r"[-_.]+", "-", d.metadata["Name"].lower())
print(json.dumps({name(d):d.version for d in importlib.metadata.distributions()
if name(d) != 'pip'},sort_keys=True))
"""

_OPERATION_ROW: TypeAdapter[tuple[str, str] | None] = TypeAdapter(
    tuple[str, str] | None
)
_TRUST_ROW: TypeAdapter[tuple[int, str] | None] = TypeAdapter(tuple[int, str] | None)
_DIGEST_ROW: TypeAdapter[tuple[str] | None] = TypeAdapter(tuple[str] | None)
_RUNTIME_DIGEST: TypeAdapter[str] = TypeAdapter(Digest)
_OWNERSHIP_TIMEOUT = 30.0


class RuntimeOperation(BaseModel):
    """Payload-safe durable progress for one offline staging operation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$",
        description="Stable operation ID; reconnect reads this operation.",
    )
    runtime_digest: Digest = Field(description="Exact selected signed runtime.")
    state: Literal["staging", "staged", "recovery_required"] = Field(
        description="Durable progress; interrupted work is never replayed implicitly."
    )
    error_code: Literal["installation_interrupted", "installation_failed"] | None = (
        Field(
            default=None,
            description="Sanitized failure class; raw output stays host-local.",
        )
    )


async def finish_runtime_work[Value](task: asyncio.Task[Value]) -> Value:
    """Finish owned asynchronous work before propagating caller cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _execute(
    arguments: tuple[str, ...], directory: Path, lock: RuntimeLock, timeout: float
) -> bytes:
    async def owned() -> bytes:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lock.descriptor,),
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        output = bytearray()
        try:
            assert process.stdout is not None
            async with asyncio.timeout(timeout):
                while block := await process.stdout.read(16384):
                    output.extend(block)
                    if len(output) > 262144:
                        raise ValueError("runtime installer output exceeds bound")
                await process.wait()
            if process.returncode != 0:
                raise ValueError("runtime installer failed")
            return bytes(output)
        except (OSError, ValueError, TimeoutError):
            write_private(directory / "installer-evidence.log", bytes(output[:262144]))
            raise
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await finish_runtime_work(asyncio.create_task(process.wait()))

    return await finish_runtime_work(asyncio.create_task(owned()))


@final
class RuntimeInstaller:
    """Verify and stage complete runtimes without importing their SDKs.

    Each generation is installed at its final path to preserve virtual-environment
    shebangs. Only a fsynced completion record publishes it as staged. Logical
    plugin state, active selection and independently supervised cleanup are not
    modified by staging. Trust is read from protected local owner provisioning.
    """

    def __init__(self, root: Path) -> None:
        """Select stable nonroot service storage and initialize a protected journal."""
        if os.geteuid() == 0:
            raise ValueError("runtime installer must run without root authority")
        private_directory(root)
        self.root = root.resolve()
        self.installer = self.root / "installer"
        private_directory(self.installer)
        self.database = self.installer / "operations.sqlite3"
        descriptor = os.open(
            self.database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("runtime journal must be owner-only")
        finally:
            os.close(descriptor)
        with self._connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS operations "
                "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, record TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS trust_floor "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, digest TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS releases "
                "(bundle TEXT NOT NULL, sequence INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(bundle,sequence));"
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _save(self, operation: RuntimeOperation) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                (
                    operation.operation_id,
                    operation.runtime_digest,
                    operation.model_dump_json(),
                ),
            )

    def operation(self, operation_id: str) -> RuntimeOperation:
        """Read retained operation progress without starting or replaying work."""
        with self._connect() as connection:
            row = _OPERATION_ROW.validate_python(
                cast(
                    object,
                    connection.execute(
                        "SELECT digest,record FROM operations WHERE id=?",
                        (operation_id,),
                    ).fetchone(),
                ),
                strict=True,
            )
        if row is None:
            raise LookupError("runtime operation not found")
        operation = RuntimeOperation.model_validate_json(row[1])
        if operation.operation_id != operation_id or operation.runtime_digest != row[0]:
            raise ValueError("runtime operation journal is inconsistent")
        return operation

    def _verify(self, metadata: bytes, host: QualifiedHost) -> VerifiedRuntime:
        trust = RuntimeTrust.model_validate_json(
            read_private(self.root / "publisher-trust.json")
        )
        trust_digest = hashlib.sha256(
            canonical_json(trust.model_dump(mode="json"))
        ).hexdigest()
        with self._connect() as connection:
            prior = _TRUST_ROW.validate_python(
                cast(
                    object,
                    connection.execute(
                        "SELECT revision,digest FROM trust_floor WHERE singleton=1"
                    ).fetchone(),
                ),
                strict=True,
            )
            if prior is not None and (
                trust.revision < prior[0]
                or (trust.revision == prior[0] and trust_digest != prior[1])
            ):
                raise ValueError("runtime trust rollback or equivocation refused")
            # Trust is local owner authority, independent of release validity.
            # Even a refused artifact must not let an older trust view return.
            connection.execute(
                "INSERT INTO trust_floor VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,digest=excluded.digest",
                (trust.revision, trust_digest),
            )
        runtime = verify_runtime(metadata, trust, host, now=int(time.time()))
        with self._connect() as connection:
            release = runtime.claims.release
            previous = _DIGEST_ROW.validate_python(
                cast(
                    object,
                    connection.execute(
                        "SELECT digest FROM releases WHERE bundle=? AND sequence=?",
                        (release.manifest.bundle_id, release.sequence),
                    ).fetchone(),
                ),
                strict=True,
            )
            if previous is not None and previous[0] != runtime.digest:
                raise ValueError("runtime release sequence equivocation refused")
            connection.execute(
                "INSERT OR IGNORE INTO releases VALUES (?,?,?)",
                (release.manifest.bundle_id, release.sequence, runtime.digest),
            )
        return runtime

    @contextlib.asynccontextmanager
    async def locked_generation(
        self,
        runtime_digest: str,
        *,
        inherit_on_exec: bool = False,
        wait_for_ownership: bool = False,
    ) -> AsyncIterator[tuple[VerifiedRuntime, QualifiedHost]]:
        """Hold installation ownership around a fully verified staged generation.

        Verify trust, current host, cached artifacts and installed files without
        executing plugin code. Activation callers retain this fence through their
        local state transition. Cancellation waits for file verification to finish.
        Explicit local setup may inherit the fence across exec until its terminal
        process exits; ordinary owners and verification callers do not inherit it.
        Local entrypoints may wait up to 30 seconds for ownership before any
        verification or execution; no command is retried after acquiring the lock.
        """
        digest = _RUNTIME_DIGEST.validate_python(runtime_digest, strict=True)
        lock = await self._ownership_lock(wait_for_ownership)
        try:
            generation = self.root / "generations" / digest

            async def inspect() -> tuple[VerifiedRuntime, QualifiedHost]:
                if not generation.is_dir():
                    raise FileNotFoundError("staged generation is unavailable")
                private_directory(self.root / "generations")
                private_directory(generation)
                host = await asyncio.to_thread(measure_host)
                runtime = self._verify(read_private(generation / "staged.json"), host)
                if runtime.digest != digest:
                    raise ValueError("staged runtime identity differs")
                await asyncio.to_thread(
                    verified_artifacts, runtime, generation / "artifacts"
                )
                await asyncio.to_thread(verify_installed_runtime, generation, digest)
                current_host = await asyncio.to_thread(measure_host)
                self._verify(runtime.metadata, current_host)
                return runtime, current_host

            verified = await finish_runtime_work(asyncio.create_task(inspect()))
            if inherit_on_exec:
                os.set_inheritable(lock.descriptor, True)
            yield verified
        finally:
            lock.close()

    async def inspect_metadata(self, metadata: bytes) -> VerifiedRuntime:
        """Verify signed metadata and trust floors without downloading or executing code."""
        lock = RuntimeLock(self.installer)
        try:
            host = await asyncio.to_thread(measure_host)
            return self._verify(metadata, host)
        finally:
            lock.close()

    async def _ownership_lock(self, wait_for_ownership: bool) -> RuntimeLock:
        # Supervisor verification briefly shares this lock. Wait only before
        # staging or local entrypoint execution has any effects. Retrying either
        # operation itself could replay partially completed work.
        async with asyncio.timeout(_OWNERSHIP_TIMEOUT):
            while True:
                try:
                    return RuntimeLock(self.installer)
                except BlockingIOError:
                    if not wait_for_ownership:
                        raise
                    await asyncio.sleep(0.1)

    async def stage(
        self,
        metadata: bytes,
        artifacts: Path,
        *,
        operation_id: str | None = None,
        recover: bool = False,
        wait_for_ownership: bool = False,
    ) -> RuntimeOperation:
        """Stage one verified artifact set offline with a reconnectable operation ID.

        A reused ID must name the same digest. Failed or interrupted operations
        return recovery_required and cannot spawn another installer implicitly.
        Callers must persist the returned ID; a new browser connection reads
        operation status instead of resubmitting installation.
        Explicit recovery preserves incomplete generations as evidence before
        rebuilding the same signed bytes. Selected or completed generations are
        never moved or resealed by recovery.
        Managed downloads may set wait_for_ownership to wait up to 30 seconds
        for a competing local lock before any staging effects. Verification
        runs after ownership is acquired; cancellation while waiting is safe.
        """
        if recover and operation_id is None:
            raise ValueError("recovery requires the original operation ID")
        lock = await self._ownership_lock(wait_for_ownership)
        operation: RuntimeOperation | None = None
        record_started = False
        try:
            host = await asyncio.to_thread(measure_host)
            runtime = self._verify(metadata, host)
            operation = RuntimeOperation(
                operation_id=operation_id or uuid4().hex,
                runtime_digest=runtime.digest,
                state="staging",
            )
            try:
                prior = self.operation(operation.operation_id)
            except LookupError:
                prior = None
            if prior is not None:
                if prior.runtime_digest != runtime.digest:
                    raise ValueError("operation identity differs")
                if prior.state != "staged" and not recover:
                    interrupted = prior.model_copy(
                        update={
                            "state": "recovery_required",
                            "error_code": prior.error_code
                            or "installation_interrupted",
                        }
                    )
                    self._save(interrupted)
                    return interrupted
            self._save(operation)
            record_started = True
            private_directory(self.root / "generations")
            generation = self.root / "generations" / runtime.digest
            if generation.exists() and not (generation / "staged.json").exists():
                if recover:
                    self._retain_incomplete(generation, prior or operation)
                else:
                    operation = operation.model_copy(
                        update={
                            "state": "recovery_required",
                            "error_code": "installation_interrupted",
                        }
                    )
                    self._save(operation)
                    return operation

            staged_operation = operation.model_copy(update={"state": "staged"})

            async def prepare() -> RuntimeOperation:
                self._verify(metadata, host)
                if generation.exists():
                    private_directory(generation)
                    if read_private(generation / "staged.json") != runtime.metadata:
                        raise ValueError("runtime completion record differs")
                    await asyncio.to_thread(
                        verified_artifacts, runtime, generation / "artifacts"
                    )
                    await asyncio.to_thread(
                        verify_installed_runtime, generation, runtime.digest
                    )
                else:
                    supplied = await asyncio.to_thread(
                        verified_artifacts, runtime, artifacts
                    )
                    private_directory(self.root / "generations")
                    private_directory(generation)
                    artifact_directory = generation / "artifacts"
                    private_directory(artifact_directory)
                    for name, content in supplied.items():
                        write_private(artifact_directory / name, content)
                    write_private(
                        generation / "requirements.txt",
                        "".join(
                            f"./artifacts/{wheel.filename} --hash=sha256:{wheel.sha256}\n"
                            for wheel in runtime.claims.wheels
                        ).encode(),
                    )
                    await _execute(
                        (
                            str(Path(sys.executable).resolve()),
                            "-I",
                            "-m",
                            "venv",
                            "--symlinks",
                            str(generation / "runtime"),
                        ),
                        generation,
                        lock,
                        120,
                    )
                    python = str(generation / "runtime" / "bin" / "python")
                    await _execute(
                        (
                            python,
                            "-I",
                            "-B",
                            "-m",
                            "pip",
                            "--isolated",
                            "--disable-pip-version-check",
                            "--no-cache-dir",
                            "install",
                            "--no-index",
                            "--no-deps",
                            "--require-hashes",
                            "--only-binary=:all:",
                            "--no-compile",
                            "-r",
                            "requirements.txt",
                        ),
                        generation,
                        lock,
                        120,
                    )
                python = str(generation / "runtime" / "bin" / "python")
                await _execute(
                    (python, "-I", "-B", "-m", "pip", "--isolated", "check"),
                    generation,
                    lock,
                    30,
                )
                inventory = await _execute(
                    (python, "-I", "-B", "-c", _INVENTORY), generation, lock, 30
                )
                if json.loads(inventory) != runtime.inventory:
                    raise ValueError("installed runtime inventory differs")
                self._verify(metadata, await asyncio.to_thread(measure_host))
                if not (generation / "staged.json").exists():
                    await asyncio.to_thread(seal_runtime, generation, runtime.digest)
                write_private(generation / "staged.json", runtime.metadata)
                # Completion belongs to the owned work, not the waiting browser
                # or terminal. A cancelled waiter must still see staged on reconnect.
                self._save(staged_operation)
                return staged_operation

            return await finish_runtime_work(asyncio.create_task(prepare()))
        except (OSError, ValueError, TimeoutError, asyncio.CancelledError) as error:
            if (
                operation is not None
                and record_started
                and not (
                    isinstance(error, asyncio.CancelledError)
                    and self.operation(operation.operation_id).state == "staged"
                )
            ):
                self._save(
                    operation.model_copy(
                        update={
                            "state": "recovery_required",
                            "error_code": "installation_failed",
                        }
                    )
                )
            raise
        finally:
            lock.close()

    def _retain_incomplete(self, generation: Path, operation: RuntimeOperation) -> None:
        # The installer fence excludes surviving pip/venv processes. A selected
        # runtime or pending activation must never be repaired underneath an owner.
        for path in (
            self.root / "runtime-selection.json",
            self.root / "lifecycle-operations" / "pending.json",
            self.installer / "selections" / "pending.json",
        ):
            try:
                record = TypeAdapter(dict[str, JsonValue]).validate_json(
                    read_private(path)
                )
            except FileNotFoundError:
                continue
            selection = record.get("selection", record)
            if not isinstance(selection, dict) or not isinstance(
                selection.get("runtime_digest"), str
            ):
                raise ValueError("selection state requires recovery first")
            if selection["runtime_digest"] == operation.runtime_digest:
                raise ValueError("selected runtime cannot be rebuilt in place")
        private_directory(generation)
        history = self.installer / "recovery" / operation.operation_id / uuid4().hex
        private_directory(history)
        write_private(history / "operation.json", operation.model_dump_json().encode())
        os.rename(generation, history / "generation")
        for directory in (generation.parent, history):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
