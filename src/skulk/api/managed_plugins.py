# pyright: reportUnusedFunction=false
"""Explicitly scoped HTTP access to the independently supervised local manager."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from skulk.api.operator_auth import (
    TailnetPeerVerifier,
    authorize_plugin_owner_request,
    authorize_plugin_request,
)
from skulk.extensions.loader import LoadedExtensions
from skulk.extensions.managed_services import (
    ManagedInstallation,
    ManagedInventory,
    ManagedServices,
)
from skulk.extensions.runtime_attachment import (
    InstallationIdentifier,
    ProfileIdentifier,
)
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
    OperationRequest,
    ReleaseRequest,
    SourceRegistration,
    SubmitRequest,
)
from skulk.extensions.runtime_selection import RuntimeSelection
from skulk.operator.pairing import OperatorPairingService
from skulk.operator.plugin_scopes import PluginScope


class ProtectedManagementRoute(APIRoute):
    """Keep write-only source credentials out of FastAPI validation error responses."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        """Preserve typed OpenAPI bodies while returning no rejected input values."""
        handler = super().get_route_handler()

        async def protected(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    status_code=422, detail="invalid plugin management request"
                ) from None

        return protected


class RegistrationRequest(BaseModel):
    """Create only an empty local installation; no executable or service-path choice."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    plugin_id: InstallationIdentifier = Field(
        description="Stable managed plugin installation ID."
    )


class ManagedSelection(BaseModel):
    """Current local installation and desired signed runtime, separate from readiness."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    installation: ManagedInstallation = Field(
        description="Current bounded service observation."
    )
    selection: RuntimeSelection | None = Field(
        description="Desired generation, absent until selected."
    )


class InstallStatus(BaseModel):
    """Server-retained installation progress, separate from local activation."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    operation: InstallOperation | None = Field(
        description="Latest accepted installation, if any."
    )


