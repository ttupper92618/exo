"""Fixed nonroot launcher for a verified managed runtime, separate from inference."""

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from pathlib import Path
from typing import Literal, cast, final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from skulk.extensions.runtime_artifacts import Digest
from skulk.extensions.runtime_files import RuntimeLock, write_private
from skulk.extensions.runtime_install import finish_runtime_work
from skulk.extensions.runtime_selection import RuntimeSelection, RuntimeSelector

_BOOTSTRAP = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('__owner__',run_name='__main__')"
_CHECK_SECONDS = 30.0
_SHUTDOWN_SECONDS = 30.0


class RuntimeServiceStatus(BaseModel):
    """Protected process observation; running does not imply capability readiness."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    service_instance: str = Field(description="Unique launcher lifetime identifier.")
    observed_at: float = Field(
        description="Unix observation time; old records are stale."
    )
    state: Literal["verifying", "running", "stopping", "stopped", "failed"] = Field(
        description="Observed local owner process state, independent of desired selection."
    )
    selected_digest: Digest | None = Field(description="Desired selected generation.")
    active_digest: Digest | None = Field(
        description="Generation owned by this launcher."
    )
    error_code: (
        Literal[
            "verification_failed", "owner_exited", "ownership_busy", "service_failed"
        ]
        | None
    ) = Field(default=None, description="Sanitized actionable failure class.")
    output_bytes: int = Field(
        ge=0, description="Discarded owner output volume; no payloads."
    )


@final
class RuntimeService:
    """Own one fixed plugin process until shutdown or admission verification fails.

    This launcher has no provider SDK, spending policy or witness lifecycle. A
    service manager may restart it; each lifetime revalidates local selection and
    artifacts. An owner failure ends this lifetime without replaying API work.
    """

    def __init__(self, root: Path) -> None:
        """Use protected local installation storage without starting plugin code."""
        self.selector = RuntimeSelector(root)
        self.root = self.selector.root
        self.instance = uuid4().hex
        self.process: asyncio.subprocess.Process | None = None
        self.selection: RuntimeSelection | None = None
        self.writer: int | None = None
        self.output_bytes = 0
        self.drains: list[asyncio.Task[None]] = []

    def _status(
        self,
        state: Literal["verifying", "running", "stopping", "stopped", "failed"],
        error: Literal[
            "verification_failed", "owner_exited", "ownership_busy", "service_failed"
        ]
        | None = None,
    ) -> None:
        active = self.process is not None and self.process.returncode is None
        value = RuntimeServiceStatus(
            service_instance=self.instance,
            observed_at=time.time(),
            state=state,
            selected_digest=self.selection.runtime_digest if self.selection else None,
            active_digest=self.selection.runtime_digest
            if self.selection and active
            else None,
            error_code=error,
            output_bytes=self.output_bytes,
        )
        write_private(
            self.root / "service-status.json", value.model_dump_json().encode()
        )

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        while block := await stream.read(16384):
            self.output_bytes += len(block)

    async def _spawn(self, lock: RuntimeLock) -> None:
        assert self.selection is not None
        generation = self.root / "generations" / self.selection.runtime_digest
        reader, self.writer = os.pipe()
        try:
            # The owner inherits the service fence, but only the read end of its
            # lifetime pipe. Abrupt launcher death therefore asks it to shut down
            # before another launcher can acquire this service's ownership.
            self.process = await asyncio.create_subprocess_exec(
                str(generation / "runtime/bin/python"),
                "-I",
                "-B",
                "-c",
                _BOOTSTRAP,
                str(generation / "artifacts/bundle.pyz"),
                "--root",
                str(self.root),
                "--lifetime-fd",
                str(reader),
                pass_fds=(reader, lock.descriptor),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            assert self.process.stdout is not None
            self.drains.append(asyncio.create_task(self._drain(self.process.stdout)))
        finally:
            os.close(reader)

    async def _close(self) -> None:
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
        process = self.process
        if process is not None:
            # Pipe EOF is the managed owner's graceful shutdown request. Sending
            # SIGTERM as well can race its final interpreter shutdown and turn a
            # successful teardown into a signal failure after handlers reset.
            try:
                async with asyncio.timeout(_SHUTDOWN_SECONDS):
                    await process.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        # A descendant can keep stdout open after its owner exits. Its pipe must
        # not prevent bounded shutdown or cause us to retain any output payload.
        for drain in self.drains:
            drain.cancel()
        await asyncio.gather(*self.drains, return_exceptions=True)
        # Children inherit the owner's fence. Do not report stopped or allow a
        # replacement if an old child survived the bounded owner teardown.
        RuntimeLock(self.root, "supervisor.lock").close()

    async def serve(self, stopped: asyncio.Event) -> bool:
        """Run one verified owner lifetime; return false for an actionable failure.

        Cancellation waits for owned process cleanup. Local pending selection is
        recovered before launch; provider operations are never replayed here.
        A disabled or absent selection returns a successful stopped observation.
        """
        lock = RuntimeLock(self.root, "service.lock")
        success = False
        failure: (
            Literal[
                "verification_failed",
                "owner_exited",
                "ownership_busy",
                "service_failed",
            ]
            | None
        ) = None
        waiter: asyncio.Task[bool] | None = None
        exited: asyncio.Task[int] | None = None
        try:
            try:
                if self.selector.pending.exists():
                    await self.selector.recover()
                self.selection = self.selector.current()
                self._status("verifying")
                if (
                    self.selection is None
                    or not self.selection.enabled
                    or stopped.is_set()
                ):
                    success = True
                else:
                    async with self.selector.installer.locked_generation(
                        self.selection.runtime_digest
                    ):
                        if self.selector.current() != self.selection:
                            raise ValueError("runtime selection changed")
                        RuntimeLock(self.root, "supervisor.lock").close()
                        if not stopped.is_set():
                            await finish_runtime_work(
                                asyncio.create_task(self._spawn(lock))
                            )
                    if self.process is None:
                        success = True
                    else:
                        self._status("running")
                        waiter = asyncio.create_task(stopped.wait())
                        exited = asyncio.create_task(self.process.wait())
                        while True:
                            done, _ = await asyncio.wait(
                                (waiter, exited),
                                timeout=_CHECK_SECONDS,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if waiter in done:
                                success = True
                                break
                            if exited in done:
                                failure = "owner_exited"
                                break
                            try:
                                async with self.selector.installer.locked_generation(
                                    self.selection.runtime_digest
                                ):
                                    if self.selector.current() != self.selection:
                                        raise ValueError("runtime selection changed")
                            except BlockingIOError:
                                # Offline staging holds this same lock. This is
                                # contention, not evidence that the owner failed.
                                continue
                            self._status("running")
            except BlockingIOError:
                failure = "ownership_busy"
            except ValueError:
                failure = "verification_failed"
            except OSError:
                failure = "service_failed"
        finally:
            try:
                try:
                    self._status("stopping")
                finally:
                    await finish_runtime_work(asyncio.create_task(self._close()))
            except BlockingIOError:
                success, failure = False, "ownership_busy"
            finally:
                for task in (waiter, exited):
                    if task is not None:
                        task.cancel()
                if waiter is not None:
                    await asyncio.gather(waiter, return_exceptions=True)
                if exited is not None:
                    await asyncio.gather(exited, return_exceptions=True)
                try:
                    self._status(
                        "stopped" if success else "failed",
                        failure if success or failure else "service_failed",
                    )
                finally:
                    lock.close()
        return success


async def _run(root: Path) -> bool:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(shutdown_signal, stopped.set)
    try:
        return await RuntimeService(root).serve(stopped)
    finally:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(shutdown_signal)


def main() -> None:
    """Launch a fixed verified owner using only a locally provisioned root path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        success = asyncio.run(_run(cast(Path, arguments.root)))
    except (OSError, ValueError):
        print(
            "managed plugin service unavailable; inspect local service status",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
