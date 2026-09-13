# pyright: reportUnusedFunction=false
"""Owner-authorized, schema-driven configuration of installed capability nodes."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field, JsonValue

from skulk.api.managed_plugins import (
    ProtectedManagementRoute,
    create_managed_plugins_router,
)
from skulk.api.operator_auth import TailnetPeerVerifier, authorize_plugin_request
from skulk.connectivity.tailscale import is_tailscale_peer
from skulk.extensions.configuration import (
    ConfigurableNode,
    ConfigurationMutation,
    ConfigurationResult,
    NodeConfiguration,
    NodeConfigurationProvider,
)
from skulk.extensions.credentials import (
    CredentialMutation,
    NodeCredentialProvider,
    NodeCredentials,
)
from skulk.extensions.host_network import HostNetwork
from skulk.extensions.loader import LoadedExtensions
from skulk.extensions.preflight import NodePreflight, NodePreflightProvider
from skulk.extensions.proposal_actions import (
    NodeProposalActionsProvider,
    ProposalApproval,
    ProposalOperation,
    ProposalOperationId,
)
from skulk.extensions.proposal_review import (
    NodeProposalReviewProvider,
    ProposalPage,
    ProposalReference,
    ProposalReview,
)
from skulk.extensions.setup import NodeSetup, NodeSetupProvider
from skulk.extensions.setup_actions import (
    NodeSetupActionsProvider,
    SetupActions,
    SetupMutation,
    SetupOperation,
    SetupOperationId,
    SetupResume,
)
from skulk.operator.pairing import OperatorPairingService
from skulk.utils.pydantic_ext import FrozenModel

_Result = TypeVar("_Result")


class PluginNodes(FrozenModel):
    """Installed nodes exposed by one plugin, including management availability."""

    plugin_id: str = Field(description="Local installed extension identifier.")
    nodes: tuple[ConfigurableNode, ...] = Field(
        description="Persistent installed nodes, including disabled nodes."
    )
    available: bool = Field(
        description="Whether the management provider answered this read."
    )


def _public_configuration(value: NodeConfiguration, node_id: str) -> NodeConfiguration:
    """Bound ordinary responses and reject credentials misdeclared as settings."""
    if value.node_id != node_id or len(value.model_dump_json().encode()) > 131072:
        raise ValueError("invalid configuration response")
    _ordinary_schema(value.configuration_schema)
    return value


def _ordinary_schema(schema: dict[str, JsonValue]) -> None:
    """Reject secret-bearing forms before returning ordinary management schemas."""
    pending: list[JsonValue] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            # Secret replacement gets its own write-only interface. Refuse a
            # mixed schema rather than relying on a renderer to hide its values.
            if item.get("writeOnly") is True or item.get("format") == "password":
                raise ValueError("credentials cannot be returned as ordinary settings")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def create_plugins_router(
    extensions: LoadedExtensions,
    pairing_service: OperatorPairingService | None,
    *,
    tailnet_peer_verifier: TailnetPeerVerifier = is_tailscale_peer,
    host_network: Callable[[], Awaitable[HostNetwork]] | None = None,
) -> APIRouter:
    """Expose ordinary configuration through optional plugin management facets.

    Management is intentionally independent of capability readiness. Providers
    validate their own settings; the API owns authorization, bounded dispatch
    and payload-safe failure responses. Distinct reviewed owner actions use approval
    scopes; executable selection and replacement provider inputs are never accepted.
    """
    router = APIRouter(
        prefix="/v1/plugins", tags=["Plugins"], route_class=ProtectedManagementRoute
    )
    router.include_router(
        create_managed_plugins_router(
            extensions, pairing_service, tailnet_peer_verifier
        )
    )
    capacity = anyio.CapacityLimiter(8)

    @router.get(
        "/host-network",
        response_model=HostNetwork,
        summary="Read local plugin attachment endpoints",
        description=(
            "Return actual bound control/data TCP listeners and local peer identity "
            "for plugin-owned secure attachment. Includes the required data transport "
            "and a non-routing namespace comparison digest; never namespace secrets. "
            "Requires plugins:read or direct owner authority. Does not connect, "
            "reconfigure or restart the host. Unavailable listeners return 503."
        ),
    )
    async def read_host_network(request: Request, response: Response) -> HostNetwork:
        """Authorize before inspecting live listeners; return no cached observations."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        if host_network is None:
            raise HTTPException(status_code=503, detail="host network unavailable")
        try:
            capacity.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(status_code=429, detail="plugin management busy") from None
        try:
            async with asyncio.timeout(2):
                return await host_network()
        except Exception:
            # Native networking errors can contain private routing addresses or
            # namespace material. Only the typed successful projection is public.
            raise HTTPException(
                status_code=503, detail="host network unavailable"
            ) from None
        finally:
            capacity.release()

    async def invoke(call: Callable[[], Awaitable[_Result]]) -> _Result:
        try:
            capacity.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(
                status_code=429, detail="plugin management is busy"
            ) from None
        try:
            async with asyncio.timeout(30):
                return await call()
        except LookupError:
            raise HTTPException(
                status_code=404, detail="installed node not found"
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=409,
                detail="configuration refused; reload settings and validation",
            ) from None
        except TimeoutError:
            raise HTTPException(
                status_code=504, detail="plugin management timed out"
            ) from None
        except Exception:  # noqa: BLE001 - provider failures must not expose payloads
            raise HTTPException(
                status_code=503, detail="plugin management unavailable"
            ) from None
        finally:
            capacity.release()

    def provider(plugin_id: str) -> NodeConfigurationProvider:
        selected = extensions.configuration_providers.get(plugin_id)
        if selected is None:
            raise HTTPException(
                status_code=404, detail="configuration provider not found"
            )
        return selected

    @router.get(
        "",
        response_model=tuple[PluginNodes, ...],
        summary="List configurable plugin nodes",
        description=(
            "List installed capability nodes and management availability without "
            "filtering disabled or failed children. Requires direct dashboard owner "
            "authority or the explicit plugins:read paired-operator scope."
        ),
    )
    async def list_nodes(
        request: Request, response: Response
    ) -> tuple[PluginNodes, ...]:
        """Read bounded installed-node inventories without starting children."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"

        async def read(
            plugin_id: str, selected: NodeConfigurationProvider
        ) -> PluginNodes:
            try:
                async with asyncio.timeout(5):
                    nodes = await selected.configuration_nodes()
                result = PluginNodes(plugin_id=plugin_id, nodes=nodes, available=True)
                if len(nodes) > 128 or len(result.model_dump_json().encode()) > 131072:
                    raise ValueError("inventory exceeds bound")
                if len({node.node_id for node in nodes}) != len(nodes):
                    raise ValueError("ambiguous node identity")
                return result
            except Exception:  # noqa: BLE001 - inventory errors contain no provider text
                return PluginNodes(plugin_id=plugin_id, nodes=(), available=False)

        async def collect() -> tuple[PluginNodes, ...]:
            providers = extensions.configuration_providers
            if len(providers) > 32:
                raise ValueError("too many management providers")
            return tuple(
                await asyncio.gather(
                    *(read(key, value) for key, value in providers.items())
                )
            )

        return await invoke(collect)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/configuration",
        response_model=NodeConfiguration,
        summary="Read a capability node's configuration",
        description=(
            "Return the plugin-provided ordinary-settings schema, values, enabled "
            "state and revision for one persistent installed node. Multiple "
            "capabilities may share this node configuration. Secret values are excluded. "
            "Requires direct owner authority or plugins:read."
        ),
    )
    async def get_configuration(
        plugin_id: str, node_id: str, request: Request, response: Response
    ) -> NodeConfiguration:
        """Return the configuration of this exact installed node."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = provider(plugin_id)

        async def read() -> NodeConfiguration:
            return _public_configuration(
                await selected.node_configuration(node_id), node_id
            )

        return await invoke(read)

    @router.post(
        "/{plugin_id}/nodes/{node_id}/configuration",
        response_model=ConfigurationResult,
        summary="Validate or change capability-node configuration",
        description=(
            "Validate, edit, enable or disable one node using the exact observed "
            "configuration revision and schema digest. The plugin owns validation "
            "and preflight. This route cannot approve paid effects. Requires direct "
            "owner authority or plugins:manage; conflicts require a fresh read."
        ),
    )
    async def change_configuration(
        plugin_id: str,
        node_id: str,
        mutation: ConfigurationMutation,
        request: Request,
        response: Response,
    ) -> ConfigurationResult:
        """Dispatch one revision-fenced ordinary-settings action to its owner."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:manage", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = provider(plugin_id)
        if (mutation.operation in {"validate", "edit"}) != (
            mutation.values is not None
        ):
            raise HTTPException(
                status_code=422, detail="values are required only for validate/edit"
            )

        async def change() -> ConfigurationResult:
            result = await selected.configure_node(node_id, mutation)
            _public_configuration(result.configuration, node_id)
            return result

        return await invoke(change)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/preflight",
        response_model=NodePreflight,
        summary="Check a capability node's setup prerequisites",
        description="Run bounded nonbillable plugin-declared prerequisite checks for the exact installed node, including disabled or failed children. Returns stable check codes, corrective actions, observed configuration and credential revisions, ordinary-values digest and observation time. Local storage probes may create and remove protected test files; this does not approve spending or enable the node. Enable and restart enforce fresh checks in the plugin. Requires direct owner authority or plugins:read. Results are observations, never reusable admission tokens.",
    )
    async def get_preflight(
        plugin_id: str, node_id: str, request: Request, response: Response
    ) -> NodePreflight:
        """Return bounded safe prerequisite results without changing node enablement."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = provider(plugin_id)
        if not isinstance(selected, NodePreflightProvider):
            raise HTTPException(status_code=404, detail="preflight is not supported")

        async def read() -> NodePreflight:
            result = await selected.node_preflight(node_id)
            if (
                result.node_id != node_id
                or len({item.code for item in result.checks}) != len(result.checks)
                or len(result.model_dump_json().encode()) > 65536
            ):
                raise ValueError("invalid preflight response")
            return result

        return await invoke(read)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/setup",
        response_model=NodeSetup,
        summary="Read a capability node's public setup files",
        description="Read bounded plugin-declared public setup files for an exact installed node, including disabled children. Returns the current configuration and credential revisions. This read never generates keys, replaces credentials, enables a node or approves spending. Requires direct owner authority or plugins:read. Secret values and executable content are outside this contract; file names and inert text media types are bounded.",
    )
    async def get_setup(
        plugin_id: str, node_id: str, request: Request, response: Response
    ) -> NodeSetup:
        """Return current public setup artifacts under explicit plugin read authority."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = provider(plugin_id)
        if not isinstance(selected, NodeSetupProvider):
            raise HTTPException(status_code=404, detail="setup export is not supported")

        async def read() -> NodeSetup:
            result = await selected.node_setup(node_id)
            if (
                result.node_id != node_id
                or len({item.name for item in result.artifacts})
                != len(result.artifacts)
                or len(result.model_dump_json().encode()) > 65536
            ):
                raise ValueError("invalid setup response")
            return result

        return await invoke(read)

    def credentials_provider(plugin_id: str) -> NodeCredentialProvider:
        selected = provider(plugin_id)
        if not isinstance(selected, NodeCredentialProvider):
            raise HTTPException(
                status_code=404, detail="credential management is not supported"
            )
        return selected

    def public_credentials(value: NodeCredentials, node_id: str) -> NodeCredentials:
        if (
            value.node_id != node_id
            or len(value.model_dump_json().encode()) > 65536
            or len({item.credential_id for item in value.credentials})
            != len(value.credentials)
        ):
            raise ValueError("invalid credential metadata")
        return value

    @router.get(
        "/{plugin_id}/nodes/{node_id}/credentials",
        response_model=NodeCredentials,
        summary="Read capability-node credential readiness",
        description="Return plugin-declared credential references, labels, requirements and readiness with the current credential revision and declaration digest. Never returns values, value fingerprints or backend paths. Requires direct owner authority or plugins:read. Available independently of capability readiness when the plugin supports credential management.",
    )
    async def get_credentials(
        plugin_id: str, node_id: str, request: Request, response: Response
    ) -> NodeCredentials:
        """Read only safe metadata from the exact installed node's credential provider."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = credentials_provider(plugin_id)

        async def read() -> NodeCredentials:
            return public_credentials(await selected.node_credentials(node_id), node_id)

        return await invoke(read)

    @router.post(
        "/{plugin_id}/nodes/{node_id}/credentials",
        response_model=NodeCredentials,
        summary="Replace or retire a capability-node credential",
        description="Apply one plugin-declared write-only credential replacement or retirement using operation_id, credential_id, expected_revision and expected_schema_digest. Replacement requires value; retirement prohibits it. Providers retain credential history needed by cleanup and durably deduplicate exact operation IDs. Does not enable a capability or approve spending. Requires direct owner authority or plugins:manage. An unconfirmed response requires reading current metadata before another action; values are excluded from validation errors.",
    )
    async def change_credentials(
        plugin_id: str,
        node_id: str,
        mutation: CredentialMutation,
        request: Request,
        response: Response,
    ) -> NodeCredentials:
        """Forward one bounded secret-bearing operation and return metadata only."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:manage", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        if (mutation.operation == "replace") != (mutation.value is not None):
            raise HTTPException(
                status_code=422, detail="value is required only for replacement"
            )
        if (
            mutation.value is not None
            and not 0 < len(mutation.value.get_secret_value().encode()) <= 4096
        ):
            raise HTTPException(status_code=422, detail="credential exceeds bound")
        selected = credentials_provider(plugin_id)

        async def change() -> NodeCredentials:
            return public_credentials(
                await selected.change_node_credential(node_id, mutation), node_id
            )

        return await invoke(change)

    def setup_actions_provider(plugin_id: str) -> NodeSetupActionsProvider:
        selected = provider(plugin_id)
        if not isinstance(selected, NodeSetupActionsProvider):
            raise HTTPException(status_code=404, detail="setup actions unavailable")
        return selected

    def setup_observation(
        value: SetupOperation, node_id: str, operation_id: str
    ) -> SetupOperation:
        if value.node_id != node_id or value.operation_id != operation_id:
            raise ValueError("setup observation identity changed")
        return value

    @router.get(
        "/{plugin_id}/nodes/{node_id}/setup-actions",
        response_model=SetupActions,
        summary="Read installed nonbillable setup actions",
        description="Return ordinary action forms, configuration and credential fences, and retained safe progress. Reads do not initialize credentials or perform setup. Available independently of child readiness. Requires owner authority or plugins:read.",
    )
    async def get_setup_actions(
        plugin_id: str, node_id: str, request: Request, response: Response
    ) -> SetupActions:
        """Read bounded ordinary forms and progress for this installed node."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = setup_actions_provider(plugin_id)

        async def read() -> SetupActions:
            value = await selected.node_setup_actions(node_id)
            if (
                value.node_id != node_id
                or len(value.model_dump_json().encode()) > 65536
                or len({a.action_id for a in value.actions}) != len(value.actions)
                or len({o.operation_id for o in value.operations})
                != len(value.operations)
                or any(o.node_id != node_id for o in value.operations)
            ):
                raise ValueError("invalid setup actions")
            for action in value.actions:
                _ordinary_schema(action.parameters_schema)
            return value

        return await invoke(read)

    @router.post(
        "/{plugin_id}/nodes/{node_id}/setup-operations",
        response_model=SetupOperation,
        summary="Start a nonbillable owner setup operation",
        description="Accept an exact action, ordinary values, operation ID and configuration/credential/action revision and schema fences. The provider durably reserves intent before work and continues independently of browser disconnect. A lost response requires observing the same ID, not starting a replacement. Does not enable a node or approve spending. Requires owner authority or plugins:manage, plus plugins:approve when the installed action declares requiresApproval.",
    )
    async def start_setup_operation(
        plugin_id: str,
        node_id: str,
        mutation: SetupMutation,
        request: Request,
        response: Response,
    ) -> SetupOperation:
        """Dispatch one bounded setup intent with no executable or secret fields."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:manage", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        if len(mutation.model_dump_json().encode()) > 16384:
            raise HTTPException(status_code=422, detail="setup intent exceeds bound")
        selected = setup_actions_provider(plugin_id)

        async def requirement() -> bool:
            actions = await selected.node_setup_actions(node_id)
            matching = tuple(
                a for a in actions.actions if a.action_id == mutation.action_id
            )
            if actions.node_id != node_id or len(matching) != 1:
                raise ValueError("setup action is unavailable")
            action = matching[0]
            _ordinary_schema(action.parameters_schema)
            if action.requires_approval != mutation.expected_requires_approval:
                raise ValueError("setup authorization requirement changed")
            return action.requires_approval

        if await invoke(requirement):
            await authorize_plugin_request(
                request, pairing_service, "plugins:approve", tailnet_peer_verifier
            )

        async def start() -> SetupOperation:
            value = setup_observation(
                await selected.start_node_setup(node_id, mutation),
                node_id,
                mutation.operation_id,
            )
            if (
                value.action_id != mutation.action_id
                or value.requires_approval != mutation.expected_requires_approval
            ):
                raise ValueError("setup action identity changed")
            return value

        return await invoke(start)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/setup-operations/{operation_id}",
        response_model=SetupOperation,
        summary="Read retained setup progress",
        description="Read the last durable observation for an exact installed node and accepted operation ID. This does not wait for running setup or infer readiness from completion. Requires owner authority or plugins:read.",
    )
    async def get_setup_operation(
        plugin_id: str,
        node_id: str,
        operation_id: SetupOperationId,
        request: Request,
        response: Response,
    ) -> SetupOperation:
        """Observe setup independently of the original browser connection."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = setup_actions_provider(plugin_id)

        async def read() -> SetupOperation:
            return setup_observation(
                await selected.node_setup_operation(node_id, operation_id),
                node_id,
                operation_id,
            )

        return await invoke(read)

    @router.post(
        "/{plugin_id}/nodes/{node_id}/setup-operations/{operation_id}/resume",
        response_model=SetupOperation,
        summary="Resume an accepted owner setup operation",
        description="Explicitly resume the original retained nonbillable intent by operation ID. No replacement input, new approval or automatic provider-create retry is accepted. Current prerequisites are revalidated. Requires owner authority or plugins:manage, plus plugins:approve when either the retained or current action requires approval access.",
    )
    async def resume_setup_operation(
        plugin_id: str,
        node_id: str,
        operation_id: SetupOperationId,
        request: Request,
        response: Response,
        body: SetupResume | None = None,
    ) -> SetupOperation:
        """Resume retained intent without redefining its reviewed inputs."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:manage", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = setup_actions_provider(plugin_id)

        async def requirement() -> tuple[SetupOperation, bool]:
            original = setup_observation(
                await selected.node_setup_operation(node_id, operation_id),
                node_id,
                operation_id,
            )
            actions = await selected.node_setup_actions(node_id)
            matching = tuple(
                a for a in actions.actions if a.action_id == original.action_id
            )
            if actions.node_id != node_id or len(matching) != 1:
                raise ValueError("retained setup action is unavailable")
            # The retained requirement cannot be downgraded by a newer manifest;
            # a newly strengthened requirement also applies before recovery.
            return original, original.requires_approval or matching[0].requires_approval

        original, requires_approval = await invoke(requirement)
        if requires_approval:
            await authorize_plugin_request(
                request, pairing_service, "plugins:approve", tailnet_peer_verifier
            )

        async def resume() -> SetupOperation:
            result = setup_observation(
                await selected.resume_node_setup(node_id, operation_id),
                node_id,
                operation_id,
            )
            if (
                result.action_id != original.action_id
                or result.requires_approval != original.requires_approval
            ):
                raise ValueError("retained setup authorization changed")
            return result

        return await invoke(resume)

    def proposal_provider(plugin_id: str) -> NodeProposalReviewProvider:
        selected = provider(plugin_id)
        if not isinstance(selected, NodeProposalReviewProvider):
            raise HTTPException(
                status_code=404, detail="proposal review is not supported"
            )
        return selected

    @router.get(
        "/{plugin_id}/nodes/{node_id}/proposals",
        response_model=ProposalPage,
        summary="List retained plugin proposals",
        description="Read up to sixteen safe retained proposal summaries at offset 0–127 for one installed node. Requires owner authority or plugins:read. Pagination is advisory under concurrent journal changes; selecting a proposal requires a fresh exact-ID-and-digest review. This read creates no proposal, issues no approval and never replays execution.",
    )
    async def list_proposals(
        plugin_id: str,
        node_id: str,
        request: Request,
        response: Response,
        offset: int = Query(
            default=0, ge=0, le=127, description="Advisory journal offset."
        ),
    ) -> ProposalPage:
        """Authorize before asking the exact installation for safe journal metadata."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = proposal_provider(plugin_id)

        async def read() -> ProposalPage:
            page = await selected.node_proposals(node_id, offset)
            if (
                any(
                    item.reference.plugin_id != plugin_id
                    or item.reference.node_id != node_id
                    for item in page.proposals
                )
                or len({item.reference.proposal_id for item in page.proposals})
                != len(page.proposals)
                or (page.next_offset is not None and page.next_offset <= offset)
                or len(page.model_dump_json().encode()) > 131072
            ):
                raise ValueError("invalid proposal listing")
            return page

        return await invoke(read)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/proposals/{proposal_id}",
        response_model=ProposalReview,
        summary="Review an exact retained plugin proposal",
        description="Resolve the provider-owned opaque proposal ID and required immutable proposal_digest for the exact installed plugin/node. Returns bounded plain-text review facts, expiry and observation time; canonical executable input, approval material and credentials remain in the provider. Requires owner authority or plugins:read. A missing or changed reference is refused; this observation grants no execution authority.",
    )
    async def review_proposal(
        plugin_id: str,
        node_id: str,
        proposal_id: str,
        request: Request,
        response: Response,
        proposal_digest: str = Query(
            pattern=r"^[a-f0-9]{64}$",
            description="Digest of the exact reviewed immutable intent.",
        ),
    ) -> ProposalReview:
        """Require the complete immutable reference before returning safe review fields."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"
        selected = proposal_provider(plugin_id)

        async def read() -> ProposalReview:
            reference = ProposalReference(
                plugin_id=plugin_id,
                node_id=node_id,
                proposal_id=proposal_id,
                proposal_digest=proposal_digest,
            )
            result = await selected.node_proposal(reference)
            if (
                result.proposal.reference != reference
                or len(result.model_dump_json().encode()) > 131072
            ):
                raise ValueError(
                    "proposal review does not match the selected reference"
                )
            return result

        return await invoke(read)

    def action_provider(plugin_id: str) -> NodeProposalActionsProvider:
        selected = provider(plugin_id)
        if not isinstance(selected, NodeProposalActionsProvider):
            raise HTTPException(
                status_code=404, detail="proposal actions are not supported"
            )
        return selected

    def check_operation(
        value: ProposalOperation, plugin_id: str, node_id: str, operation_id: str
    ) -> ProposalOperation:
        if (
            value.reference.plugin_id != plugin_id
            or value.reference.node_id != node_id
            or value.operation_id != operation_id
            or len(value.model_dump_json().encode()) > 16384
        ):
            raise ValueError("proposal action response differs")
        return value

    @router.post(
        "/{plugin_id}/nodes/{node_id}/proposals/{proposal_id}/approve",
        response_model=ProposalOperation,
        summary="Approve and execute an exact reviewed plugin proposal",
        description="Distinct owner action requiring plugins:approve, independent of plugins:manage. Accepts only the complete retained reference, provider review revision and durable operation ID. The provider revalidates current terms and retains approval/dispatch progress. Repeated requests observe the same action; reconnect never replays an uncertain submission. Signing credentials and proof remain in the provider.",
    )
    async def approve_proposal(
        plugin_id: str,
        node_id: str,
        proposal_id: str,
        payload: ProposalApproval,
        request: Request,
        response: Response,
    ) -> ProposalOperation:
        """Supply the authenticated actor independently of caller-controlled input."""
        operator_id = await authorize_plugin_request(
            request, pairing_service, "plugins:approve", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"

        async def approve() -> ProposalOperation:
            reference = payload.reference
            if (
                reference.plugin_id != plugin_id
                or reference.node_id != node_id
                or reference.proposal_id != proposal_id
            ):
                raise ValueError("proposal reference differs from route")
            result = await action_provider(plugin_id).approve_node_proposal(
                payload, operator_id
            )
            if result.reference != reference:
                raise ValueError("proposal action returned another reference")
            return check_operation(result, plugin_id, node_id, payload.operation_id)

        return await invoke(approve)

    @router.get(
        "/{plugin_id}/nodes/{node_id}/proposal-operations/{operation_id}",
        response_model=ProposalOperation,
        summary="Read retained owner proposal action status",
        description="Read one safe durable owner action observation with plugins:read. This route performs no approval, dispatch or retry; interrupted or uncertain work remains explicitly recorded. No credentials, canonical input or signed proof are returned.",
    )
    async def proposal_operation(
        plugin_id: str,
        node_id: str,
        operation_id: ProposalOperationId,
        request: Request,
        response: Response,
    ) -> ProposalOperation:
        """Read status after reconnect without repeating any effect."""
        await authorize_plugin_request(
            request, pairing_service, "plugins:read", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"

        async def observe() -> ProposalOperation:
            result = await action_provider(plugin_id).node_proposal_operation(
                node_id, operation_id
            )
            return check_operation(result, plugin_id, node_id, operation_id)

        return await invoke(observe)

    @router.post(
        "/{plugin_id}/nodes/{node_id}/proposal-operations/{operation_id}/resume",
        response_model=ProposalOperation,
        summary="Explicitly resume interrupted owner approval",
        description="Requires current plugins:approve authority. The provider may recover interrupted approval under the same retained intent after fresh policy checks, retaining the original signing identity and auditing the current operator. Submitted execution is never automatically replayed; an uncertain dispatch remains observable. The request accepts no replacement proposal or credentials.",
    )
    async def resume_proposal(
        plugin_id: str,
        node_id: str,
        operation_id: ProposalOperationId,
        request: Request,
        response: Response,
        payload: SetupResume | None = None,
    ) -> ProposalOperation:
        """Recover only the original owner approval after explicit authorization."""
        del payload
        operator_id = await authorize_plugin_request(
            request, pairing_service, "plugins:approve", tailnet_peer_verifier
        )
        response.headers["Cache-Control"] = "no-store"

        async def resume() -> ProposalOperation:
            result = await action_provider(plugin_id).resume_node_proposal(
                node_id, operation_id, operator_id
            )
            return check_operation(result, plugin_id, node_id, operation_id)

        return await invoke(resume)

    return router
