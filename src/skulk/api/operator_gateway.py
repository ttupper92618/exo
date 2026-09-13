"""Authentication boundary for the relay-only canonical Skulk API listener."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, cast, final

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from skulk.operator.pairing import (
    OperatorCredentialExpiredError,
    OperatorCredentialInvalidError,
    OperatorPairingService,
    OperatorScope,
    OperatorScopeError,
    PairingGatewayNotInitializedError,
)
from skulk.operator.plugin_scopes import required_plugin_scope

_UNAUTHENTICATED_PATHS: Final = frozenset(
    {
        "/v1/auth/pairing-sessions/challenge",
        "/v1/auth/pairing-sessions/exchange",
        "/v1/auth/token",
    }
)
_DIRECT_DASHBOARD_ONLY_PREFIXES: Final = (
    "/v1/auth/pairing-invitations",
    "/v1/auth/plugin-grants",
)
_MODEL_PREFIXES: Final = ("/v1/models", "/models", "/model-store", "/v1/store")
_INFERENCE_PREFIXES: Final = (
    "/v1/chat",
    "/v1/responses",
    "/v1/embeddings",
    "/v1/audio",
    "/v1/images",
    "/v1/realtime",
    "/v1/fabric/chains",
    "/bench/",
)
OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY: Final = "skulk.operator_gateway_authorized"
"""ASGI scope marker set only after operator bearer validation succeeds."""


@final
class OperatorGatewayAuthorization:
    """Require scoped operator credentials on the relay-only ASGI listener.

    The wrapper serves the existing FastAPI application without adding an
    operator-specific model, inference, or command surface. Pairing challenge,
    exchange, and refresh remain reachable before access-token validation.
    """

    def __init__(self, app: ASGIApp, service: OperatorPairingService) -> None:
        """Wrap the canonical application with one pairing service."""

        self._app = app
        self._service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Validate the request before invoking the canonical application."""

        scope_type = cast(str, scope["type"])
        if scope_type not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        path = cast(str, scope.get("path", ""))
        if path.startswith(_DIRECT_DASHBOARD_ONLY_PREFIXES):
            await _deny(
                scope,
                receive,
                send,
                status_code=404,
                detail="route unavailable",
            )
            return
        if path in _UNAUTHENTICATED_PATHS:
            await self._app(scope, receive, send)
            return
        required_scopes = _required_scopes(scope)
        try:
            bearer = _bearer_from_scope(scope)
            self._service.validate_access_token(
                bearer,
                required_scopes=required_scopes,
            )
        except PairingGatewayNotInitializedError:
            await _deny(
                scope,
                receive,
                send,
                status_code=503,
                detail="operator gateway unavailable",
            )
            return
        except OperatorScopeError:
            await _deny(
                scope, receive, send, status_code=403, detail="operator scope denied"
            )
            return
        except (OperatorCredentialInvalidError, OperatorCredentialExpiredError):
            await _deny(
                scope,
                receive,
                send,
                status_code=401,
                detail="operator credential invalid",
            )
            return
        scope[OPERATOR_GATEWAY_AUTHORIZED_SCOPE_KEY] = True
        await self._app(scope, receive, send)


def _required_scopes(scope: Scope) -> Sequence[OperatorScope]:
    """Map canonical routes onto the smallest existing operator scope."""

    path = cast(str, scope.get("path", ""))
    plugin_scope = required_plugin_scope(cast(str, scope.get("method", "GET")), path)
    if plugin_scope is not None:
        return (plugin_scope,)
    if path.startswith("/v1/auth/devices"):
        return ("devices:manage",)
    if path.startswith(_INFERENCE_PREFIXES):
        return ("chat:write",)
    if path.startswith(_MODEL_PREFIXES):
        method = cast(str, scope.get("method", "GET")).upper()
        return (
            ("models:read",)
            if method in {"GET", "HEAD", "OPTIONS"}
            else ("operations:write",)
        )
    method = cast(str, scope.get("method", "GET")).upper()
    if scope["type"] == "http" and method in {"GET", "HEAD", "OPTIONS"}:
        return ("cluster:read",)
    return ("operations:write",)


def _bearer_from_scope(scope: Scope) -> str:
    """Extract one exact bearer from raw ASGI headers."""

    raw_authorization: bytes | None = None
    headers = cast(Sequence[tuple[bytes, bytes]], scope.get("headers", ()))
    for name, value in headers:
        if name.lower() == b"authorization":
            if raw_authorization is not None:
                raise OperatorCredentialInvalidError(
                    "multiple authorization headers are invalid"
                )
            raw_authorization = value
    if raw_authorization is None:
        raise OperatorCredentialInvalidError("operator bearer is required")
    try:
        authorization = raw_authorization.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OperatorCredentialInvalidError("operator bearer is invalid") from exc
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise OperatorCredentialInvalidError("operator bearer is invalid")
    return token.strip()


async def _deny(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    detail: str,
) -> None:
    """Return a payload-safe denial for HTTP or WebSocket requests."""

    if scope["type"] == "websocket":
        message: Message = {
            "type": "websocket.close",
            "code": 1008,
            "reason": detail,
        }
        await send(message)
        return
    response = JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )
    await response(scope, receive, send)