class InstallRecoveryBody(BaseModel):
    """Review the current source revision without changing the original release intent."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    expected_source_revision: int = Field(
        ge=1, description="Owner-reviewed current release source revision."
    )


def create_managed_plugins_router(
    extensions: LoadedExtensions,
    pairing_service: OperatorPairingService | None,
    tailnet_peer_verifier: TailnetPeerVerifier,
) -> APIRouter:
    """Bind fixed local management verbs to explicit plugin grants and owner origin checks."""
    router = APIRouter(
        prefix="/managed", tags=["Plugins"], route_class=ProtectedManagementRoute
    )
    capacity = anyio.CapacityLimiter(8)

    async def authorized(
        request: Request, response: Response, scope: PluginScope
    ) -> ManagedServices:
        await authorize_plugin_request(
            request, pairing_service, scope, tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        if extensions.managed_services is None:
            raise HTTPException(
                status_code=503, detail="local plugin service setup is unavailable"
            )
        return extensions.managed_services

    async def invoke[Result](call: Callable[[], Awaitable[Result]]) -> Result:
        try:
            capacity.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(
                status_code=429, detail="plugin management is busy"
            ) from None
        try:
            async with asyncio.timeout(30):
                return await call()
        except FileNotFoundError:
            raise HTTPException(
                status_code=503,
                detail="local plugin service setup or operation is unavailable",
            ) from None
        except TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="plugin manager response timed out; read the original operation status",
            ) from None
        except (OSError, RuntimeError):
            raise HTTPException(
                status_code=503, detail="local plugin manager is unavailable"
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="local plugin operation refused; refresh selection and operation status",
            ) from None
        finally:
            capacity.release()

    @router.get(
        "",
        response_model=ManagedInventory,
        summary="Read local managed plugin installations",
        description="Read installed plugin selections and stale/process health independently of child availability. Requires plugins:read or direct owner authority. Local setup supplies the manager connection; no path is accepted.",
    )
    async def inventory(request: Request, response: Response) -> ManagedInventory:
        """Read the live local manager and reconcile cached plugin membership."""
        services = await authorized(request, response, "plugins:read")
        return await invoke(services.refresh)

    @router.post(
        "/installations",
        response_model=ManagedInstallation,
        summary="Register an empty managed plugin installation",
        description="Register one stable managed plugin ID using the locally generated host binding. Requires plugins:manage or direct owner authority. Accepts no artifact, path, command, credential or paid approval; does not enable a runtime.",
    )
    async def register(
        body: RegistrationRequest, request: Request, response: Response
    ) -> ManagedInstallation:
        """Create the manager-owned installation through the same terminal operation."""
        services = await authorized(request, response, "plugins:manage")

        async def action() -> ManagedInstallation:
            result = await services.request(
                InstallationRequest(action="register", plugin_id=body.plugin_id)
            )
            return ManagedInstallation.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.get(
        "/installations/{plugin_id}",
        response_model=ManagedSelection,
        summary="Read a managed plugin's selected runtime",
        description="Read exact desired runtime selection and current local process observation. Requires plugins:read or direct owner authority. Disabled selections retain their state and records.",
    )
    async def selection(
        plugin_id: InstallationIdentifier, request: Request, response: Response
    ) -> ManagedSelection:
        """Inspect one exact registered installation, without activating it."""
        services = await authorized(request, response, "plugins:read")

        async def action() -> ManagedSelection:
            result = await services.request(
                InstallationRequest(action="get", plugin_id=plugin_id)
            )
            return ManagedSelection.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.post(
        "/installations/{plugin_id}/operations",
        response_model=LifecycleOperation,
        summary="Submit a local plugin lifecycle operation",
        description="Submit revision-fenced activate, select, disable or uninstall with a retained operation_id. Activation and stopped selection require an already staged verified runtime. Select keeps the owner stopped for offline setup or migration until a later explicit activation; explicit rollback and permission acceptance are separate flags. Requires plugins:manage or direct owner authority. Client disconnect does not abandon accepted work. Explicit disable or uninstall may withdraw a stalled activation or selection without executing its release; it preserves the prior operation through withdraws_operation_id and terminal superseded history when publication never happened. Live work and pending withdrawal cannot be superseded. Uninstall retains credentials, records, runtime artifacts and independent cleanup supervision; inventory reports uninstalled until a later verified select or activate. It does not purge data. This never approves spending or replays provider requests.",
    )
    async def submit(
        plugin_id: InstallationIdentifier,
        body: LifecycleRequest,
        request: Request,
        response: Response,
    ) -> LifecycleOperation:
        """Submit exact local lifecycle intent with the caller's durable operation ID."""
        services = await authorized(request, response, "plugins:manage")

        async def action() -> LifecycleOperation:
            result = await services.request(
                SubmitRequest(plugin_id=plugin_id, request=body)
            )
            return LifecycleOperation.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.get(
        "/installations/{plugin_id}/operations/{operation_id}",
        response_model=LifecycleOperation,
        summary="Read a retained local plugin operation",
        description="Read the original accepted local operation after reconnect or manager restart. Requires plugins:read or direct owner authority. Read status before resubmitting any request whose response was lost.",
    )
    async def operation(
        plugin_id: InstallationIdentifier,
        operation_id: ProfileIdentifier,
        request: Request,
        response: Response,
    ) -> LifecycleOperation:
        """Read the original local operation result without repeating its effect."""
        services = await authorized(request, response, "plugins:read")

        async def action() -> LifecycleOperation:
            result = await services.request(
                OperationRequest(plugin_id=plugin_id, operation_id=operation_id)
            )
            return LifecycleOperation.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.post(
        "/installations/{plugin_id}/operations/{operation_id}/recover",
        response_model=LifecycleOperation,
        summary="Recover an interrupted local plugin operation",
        description="Explicitly finish only an existing journaled local selection using the original operation ID. Completed or superseded operations are not reapplied. Requires plugins:manage or direct owner authority. Recovery never recreates a provider resource or grants spending approval.",
    )
    async def recover(
        plugin_id: InstallationIdentifier,
        operation_id: ProfileIdentifier,
        request: Request,
        response: Response,
    ) -> LifecycleOperation:
        """Resume only previously accepted local lifecycle intent."""
        services = await authorized(request, response, "plugins:manage")

        async def action() -> LifecycleOperation:
            result = await services.request(
                OperationRequest(
                    action="recover", plugin_id=plugin_id, operation_id=operation_id
                )
            )
            return LifecycleOperation.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.get(
        "/installations/{plugin_id}/release",
        response_model=ReleaseReview,
        summary="Inspect the configured signed plugin release",
        description="Verify metadata from the owner-configured HTTPS source against current publisher trust and exact host compatibility before artifact download. Requires plugins:read or direct owner authority. Returns immutable digest, version, permissions and size without source paths or credentials; does not stage, activate or approve spending.",
    )
    async def release(
        plugin_id: InstallationIdentifier, request: Request, response: Response
    ) -> ReleaseReview:
        """Review the one configured private source without downloading its artifacts."""
        services = await authorized(request, response, "plugins:read")

        async def action() -> ReleaseReview:
            result = await services.request(
                ReleaseRequest(action="inspect_release", plugin_id=plugin_id)
            )
            return ReleaseReview.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.post(
        "/installations/{plugin_id}/install",
        response_model=InstallOperation,
        summary="Download and stage a reviewed plugin release",
        description="Journal an exact reviewed runtime digest, source revision and operation ID, then download signed artifacts and stage an isolated offline runtime under independent manager ownership. Requires plugins:manage or direct owner authority. Disconnect does not abandon accepted work. This does not activate a runtime or grant spending approval.",
    )
    async def install(
        plugin_id: InstallationIdentifier,
        body: InstallRequest,
        request: Request,
        response: Response,
    ) -> InstallOperation:
        """Accept immutable staging intent without a caller URL or executable path."""
        services = await authorized(request, response, "plugins:manage")

        async def action() -> InstallOperation:
            result = await services.request(
                InstallSubmission(plugin_id=plugin_id, request=body)
            )
            return InstallOperation.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.get(
        "/installations/{plugin_id}/install",
        response_model=InstallStatus,
        summary="Read retained plugin installation progress",
        description="Read the latest original installation after reconnect or manager restart. Requires plugins:read or direct owner authority. Interrupted work reports recovery_required and is never automatically repeated. Staged is separate from activation and capability readiness.",
    )
    async def installation_status(
        plugin_id: InstallationIdentifier, request: Request, response: Response
    ) -> InstallStatus:
        """Restore installation observation from the server without another POST."""
        services = await authorized(request, response, "plugins:read")

        async def action() -> InstallStatus:
            result = await services.request(
                ReleaseRequest(action="install_status", plugin_id=plugin_id)
            )
            return InstallStatus.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.get(
        "/installations/{plugin_id}/source",
        response_model=SourceStatus,
        summary="Read plugin release source readiness",
        description="Read source/trust revisions and write-only credential reference readiness without network access or stored secret values. Requires plugins:read or direct owner authority.",
    )
    async def source_status(
        plugin_id: InstallationIdentifier,
        request: Request,
        response: Response,
    ) -> SourceStatus:
        """Observe readiness without disclosing the stored feed token or local path."""
        services = await authorized(request, response, "plugins:read")

        async def action() -> SourceStatus:
            result = await services.request(
                ReleaseRequest(action="source_status", plugin_id=plugin_id)
            )
            return SourceStatus.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.post(
        "/installations/{plugin_id}/source",
        response_model=SourceStatus,
        summary="Configure an owner-trusted plugin release source",
        description="Direct localhost/Tailscale owner administration only: update the HTTPS directory, metadata basename and publisher trust at expected_revision, optionally provisioning a write-only feed token. Omitted fields retain configured values; initial setup requires missing directory, metadata and trust. Existing revocations remain in force. Paired or relay plugin grants cannot replace trust or credential destinations. Omitted token retains its reference; changing a credential-bearing source requires explicit replacement or clear_token. No download, activation or paid request is made.",
    )
    async def configure_source(
        plugin_id: InstallationIdentifier,
        body: SourceUpdate,
        request: Request,
        response: Response,
    ) -> SourceStatus:
        """Provision explicit owner trust through a fixed operation without root authority."""
        await authorize_plugin_owner_request(request, tailnet_peer_verifier)
        services = await authorized(request, response, "plugins:manage")

        async def action() -> SourceStatus:
            result = await services.request(
                SourceRegistration(plugin_id=plugin_id, request=body)
            )
            return SourceStatus.model_validate_json(json.dumps(result))

        return await invoke(action)

    @router.post(
        "/installations/{plugin_id}/install/{operation_id}/recover",
        response_model=InstallOperation,
        summary="Recover an interrupted plugin installation",
        description="Explicitly resume the original signed release under expected_source_revision. Requires plugins:manage or direct owner authority. Credential rotation may change the source but never the original digest or intent. Retains prior attempts and incomplete runtime evidence; refuses rebuilding a selected or pending generation. Completed work is not repeated. Does not activate or make provider requests.",
    )
    async def recover_installation(
        plugin_id: InstallationIdentifier,
        operation_id: ProfileIdentifier,
        body: InstallRecoveryBody,
        request: Request,
        response: Response,
    ) -> InstallOperation:
        """Retry only the original nonbillable installation after explicit operator action."""
        services = await authorized(request, response, "plugins:manage")

        async def action() -> InstallOperation:
            result = await services.request(
                InstallRecoveryRequest(
                    plugin_id=plugin_id,
                    operation_id=operation_id,
                    expected_source_revision=body.expected_source_revision,
                )
            )
            return InstallOperation.model_validate_json(json.dumps(result))

        return await invoke(action)

    return router
