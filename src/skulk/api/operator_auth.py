# pyright: reportUnusedFunction=false
"""FastAPI routes for Skulk operator pairing and credential lifecycle."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Annotated, Final, Never
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from skulk.connectivity.tailscale import is_tailscale_peer
from skulk.operator.pairing import (
    OperatorCredentialExpiredError,
    OperatorCredentialInvalidError,
    OperatorDeviceNotFoundError,
    OperatorDevicesResponse,
    OperatorPairingService,
    OperatorScopeError,
    OperatorTokenRequest,
    OperatorTokenResponse,
    PairingChallengeRequest,
    PairingChallengeResponse,
    PairingExchangeRequest,
    PairingExchangeResponse,
    PairingGatewayNotInitializedError,
    PairingInvitationCapacityError,
    PairingInvitationCreateRequest,
    PairingInvitationCreateResponse,
    PairingInvitationSummary,
    PairingPackageTooLargeError,
    PairingProofError,
    PairingSessionExpiredError,
    PairingSessionNotFoundError,
    PairingSessionStateError,
    PluginGrant,
    PluginGrantUpdate,
)
from skulk.operator.plugin_scopes import PluginScope
from skulk.operator.relay import OperatorRelayUnavailableError

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}
_PAIRING_INVITATION_PATH = "/pairing-invitations"
_DASHBOARD_REQUEST_HEADER = "pairing-v1"
_TAILSCALE_IPV4_NETWORK: Final = IPv4Network("100.64.0.0/10")
_TAILSCALE_IPV6_NETWORK: Final = IPv6Network("fd7a:115c:a1e0::/48")
_PAIRING_AUTHORITY_DETAIL: Final = (
    "Pairing invitations can be managed only from the configured operator gateway. "
    "Open that gateway's dashboard through Tailscale using its MagicDNS name or "
    "Tailscale IP, or open it through localhost. Public relay and ordinary LAN "
    "access are blocked."
)
_PAIRING_GATEWAY_DETAIL: Final = (
    "This node is not ready to manage pairing invitations. Open Settings on the "
    "configured operator gateway through Tailscale or localhost; if this is the "
    "gateway, configure its relay access first."
)
_FORWARDED_REQUEST_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-real-ip",
        b"cf-connecting-ip",
        b"true-client-ip",
    }
)
TailnetPeerVerifier = Callable[[str], Awaitable[bool]]


async def _direct_dashboard_authority_request(
    request: Request,
    tailnet_peer_verifier: TailnetPeerVerifier,
) -> bool:
    """Return whether a browser request came directly over localhost or Tailscale.

    Minting a bearer invitation establishes persistent remote operator access,
    so same-origin headers alone are insufficient. The socket peer must be
    loopback or a Tailscale-assigned address, while the browser origin must
    exactly match the direct dashboard URL and use a loopback, MagicDNS, or
    literal Tailscale host. Forwarding headers are rejected because a proxy
    would otherwise erase the real peer boundary.
    """

    if any(
        name.lower() in _FORWARDED_REQUEST_HEADERS for name, _ in request.headers.raw
    ):
        return False
    if request.headers.get("x-skulk-dashboard") != _DASHBOARD_REQUEST_HEADER:
        return False
    origin = request.headers.get("origin") or request.headers.get("referer")
    client_host = request.client.host if request.client is not None else None
    if origin is None or client_host is None:
        return False

    try:
        parsed_origin = urlsplit(origin)
        origin_port = parsed_origin.port
    except ValueError:
        return False
    origin_hostname = _normalized_hostname(parsed_origin.hostname)
    request_hostname = _normalized_hostname(request.url.hostname)
    if (
        parsed_origin.scheme not in {"http", "https"}
        or parsed_origin.scheme != request.url.scheme
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or origin_hostname is None
        or origin_hostname != request_hostname
        or _effective_port(parsed_origin.scheme, origin_port)
        != _effective_port(request.url.scheme, request.url.port)
    ):
        return False

    if _is_loopback_address(client_host):
        return _is_loopback_hostname(origin_hostname)
    if not (_is_tailnet_address(client_host) and _is_tailnet_hostname(origin_hostname)):
        return False
    return await tailnet_peer_verifier(client_host)


def _normalized_hostname(host: str | None) -> str | None:
    """Normalize one URL hostname for exact same-origin comparison."""

    if host is None:
        return None
    return host.rstrip(".").casefold()


def _effective_port(scheme: str, port: int | None) -> int | None:
    """Return the explicit or default port for an HTTP origin."""

    if port is not None:
        return port
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _is_loopback_address(host: str) -> bool:
    """Return whether a socket peer is an exact loopback IP address."""

    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_hostname(host: str) -> bool:
    """Return whether a same-origin dashboard host denotes loopback."""

    if host == "localhost":
        return True
    return _is_loopback_address(host)


def _is_tailnet_address(host: str) -> bool:
    """Return whether a socket peer is in Tailscale's assigned address space."""

    try:
        address = ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in _TAILSCALE_IPV4_NETWORK
    return address in _TAILSCALE_IPV6_NETWORK


