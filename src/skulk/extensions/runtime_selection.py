"""Journaled stopped-owner runtime selection with retained logical plugin state."""

import os
from pathlib import Path
from typing import Literal, Self, final
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from skulk.extensions.runtime_artifacts import (
    Digest,
    Identifier,
    QualifiedHost,
    RuntimePlatform,
    VerifiedRuntime,
    canonical_json,
)
from skulk.extensions.runtime_files import (
    RuntimeLock,
    private_directory,
    read_private,
    write_private,
)
from skulk.extensions.runtime_install import RuntimeInstaller

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class RuntimeSelection(_Contract):
    """Atomic desired runtime selection, independent of observed process health."""

    protocol: Literal[1] = 1
    revision: int = Field(
        ge=1, description="Monotonic installation selection revision."
    )
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$", description="Operation that selected this runtime."
    )
    runtime_digest: Digest = Field(description="Exact retained signed generation.")
    enabled: bool = Field(
        description="Whether the independent owner may serve this installation."
    )
    bundle_id: Identifier = Field(
        description="Stable plugin identity across generations."
    )
    sequence: int = Field(ge=1, description="Selected publisher release sequence.")
    highest_sequence: int = Field(
        ge=1,
        description="Highest previously selected sequence, retained across rollback.",
    )
    state_schema: Identifier = Field(description="Current durable plugin state schema.")
    permissions: tuple[str, ...] = Field(
        max_length=16, description="Permissions explicitly accepted for this selection."
    )
    configuration_schema: JsonValue = Field(
        description="Opaque signed configuration schema used for migration compatibility."
    )
    platform: RuntimePlatform = Field(
        description="Locally measured qualified platform."
    )
    python_version: str = Field(
        max_length=128, description="Locally measured base Python version."
    )
    skulk_version: str = Field(
        max_length=128, description="Locally measured qualified Skulk version."
    )
    skulk_build_sha256: Digest = Field(
        description="Locally measured qualified Skulk build."
    )


def _omit_false(value: bool) -> bool:
    return not value


def _omit_absent(value: str | None) -> bool:
    return value is None


class SelectionOperation(_Contract):
    """Durable local selection intent and safe reconnect/recovery status."""

    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$", description="Immutable local operation ID."
    )
    state: Literal["pending", "complete", "recovery_required", "superseded"] = Field(
        description="Selection progress, never a provider create operation."
    )
    expected_revision: int = Field(
        ge=0, description="Owner-reviewed previous selection revision."
    )
    selection: RuntimeSelection = Field(
        description="Exact authorized target; no paths or credentials."
    )
    verify_runtime: bool = Field(
        default=False,
        exclude_if=_omit_false,
        description="Reverify a newly selected stopped runtime during recovery; legacy disable journals omit this field.",
    )
    withdraws_operation_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{32}$",
        exclude_if=_omit_absent,
        description="Prior local selection withdrawn by this explicit disable; its history is retained.",
    )

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """Reject intent that could not be read back within the journal bound."""
        if len(canonical_json(self.model_dump(mode="json"))) > 131072:
            raise ValueError("selection intent exceeds bound")
        if self.operation_id != self.selection.operation_id:
            raise ValueError("selection operation identity differs")
        if self.withdraws_operation_id is not None and (
            self.withdraws_operation_id == self.operation_id
            or self.selection.enabled
            or self.verify_runtime
        ):
            raise ValueError("only disable can withdraw another selection")
        return self


def _remove_private(path: Path) -> None:
    path.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest(runtime: VerifiedRuntime) -> dict[str, JsonValue]:
    payload = _JSON_OBJECT.validate_json(runtime.payload)
    release = _JSON_OBJECT.validate_python(payload["release"], strict=True)
    return _JSON_OBJECT.validate_python(release["manifest"], strict=True)


