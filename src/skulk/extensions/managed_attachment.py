"""One process-lifetime local bridge to an independently supervised manager."""

import asyncio
import time
from pathlib import Path
from typing import final

from skulk.extensions.runtime_artifacts import measure_host
from skulk.extensions.runtime_attachment import AttachmentRequest
from skulk.extensions.runtime_files import RuntimeLock
from skulk.extensions.runtime_manager import manager_request


@final
class ManagedAttachment:
    """Share a local attachment fence across one API's installed plugin adapters."""

    def __init__(self, root: Path, profile_id: str) -> None:
        """Bind locally provisioned service storage and its stable profile identity."""
        if not root.is_absolute():
            raise ValueError("manager state root must be absolute")
        self.root = root
        self.profile_id = profile_id
        self.lock: RuntimeLock | None = None
        self.guard = asyncio.Lock()
        self.transport_node_id: str | None = None
        self.build: str | None = None
        self.observed = 0.0
        self.users = 0

    def retain(self, transport_node_id: str) -> None:
        """Retain the bridge for one adapter in the same live API process."""
        if (
            self.transport_node_id is not None
            and self.transport_node_id != transport_node_id
        ):
            raise ValueError("bridge already belongs to another Skulk lifetime")
        self.transport_node_id = transport_node_id
        self.users += 1

    async def ensure(self) -> None:
        """Refresh local attachment and compatibility; refuse competing API lifetimes."""
        async with self.guard:
            if self.users == 0 or self.transport_node_id is None:
                raise ValueError("bridge is not active")
            if self.lock is None:
                self.lock = RuntimeLock(self.root, "attachment.lock")
            if time.monotonic() - self.observed < 1:
                return
            if self.build is None:
                host = await asyncio.to_thread(measure_host)
                self.build = host.skulk_build_sha256
            request = AttachmentRequest(
                profile_id=self.profile_id,
                transport_node_id=self.transport_node_id,
                skulk_build_sha256=self.build,
            )
            result = await manager_request(self.root, request)
            if result != {
                "result": {
                    "transport_node_id": self.transport_node_id,
                    "skulk_build_sha256": self.build,
                }
            }:
                raise ValueError("local attachment unavailable or incompatible")
            self.observed = time.monotonic()

    async def release(self) -> None:
        """Release the API fence after its last adapter stops; leave cleanup running."""
        async with self.guard:
            self.users -= 1
            if self.users == 0 and self.lock is not None:
                self.lock.close()
                self.lock = None
                self.observed = 0.0