def _is_tailnet_hostname(host: str) -> bool:
    """Return whether a same-origin host is a MagicDNS name or Tailscale IP."""

    if _is_tailnet_address(host):
        return True
    return "." not in host or host.endswith(".ts.net")


async def _require_direct_dashboard_authority(
    request: Request,
    tailnet_peer_verifier: TailnetPeerVerifier,
) -> None:
    """Reject invitation management outside the direct trusted dashboard."""

    if not await _direct_dashboard_authority_request(request, tailnet_peer_verifier):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_PAIRING_AUTHORITY_DETAIL,
        )


def _raise_pairing_gateway_http_error(exc: Exception) -> Never:
    """Map an uninitialized local authority to actionable dashboard guidance."""

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_PAIRING_GATEWAY_DETAIL,
    ) from exc


def _require_bearer(authorization: str | None) -> str:
    """Extract one opaque bearer token without accepting alternate schemes."""

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer credential is required",
            headers=_BEARER_CHALLENGE,
        )
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer credential is invalid",
            headers=_BEARER_CHALLENGE,
        )
    return token.strip()


def _raise_credential_http_error(exc: Exception) -> Never:
    """Map safe credential-domain failures onto stable HTTP semantics."""

    if isinstance(exc, PairingGatewayNotInitializedError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, OperatorScopeError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(
        exc,
        (OperatorCredentialInvalidError, OperatorCredentialExpiredError),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers=_BEARER_CHALLENGE,
        ) from exc
    raise exc


async def authorize_plugin_request(
    request: Request,
    service: OperatorPairingService | None,
    required_scope: PluginScope,
    tailnet_peer_verifier: TailnetPeerVerifier = is_tailscale_peer,
) -> str:
    """Require direct owner authority or an explicitly scoped paired operator.

    A presented bearer is always checked, never silently replaced by ambient
    dashboard authority. Direct HTTP outside the verified owner transport is
    refused; relay ingress has already authenticated its encrypted listener.
    """
    from skulk.api.operator_gateway import OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY

    authorization = request.headers.getlist("authorization")
    if not authorization:
        await _require_direct_dashboard_authority(request, tailnet_peer_verifier)
        return "local-owner"
    if len(authorization) != 1 or service is None:
        raise HTTPException(status_code=401, detail="operator credential invalid")
    try:
        operator = await run_in_threadpool(
            service.validate_access_token,
            _require_bearer(authorization[0]),
            required_scopes=(required_scope,),
        )
    except (
        OperatorCredentialInvalidError,
        OperatorCredentialExpiredError,
        OperatorScopeError,
        PairingGatewayNotInitializedError,
    ) as exc:
        _raise_credential_http_error(exc)
    if (
        request.scope.get(OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY) is not True
        and request.url.scheme != "https"
        and not await _direct_dashboard_authority_request(
            request, tailnet_peer_verifier
        )
    ):
        raise HTTPException(
            status_code=403, detail="plugin management requires protected transport"
        )
    return str(operator.device_id)


async def authorize_plugin_owner_request(
    request: Request,
    tailnet_peer_verifier: TailnetPeerVerifier = is_tailscale_peer,
) -> None:
    """Reserve publisher trust and credential destinations for direct owner administration."""
    from skulk.api.operator_gateway import OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY

    if (
        request.headers.getlist("authorization")
        or request.scope.get(OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY) is True
    ):
        raise HTTPException(
            status_code=403,
            detail="this action requires direct owner authority without a paired credential",
        )
    await _require_direct_dashboard_authority(request, tailnet_peer_verifier)


def create_operator_auth_router(
    service: OperatorPairingService,
    *,
    tailnet_peer_verifier: TailnetPeerVerifier = is_tailscale_peer,
) -> APIRouter:
    """Create narrowly scoped pairing routes for one designated gateway.

    Args:
        service: Encrypted local pairing service owned by the API node.
        tailnet_peer_verifier: Async local-authority check that binds a socket
            address to an actual Tailscale node. Production uses tailscaled;
            tests inject deterministic membership.

    Returns:
        Router exposing pairing, refresh rotation, and paired-device
        management. General Skulk model and inference APIs remain canonical.
    """

    router = APIRouter(prefix="/v1/auth", tags=["Authentication"])

    @router.get(
        "/plugin-grants",
        response_model=tuple[PluginGrant, ...],
        summary="List owner-controlled plugin grants",
        description=(
            "List explicit paired-device plugin privileges from the direct localhost "
            "or verified Tailscale dashboard. This route is unavailable through relay "
            "access and refuses any paired bearer, including on the direct listener. It does not expose credentials. Existing pairings have no plugin grants."
        ),
    )
    async def list_plugin_grants(
        request: Request, response: Response
    ) -> tuple[PluginGrant, ...]:
        """Return safe grant metadata only after verifying direct owner authority."""
        await authorize_plugin_owner_request(request, tailnet_peer_verifier)
        response.headers["Cache-Control"] = "no-store"
        try:
            return await run_in_threadpool(service.plugin_grants)
        except PairingGatewayNotInitializedError as exc:
            _raise_pairing_gateway_http_error(exc)

    @router.put(
        "/plugin-grants/{device_id}",
        response_model=PluginGrant,
        summary="Replace one device's explicit plugin grants",
        description=(
            "Replace plugin read, management and approval grants after a direct owner "
            "dashboard check and expected-revision comparison. An empty scope list "
            "revokes every plugin grant immediately, including for existing access tokens. "
            "A presented paired bearer is refused even with matching direct-owner origin headers. Remote operators cannot call this route or grant themselves privileges."
        ),
    )
    async def replace_plugin_grant(
        device_id: UUID,
        payload: PluginGrantUpdate,
        request: Request,
        response: Response,
    ) -> PluginGrant:
        """Apply a revision-fenced grant without returning or rotating credentials."""
        await authorize_plugin_owner_request(request, tailnet_peer_verifier)
        response.headers["Cache-Control"] = "no-store"
        try:
            return await run_in_threadpool(service.set_plugin_grant, device_id, payload)
        except OperatorDeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            PairingGatewayNotInitializedError,
            OperatorCredentialInvalidError,
            OperatorCredentialExpiredError,
        ) as exc:
            _raise_credential_http_error(exc)

    @router.post(
        _PAIRING_INVITATION_PATH,
        response_model=PairingInvitationCreateResponse,
        response_model_by_alias=True,
        summary="Create a dashboard pairing invitation",
        description=(
            "Create one bounded, revocable pairing invitation from the configured "
            "gateway dashboard over localhost or Tailscale. The secret pairing code "
            "is returned once with no-store response headers and is never "
            "relay-accessible."
        ),
    )
    async def create_pairing_invitation(
        payload: PairingInvitationCreateRequest,
        request: Request,
        response: Response,
    ) -> PairingInvitationCreateResponse:
        """Create one invitation and return its QR payload exactly once."""

        await _require_direct_dashboard_authority(request, tailnet_peer_verifier)
        try:
            package = await run_in_threadpool(
                service.create_invitation,
                lifetime=timedelta(seconds=payload.valid_for_seconds),
                max_pairings=payload.max_pairings,
            )
            invitation = next(
                item
                for item in await run_in_threadpool(service.invitations)
                if item.invitation_id == package.invitation_id
            )
        except PairingPackageTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="pairing invitation is too large for a reliable QR code",
            ) from exc
        except (PairingGatewayNotInitializedError, ValueError) as exc:
            _raise_pairing_gateway_http_error(exc)
        except OperatorRelayUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the operator gateway identity is temporarily unavailable",
            ) from exc
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return PairingInvitationCreateResponse(
            invitation=invitation,
            pairing_code=package.as_url(),
        )

    @router.get(
        _PAIRING_INVITATION_PATH,
        response_model=list[PairingInvitationSummary],
        response_model_by_alias=True,
        summary="List dashboard pairing invitations",
        description=(
            "Return safe invitation status without bearer nonces or pairing "
            "codes. This management route is available only to the configured "
            "gateway dashboard over localhost or Tailscale and never through the "
            "relay gateway."
        ),
    )
    async def list_pairing_invitations(
        request: Request,
    ) -> list[PairingInvitationSummary]:
        """List safe status for invitations created on this gateway."""

        await _require_direct_dashboard_authority(request, tailnet_peer_verifier)
        try:
            return list(await run_in_threadpool(service.invitations))
        except PairingGatewayNotInitializedError as exc:
            _raise_pairing_gateway_http_error(exc)

    @router.delete(
        f"{_PAIRING_INVITATION_PATH}/{{invitation_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Revoke a dashboard pairing invitation",
        description=(
            "Immediately prevent new and unfinished pairing attempts for one "
            "invitation without revoking devices that already paired. This "
            "management route is available only to the configured gateway "
            "dashboard over localhost or Tailscale and is never relay-accessible."
        ),
    )
    async def revoke_pairing_invitation(
        invitation_id: UUID, request: Request
    ) -> Response:
        """Revoke one invitation from the trusted direct gateway dashboard."""

        await _require_direct_dashboard_authority(request, tailnet_peer_verifier)
        try:
            await run_in_threadpool(service.revoke_invitation, invitation_id)
        except PairingGatewayNotInitializedError as exc:
            _raise_pairing_gateway_http_error(exc)
        except PairingSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/pairing-sessions/challenge",
        response_model=PairingChallengeResponse,
        response_model_exclude_none=True,
        summary="Bind a device key to a local pairing session",
        description=(
            "Accept a candidate Ed25519 public key only when the nonce names an "
            "unexpired host-created pairing session or invitation, then return "
            "one random challenge for proof of possession. Reusable invitations "
            "return an independent five-minute attempt identity."
        ),
    )
    def create_pairing_challenge(
        request: PairingChallengeRequest,
    ) -> PairingChallengeResponse:
        """Create one device proof challenge for a pending pairing session."""

        try:
            return service.create_challenge(request)
        except PairingGatewayNotInitializedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PairingSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PairingSessionExpiredError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PairingInvitationCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except PairingProofError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post(
        "/pairing-sessions/exchange",
        response_model=PairingExchangeResponse,
        summary="Exchange a device-key proof for operator credentials",
        description=(
            "Verify the candidate device's Ed25519 signature, consume its "
            "single-use session or independent invitation attempt, and return "
            "short-lived access plus rotating refresh credentials exactly once."
        ),
    )
    def exchange_pairing_proof(
        request: PairingExchangeRequest,
    ) -> PairingExchangeResponse:
        """Verify a candidate device and consume its pairing capability."""

        try:
            return service.exchange(request)
        except PairingGatewayNotInitializedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PairingSessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PairingSessionExpiredError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PairingProofError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @router.post(
        "/token",
        response_model=OperatorTokenResponse,
        summary="Rotate an operator refresh credential",
        description=(
            "Accept the current opaque refresh credential for one paired "
            "device, invalidate its existing token pair, and return a fresh "
            "short-lived access token plus rotating refresh token exactly once."
        ),
    )
    def refresh_operator_token(request: OperatorTokenRequest) -> OperatorTokenResponse:
        """Rotate the access and refresh credentials for one paired device."""

        try:
            return service.refresh(request)
        except OperatorDeviceNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh credential is invalid",
                headers=_BEARER_CHALLENGE,
            ) from exc
        except (
            OperatorCredentialInvalidError,
            OperatorCredentialExpiredError,
            PairingGatewayNotInitializedError,
        ) as exc:
            _raise_credential_http_error(exc)
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get(
        "/devices",
        response_model=OperatorDevicesResponse,
        summary="List paired operator devices",
        description=(
            "Return safe active and revoked device projections for a bearer "
            "credential with device-management scope. Credential material is "
            "never included."
        ),
    )
    def list_operator_devices(
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperatorDevicesResponse:
        """List devices visible to an authorized operator."""

        try:
            return service.devices(_require_bearer(authorization))
        except (
            OperatorCredentialInvalidError,
            OperatorCredentialExpiredError,
            OperatorScopeError,
            PairingGatewayNotInitializedError,
        ) as exc:
            _raise_credential_http_error(exc)

    @router.delete(
        "/devices/{device_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Revoke a paired operator device",
        description=(
            "Immediately invalidate the target device's access and refresh "
            "credentials. Repeating revocation for an already revoked device "
            "is idempotent."
        ),
    )
    def revoke_operator_device(
        device_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Revoke one stable paired-device identity."""

        try:
            service.revoke_device(_require_bearer(authorization), device_id)
        except OperatorDeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            OperatorCredentialInvalidError,
            OperatorCredentialExpiredError,
            OperatorScopeError,
            PairingGatewayNotInitializedError,
        ) as exc:
            _raise_credential_http_error(exc)
        except PairingSessionStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