@final
class RuntimeSelector:
    """Select or withdraw one isolated runtime only while its owner is stopped.

    The final selection is one fsynced atomic file. Intent and operation results
    are retained separately so reconnect cannot repeat a change. Recovery only
    completes this authorized local file transition; it never invokes a provider.
    Logical identities, configuration, credentials and cleanup remain untouched.
    """

    def __init__(self, root: Path) -> None:
        """Use protected installation state shared with the generic installer."""
        self.installer = RuntimeInstaller(root)
        self.root = self.installer.root
        self.records = self.installer.installer / "selections"
        private_directory(self.records)
        self.pending = self.records / "pending.json"

    def current(self) -> RuntimeSelection | None:
        """Read the selected runtime without inferring that its process is healthy."""
        try:
            raw = read_private(self.root / "runtime-selection.json")
        except FileNotFoundError:
            return None
        return RuntimeSelection.model_validate_json(raw)

    def operation(self, operation_id: str) -> SelectionOperation:
        """Read retained progress; never launch or replay a local operation."""
        identifier = TypeAdapter(str).validate_python(operation_id, strict=True)
        if len(identifier) != 32 or any(
            c not in "0123456789abcdef" for c in identifier
        ):
            raise ValueError("invalid selection operation ID")
        try:
            raw = read_private(self.records / (identifier + ".json"))
        except FileNotFoundError:
            raw = read_private(self.pending)
        operation = SelectionOperation.model_validate_json(raw)
        if operation.operation_id != identifier:
            raise ValueError("selection operation identity differs")
        return operation

    def _save(self, operation: SelectionOperation) -> None:
        write_private(
            self.records / (operation.operation_id + ".json"),
            canonical_json(operation.model_dump(mode="json")),
        )

    def _settle_withdrawal(self, operation: SelectionOperation) -> None:
        if operation.withdraws_operation_id is None:
            return
        previous = self.operation(operation.withdraws_operation_id)
        # The revision fence tells whether the old atomic selection was already
        # published. Never call a published transition cancelled retroactively.
        state = (
            "complete"
            if previous.selection.revision <= operation.expected_revision
            else "superseded"
        )
        self._save(previous.model_copy(update={"state": state}))

    def _apply(self, operation: SelectionOperation) -> SelectionOperation:
        current = self.current()
        if current != operation.selection and (
            (current.revision if current else 0) != operation.expected_revision
        ):
            raise ValueError("selection revision conflict")
        # A legacy installation needs the explicit stopped-owner migration, not
        # a second source of manifests that could run an unintended sibling.
        if any(self.root.glob("*.manifest.json")):
            raise ValueError("legacy manifests require explicit migration")
        write_private(self.pending, canonical_json(operation.model_dump(mode="json")))
        try:
            self._save(operation)
            write_private(
                self.root / "runtime-selection.json",
                canonical_json(operation.selection.model_dump(mode="json")),
            )
            self._settle_withdrawal(operation)
            complete = operation.model_copy(update={"state": "complete"})
            self._save(complete)
            _remove_private(self.pending)
            return complete
        except OSError:
            self._save(operation.model_copy(update={"state": "recovery_required"}))
            raise

    def _start(
        self, selection: RuntimeSelection, expected_revision: int, *, verify_runtime: bool = False
    ) -> SelectionOperation:
        if self.pending.exists():
            raise ValueError("pending selection requires recovery")
        try:
            prior = self.operation(selection.operation_id)
        except FileNotFoundError:
            prior = None
        if prior is not None:
            if (
                prior.selection != selection
                or prior.expected_revision != expected_revision
                or prior.verify_runtime != verify_runtime
            ):
                raise ValueError("selection operation identity conflict")
            if prior.state != "complete":
                raise ValueError("selection operation requires recovery")
            return prior
        return self._apply(
            SelectionOperation(
                operation_id=selection.operation_id,
                state="pending",
                expected_revision=expected_revision,
                selection=selection,
                verify_runtime=verify_runtime,
            )
        )

    def _candidate(
        self,
        runtime: VerifiedRuntime,
        host: QualifiedHost,
        *,
        expected_revision: int,
        operation_id: str,
        rollback: bool,
        accept_permissions: bool,
        enabled: bool = True,
    ) -> RuntimeSelection:
        current = self.current()
        release = runtime.claims.release
        schema = _manifest(runtime).get("configuration_schema")
        if current is not None:
            if current.bundle_id != release.manifest.bundle_id:
                raise ValueError("installation bundle identity differs")
            if release.sequence < current.highest_sequence and not rollback:
                raise ValueError("rollback requires explicit selection")
            if (
                set(release.permissions) - set(current.permissions)
                and not accept_permissions
            ):
                raise ValueError("expanded permissions require acceptance")
            if (
                current.state_schema != release.state_schema
                and current.state_schema not in release.compatible_state_schemas
            ):
                raise ValueError("incompatible state migration")
            if current.configuration_schema != schema:
                raise ValueError("configuration schema requires migration")
        elif rollback:
            raise ValueError("rollback requires an existing selection")
        return RuntimeSelection(
            revision=expected_revision + 1,
            operation_id=operation_id or uuid4().hex,
            runtime_digest=runtime.digest,
            enabled=enabled,
            bundle_id=release.manifest.bundle_id,
            sequence=release.sequence,
            highest_sequence=max(
                current.highest_sequence if current else 0, release.sequence
            ),
            state_schema=release.state_schema,
            permissions=release.permissions,
            configuration_schema=schema,
            platform=host.platform,
            python_version=host.python_version,
            skulk_version=host.skulk_version,
            skulk_build_sha256=host.skulk_build_sha256,
        )

    async def preview(
        self,
        runtime_digest: str,
        *,
        expected_revision: int,
        operation_id: str,
        rollback: bool = False,
        accept_permissions: bool = False,
        enabled: bool = True,
    ) -> RuntimeSelection:
        """Validate an exact local selection without interrupting a running owner.

        This nonbillable inspection holds installer ownership, verifies the target
        and checks revision, permissions and migration compatibility. Activation
        repeats these checks under stopped-owner ownership before publication.
        Set enabled=False to preview a verified selection that keeps the owner stopped.
        """
        async with self.installer.locked_generation(runtime_digest) as (runtime, host):
            current = self.current()
            if (current.revision if current else 0) != expected_revision:
                raise ValueError("selection revision conflict")
            return self._candidate(
                runtime,
                host,
                expected_revision=expected_revision,
                operation_id=operation_id,
                rollback=rollback,
                accept_permissions=accept_permissions,
                enabled=enabled,
            )

    async def activate(
        self,
        runtime_digest: str,
        *,
        expected_revision: int,
        operation_id: str | None = None,
        rollback: bool = False,
        accept_permissions: bool = False,
        enabled: bool = True,
    ) -> SelectionOperation:
        """Select a verified generation after the caller stops its private owner.

        Expanded permissions and lower release sequences require explicit owner
        choices. State compatibility is publisher-declared; configuration schema
        changes require migration tooling. Set enabled=False to retain the verified
        generation for offline setup without permitting owner startup. Interrupted
        selection still revalidates trust. This does not approve paid proposals.
        """
        async with self.installer.locked_generation(runtime_digest) as (runtime, host):
            owner = RuntimeLock(self.root, "supervisor.lock")
            try:
                selection = self._candidate(
                    runtime,
                    host,
                    expected_revision=expected_revision,
                    operation_id=operation_id or uuid4().hex,
                    rollback=rollback,
                    accept_permissions=accept_permissions,
                    enabled=enabled,
                )
                # Stopped selection still introduces a verified runtime. Its
                # recovery must not use disable's invalid-trust escape path.
                return self._start(selection, expected_revision, verify_runtime=not enabled)
            finally:
                owner.close()

    def preview_disable(
        self,
        *,
        expected_revision: int,
        operation_id: str,
        initial_selection: RuntimeSelection | None = None,
    ) -> RuntimeSelection:
        """Preview withdrawal of current or interrupted initial selection without executing it.

        The caller must repeat this read under the installer and owner fences
        before publication. A pending initial activation supplies identity only;
        disable never treats its release as trusted executable code.
        """
        current = self.current()
        pending = (
            SelectionOperation.model_validate_json(read_private(self.pending))
            if self.pending.exists()
            else None
        )
        revision = current.revision if current is not None else 0
        if revision != expected_revision:
            raise ValueError("selection revision conflict")
        if pending is not None and current != pending.selection and (
            revision != pending.expected_revision
        ):
            raise ValueError("pending selection revision differs")
        if (
            current is None
            and pending is None
            and initial_selection is not None
            and (expected_revision != 0 or initial_selection.revision != 1)
        ):
            raise ValueError("initial withdrawal revision differs")
        base = current or (
            pending.selection if pending is not None else initial_selection
        )
        if base is None:
            raise ValueError("no selected runtime")
        return RuntimeSelection.model_validate_json(
            base.model_copy(
                update={
                    "revision": revision + 1,
                    "operation_id": operation_id,
                    "enabled": False,
                }
            ).model_dump_json()
        )

    def disable(
        self,
        *,
        expected_revision: int,
        operation_id: str | None = None,
        initial_selection: RuntimeSelection | None = None,
    ) -> SelectionOperation:
        """Withdraw a stopped installation even if release trust is now invalid.

        Withdraw interrupted local selection even when its release was revoked.
        Retain its selected version, runtime, logical state and cleanup material.
        An uninstall uses this same retained-state withdrawal in v1.
        """
        identifier = operation_id or uuid4().hex
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("invalid selection operation ID")
        installer = RuntimeLock(self.installer.installer)
        try:
            owner = RuntimeLock(self.root, "supervisor.lock")
            try:
                if (self.records / (identifier + ".json")).exists():
                    prior = self.operation(identifier)
                    if (
                        prior.selection.enabled
                        or prior.verify_runtime
                        or prior.expected_revision != expected_revision
                    ):
                        raise ValueError("selection operation identity conflict")
                    if prior.state != "complete":
                        raise ValueError("selection operation requires recovery")
                    return prior
                selection = self.preview_disable(
                    expected_revision=expected_revision,
                    operation_id=identifier,
                    initial_selection=initial_selection,
                )
                if self.pending.exists():
                    pending = SelectionOperation.model_validate_json(
                        read_private(self.pending)
                    )
                    if pending.operation_id == selection.operation_id:
                        raise ValueError("selection operation requires recovery")
                    if not pending.selection.enabled and not pending.verify_runtime:
                        raise ValueError("pending disable requires recovery")
                    if (self.records / (selection.operation_id + ".json")).exists():
                        raise ValueError("selection operation identity conflict")
                    # Materialize the old history before replacing its sole
                    # pending record. The new pending disable carries the link
                    # so restart can finish withdrawal without trusting old code.
                    previous = self.operation(pending.operation_id)
                    if (
                        previous.selection != pending.selection
                        or previous.expected_revision != pending.expected_revision
                        or previous.verify_runtime != pending.verify_runtime
                    ):
                        raise ValueError("pending selection history differs")
                    self._save(previous)
                    return self._apply(
                        SelectionOperation(
                            operation_id=selection.operation_id,
                            selection=selection,
                            expected_revision=expected_revision,
                            state="pending",
                            withdraws_operation_id=pending.operation_id,
                        )
                    )
                return self._start(selection, expected_revision)
            finally:
                owner.close()
        finally:
            installer.close()

    async def recover(self) -> SelectionOperation:
        """Finish one journaled local selection after interruption and revalidation."""
        operation = SelectionOperation.model_validate_json(read_private(self.pending))
        if operation.selection.enabled or operation.verify_runtime:
            async with self.installer.locked_generation(
                operation.selection.runtime_digest
            ) as (_, host):
                expected = operation.selection
                if host != QualifiedHost(
                    expected.platform,
                    expected.python_version,
                    expected.skulk_version,
                    expected.skulk_build_sha256,
                ):
                    raise ValueError("recovery host differs")
                return self._recover_locked(operation)
        installer = RuntimeLock(self.installer.installer)
        try:
            return self._recover_locked(operation)
        finally:
            installer.close()

    def _recover_locked(self, operation: SelectionOperation) -> SelectionOperation:
        owner = RuntimeLock(self.root, "supervisor.lock")
        try:
            if read_private(self.pending) != canonical_json(
                operation.model_dump(mode="json")
            ):
                raise ValueError("pending selection changed")
            return self._apply(operation)
        finally:
            owner.close()
