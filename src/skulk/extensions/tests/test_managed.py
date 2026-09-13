"""Dynamic plugin registration preserves static ownership and bounded discovery."""

import json
from pathlib import Path

import pytest

from skulk.extensions import (
    CapabilityCall,
    CapabilityDescriptor,
    ExtensionContext,
    LoadedExtensions,
)
from skulk.extensions.managed import load_managed_owners


class Dynamic:
    """Cached contract fixture with independently controlled readiness."""

    skulk_requires = ">=0"

    def __init__(self, name: str) -> None:
        self.name = name
        self.snapshot: tuple[CapabilityDescriptor, ...] = ()
        self.ready = True

    def chat_middleware(self) -> None:
        """No inference interception."""
        return None

    def dynamic_capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """Return only a cached snapshot."""
        return self.snapshot

    def capability_ready(self, qualified_id: str) -> bool:
        """Supply independently changing liveness."""
        return self.ready

    async def handle_call(
        self, context: ExtensionContext, call: CapabilityCall
    ) -> dict[str, object]:
        """Return an inert result for dispatch identity assertions."""
        return {}


class Static:
    """Static contracts reserve their namespace across readiness changes."""

    name = "static"
    skulk_requires = ">=0"

    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self.descriptor = descriptor

    def chat_middleware(self) -> None:
        """No inference interception."""
        return None

    def capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        """Publish the exact statically owned contract."""
        return (self.descriptor,)

    async def handle_call(
        self, context: ExtensionContext, call: CapabilityCall
    ) -> dict[str, object]:
        """Return an inert result for dispatch identity assertions."""
        return {}


def test_dynamic_changes_and_ambiguous_ownership() -> None:
    """New contracts need no reload; unavailable duplicate owners cannot take over."""
    descriptor = CapabilityDescriptor(
        id="dynamic",
        version="1.0.0",
        title="Fixture",
        description="Fixture",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    first, second = Dynamic("first"), Dynamic("second")
    registry = LoadedExtensions([first, second])
    assert not registry.capability_descriptors
    first.snapshot = (descriptor,)
    assert registry.capability_descriptors == (descriptor,)
    entry = registry.call_handler(descriptor.qualified_id)
    assert entry is not None and entry[1] is first
    assert registry.capability_ready(descriptor.qualified_id)
    assert registry.handled_capability_ids() == {"dynamic"}
    second.snapshot = (descriptor,)
    second.ready = False
    assert not registry.capability_descriptors
    assert registry.call_handler(descriptor.qualified_id) is None
    second.snapshot = ()
    assert registry.capability_descriptors == (descriptor,)
    first.ready = False
    assert not registry.capability_descriptors
    assert not registry.capability_ready(descriptor.qualified_id)
    first.ready = True
    static = Static(descriptor.model_copy(update={"version": "2.0.0"}))
    combined = registry.with_builtin_extensions([static])
    assert combined.capability_descriptors == (static.descriptor,)
    assert combined.call_handler(descriptor.qualified_id) is None
    static_entry = combined.call_handler(static.descriptor.qualified_id)
    assert static_entry is not None and static_entry[1] is static


@pytest.mark.parametrize(
    "fault",
    ["none", "directory-mode", "file-mode", "symlink", "duplicate", "bad-sibling"],
)
def test_protected_registration(tmp_path: Path, fault: str) -> None:
    """Only explicit owner-protected local records can introduce managed adapters."""
    directory = tmp_path / "connections"
    directory.mkdir(mode=0o700)
    record = directory / "owner.json"
    record.write_text(
        json.dumps(
            {"plugin_id": "managed.fixture", "state_root": str(tmp_path / "state")}
        )
    )
    record.chmod(0o600)
    if fault == "directory-mode":
        directory.chmod(0o755)
    elif fault == "file-mode":
        record.chmod(0o644)
    elif fault == "symlink":
        other = tmp_path / "source.json"
        record.rename(other)
        record.symlink_to(other)
    elif fault == "duplicate":
        duplicate = directory / "duplicate.json"
        duplicate.write_bytes(record.read_bytes())
        duplicate.chmod(0o600)
    elif fault == "bad-sibling":
        other = directory / "broken.json"
        other.write_text("invalid JSON")
        other.chmod(0o600)
    owners = load_managed_owners(directory)
    assert len(owners) == (1 if fault in {"none", "bad-sibling"} else 0)
