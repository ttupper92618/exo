"""Guided local installation over the same durable operations as the dashboard."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import final
from uuid import uuid4

from pydantic import JsonValue, SecretStr, TypeAdapter

from skulk.extensions.runtime_artifacts import RuntimeTrust
from skulk.extensions.runtime_attachment import InstallationIdentifier
from skulk.extensions.runtime_controller import LifecycleOperation, LifecycleRequest
from skulk.extensions.runtime_download import (
    InstallOperation,
    InstallRequest,
    ReleaseReview,
    SourceStatus,
    SourceUpdate,
)
from skulk.extensions.runtime_manager import (
    InstallationRequest,
    InstallRecoveryRequest,
    InstallSubmission,
    ManagerRequest,
    OperationRequest,
    ReleaseRequest,
    SourceRegistration,
    SubmitRequest,
)
from skulk.extensions.runtime_selection import RuntimeSelection

_OBJECT = TypeAdapter(dict[str, JsonValue])
_IDENTIFIER = TypeAdapter[str](InstallationIdentifier)


@final
@dataclass(frozen=True)
class TerminalInstaller:
    """Inject terminal and manager effects; never execute a provider operation.

    The manager retains every accepted effect after this client exits. Prompts
    accept external source/trust settings and separate installation consent;
    identifiers and revision fences come from generated or authoritative state.
    """

    request: Callable[[ManagerRequest], Awaitable[dict[str, JsonValue]]]
    prompt: Callable[[str], str]
    secret: Callable[[str], str]
    output: Callable[[str], None]
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def _call(self, request: ManagerRequest) -> dict[str, JsonValue]:
        response = await self.request(request)
        if "error" in response or "result" not in response:
            # Neither manager errors nor rejected credential inputs belong in
            # terminal diagnostics. The resume command was printed before effects.
            raise ValueError("manager request incomplete; inspect retained status")
        return _OBJECT.validate_python(response["result"], strict=True)

    def _confirm(self, question: str) -> bool:
        return self.prompt(question + " [y/N]: ").strip().lower() == "y"

    def _review(self, review: ReleaseReview) -> None:
        # JSON escapes control characters in publisher-supplied labels/permissions.
        self.output(json.dumps(review.model_dump(mode="json"), indent=2))

    def _source(self) -> SourceUpdate | None:
        address = self.prompt("Trusted HTTPS release directory: ").strip()
        metadata = self.prompt("Metadata filename [release.json]: ").strip()
        publisher = self.prompt("Trusted publisher ID: ").strip()
        public_key = self.prompt("Publisher Ed25519 public key (hex): ").strip()
        expiry = datetime.fromisoformat(
            self.prompt("Trust expiry (UTC, e.g. 2027-01-01T00:00:00Z): ").strip()
        )
        if expiry.tzinfo is None or expiry.timestamp() <= time.time():
            raise ValueError("trust requires an explicit future timezone-aware expiry")
        trust = RuntimeTrust(
            revision=1,
            expires_at=int(expiry.timestamp()),
            publishers={publisher: public_key},
        )
        source = SourceUpdate(
            expected_revision=0,
            base_url=address,
            metadata_filename=metadata or "release.json",
            trust=trust,
        )
        self.output(source.model_dump_json(indent=2, exclude={"token"}))
        if not self._confirm("Trust this publisher for this release source?"):
            return None
        token = self.secret("Feed bearer credential (hidden; blank for anonymous): ")
        return SourceUpdate(
            expected_revision=0,
            base_url=source.base_url,
            metadata_filename=source.metadata_filename,
            trust=trust,
            token=SecretStr(token) if token else None,
            clear_token=not token,
        )

    async def _install_status(self, identifier: str) -> InstallOperation | None:
        result = await self._call(
            ReleaseRequest(action="install_status", plugin_id=identifier)
        )
        value = result["operation"]
        return (
            InstallOperation.model_validate_json(json.dumps(value))
            if value is not None
            else None
        )

    async def _stage(
        self, identifier: str, source: SourceStatus
    ) -> InstallOperation | None:
        operation = await self._install_status(identifier)
        if operation is None:
            review = ReleaseReview.model_validate_json(
                json.dumps(
                    await self._call(
                        ReleaseRequest(action="inspect_release", plugin_id=identifier)
                    )
                )
            )
            self._review(review)
            if not self._confirm("Install this exact verified release?"):
                return None
            request = InstallRequest(
                operation_id=uuid4().hex,
                runtime_digest=review.runtime_digest,
                expected_source_revision=review.source_revision,
            )
            self.output("Installation operation: " + request.operation_id)
            operation = InstallOperation.model_validate_json(
                json.dumps(
                    await self._call(
                        InstallSubmission(plugin_id=identifier, request=request)
                    )
                )
            )
        elif operation.state == "recovery_required":
            self._review(operation.review)
            self.output("Interrupted installation: " + operation.request.operation_id)
            if not self._confirm(
                "Explicitly recover this retained local installation?"
            ):
                return None
            operation = InstallOperation.model_validate_json(
                json.dumps(
                    await self._call(
                        InstallRecoveryRequest(
                            plugin_id=identifier,
                            operation_id=operation.request.operation_id,
                            expected_source_revision=source.revision,
                        )
                    )
                )
            )
        expected = operation.request
        for _ in range(240):
            if operation.request != expected:
                raise ValueError("installation changed; inspect the retained operation")
            self.output(
                f"{operation.state}: {operation.downloaded_bytes} / "
                f"{operation.review.artifact_bytes} bytes"
            )
            if operation.state == "staged":
                return operation
            if operation.state == "recovery_required":
                raise ValueError("installation needs explicit recovery")
            await self.wait(1)
            current = await self._install_status(identifier)
            if current is None:
                raise ValueError("installation history unavailable")
            operation = current
        raise TimeoutError("installation remains owned by the manager")

    async def _activation_status(
        self, identifier: str, operation_id: str
    ) -> LifecycleOperation:
        return LifecycleOperation.model_validate_json(
            json.dumps(
                await self._call(
                    OperationRequest(
                        action="operation",
                        plugin_id=identifier,
                        operation_id=operation_id,
                    )
                )
            )
        )

    async def _activate(self, identifier: str, staged: InstallOperation) -> None:
        state = await self._call(
            InstallationRequest(action="get", plugin_id=identifier)
        )
        summary = _OBJECT.validate_python(state["installation"], strict=True)
        selection = (
            RuntimeSelection.model_validate_json(json.dumps(state["selection"]))
            if state["selection"] is not None
            else None
        )
        operation_id = summary.get("operation_id")
        if operation_id is not None:
            operation = await self._activation_status(
                identifier, TypeAdapter(str).validate_python(operation_id, strict=True)
            )
            if operation.state != "complete":
                # A lost activation response must not become another activation,
                # nor may this wizard recover an unrelated disable or selection.
                if (
                    operation.request.action != "activate"
                    or operation.request.runtime_digest != staged.request.runtime_digest
                ):
                    raise ValueError("another lifecycle operation needs inspection")
                await self._observe_activation(identifier, operation)
                return
        if selection is not None:
            if selection.runtime_digest != staged.request.runtime_digest:
                raise ValueError("selection changed; use explicit lifecycle management")
            if selection.enabled:
                return
        self._review(staged.review)
        if not self._confirm("Accept these permissions and start the plugin owner?"):
            return
        request = LifecycleRequest(
            operation_id=uuid4().hex,
            action="activate",
            expected_revision=selection.revision if selection is not None else 0,
            runtime_digest=staged.request.runtime_digest,
            accept_permissions=True,
        )
        self.output("Activation operation: " + request.operation_id)
        operation = LifecycleOperation.model_validate_json(
            json.dumps(
                await self._call(SubmitRequest(plugin_id=identifier, request=request))
            )
        )
        await self._observe_activation(identifier, operation)

    async def _observe_activation(
        self, identifier: str, operation: LifecycleOperation
    ) -> None:
        request = operation.request
        for _ in range(90):
            if operation.request != request:
                raise ValueError("activation identity changed")
            self.output("Activation: " + operation.state)
            if operation.state == "complete":
                return
            if operation.state not in ("accepted", "applying"):
                raise ValueError("activation requires explicit lifecycle recovery")
            await self.wait(1)
            operation = await self._activation_status(identifier, request.operation_id)
        raise TimeoutError("activation remains owned by the manager")

    async def run(self, plugin_id: str | None = None) -> str:
        """Install a new plugin or observe a retained installation by its printed ID.

        Print the generated identity before registration, ask for trust and
        permissions separately, and retain accepted operations on cancellation.
        This never changes capability-node settings or approves spending. Existing source
        configuration and credentials are retained unchanged when resuming.
        """
        identifier = _IDENTIFIER.validate_python(
            plugin_id if plugin_id is not None else "managed." + uuid4().hex,
            strict=True,
        )
        self.output("Resume: skulk-plugin-service install-plugin " + identifier)
        await self._call(InstallationRequest(action="register", plugin_id=identifier))
        source = SourceStatus.model_validate_json(
            json.dumps(
                await self._call(
                    ReleaseRequest(action="source_status", plugin_id=identifier)
                )
            )
        )
        if not source.configured:
            configuration = self._source()
            if configuration is None:
                return identifier
            source = SourceStatus.model_validate_json(
                json.dumps(
                    await self._call(
                        SourceRegistration(plugin_id=identifier, request=configuration)
                    )
                )
            )
        if not source.credential_ready:
            raise ValueError("replace the unavailable feed credential before resuming")
        staged = await self._stage(identifier, source)
        if staged is not None:
            await self._activate(identifier, staged)
            self.output(
                "Installation retained: "
                + identifier
                + ". Continue through this plugin's documented terminal setup or the Plugins dashboard."
            )
            self.output(
                "After owner activation, run plugin preflight before enabling capability work."
            )
        return identifier
