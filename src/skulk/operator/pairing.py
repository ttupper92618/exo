"""Host-authorized pairing for one designated Skulk gateway."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from ipaddress import ip_address
from typing import Literal, cast, final
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AnyHttpUrl, Field, ValidationInfo, field_validator, model_validator

from skulk.operator.authority import (
    AuthorityCommitConflictError,
    AuthorityNotInitializedError,
    EncryptedAuthorityStore,
)
from skulk.operator.identity import ClusterPublicIdentity, create_cluster_identity
from skulk.operator.key_provider import LocalFileAuthorityKeyProvider
from skulk.operator.plugin_scopes import PLUGIN_SCOPES, PluginScope
from skulk.operator.relay import (
    OperatorRelayConfiguration,
    OperatorRelayConfigurationRepository,
    OperatorRelayProvisioning,
    OperatorRemoteAccessMaterial,
)
from skulk.utils.pydantic_ext import FrozenModel

_PAIRING_RECORD_TYPE = "operator_pairing_session"
_PAIRING_INVITATION_RECORD_TYPE = "operator_pairing_invitation"
_PAIRING_ATTEMPT_RECORD_TYPE = "operator_pairing_attempt"
_PAIRING_LIFETIME = timedelta(minutes=5)
_MAXIMUM_INVITATION_LIFETIME = timedelta(days=90)
_DEFAULT_INVITATION_PAIRINGS = 10
_MAXIMUM_INVITATION_PAIRINGS = 20
_MAXIMUM_ACTIVE_INVITATION_ATTEMPTS = 10
_MAXIMUM_TOTAL_INVITATION_ATTEMPTS = 100
_AUTHORITY_RETRY_LIMIT = 3
_ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
_REFRESH_TOKEN_LIFETIME = timedelta(days=30)
_PAIRING_SIGNATURE_CONTEXT = b"skulk-device-pairing-v1\x00"
_PAIRING_INVITATION_SIGNATURE_CONTEXT = b"skulk-device-pairing-v2\x00"
_MAXIMUM_RELAY_PAIRING_URL_BYTES = 1_170

type OperatorScope = Literal[
    "cluster:read",
    "models:read",
    "chat:write",
    "operations:write",
    "devices:manage",
    "plugins:read",
    "plugins:manage",
    "plugins:approve",
]

_DEFAULT_SCOPES: tuple[OperatorScope, ...] = (
    "cluster:read",
    "models:read",
    "chat:write",
    "operations:write",
    "devices:manage",
)


class PairingError(RuntimeError):
    """Base class for safe pairing failures."""


class PairingSessionNotFoundError(PairingError):
    """Raised when a pairing nonce does not name a pending session."""


class PairingSessionExpiredError(PairingError):
    """Raised when a pairing session has passed its five-minute lifetime."""


class PairingSessionStateError(PairingError):
    """Raised when a pairing transition is repeated or out of order."""


class PairingInvitationCapacityError(PairingError):
    """Raised when an invitation cannot issue another bounded attempt."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        """Create a safe capacity failure with a bounded retry hint."""

        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PairingProofError(PairingError):
    """Raised when a device cannot prove possession of its proposed key."""


class PairingGatewayNotInitializedError(PairingError):
    """Raised when this API node has not been designated through local pairing."""


class PairingPackageTooLargeError(PairingError):
    """Raised before persisting relay pairing material that will not scan reliably."""


class OperatorCredentialError(RuntimeError):
    """Base class for safe operator credential failures."""


class OperatorCredentialInvalidError(OperatorCredentialError):
    """Raised when an access or refresh credential is unknown or revoked."""


class OperatorCredentialExpiredError(OperatorCredentialError):
    """Raised when an otherwise valid operator credential has expired."""


class OperatorScopeError(OperatorCredentialError):
    """Raised when a credential lacks a required canonical API scope."""


class OperatorDeviceNotFoundError(OperatorCredentialError):
    """Raised when a stable paired-device identity is unknown."""


class PairingPackage(FrozenModel):
    """Short-lived public package encoded in a Skulk pairing QR code."""

    version: Literal[1, 2] = Field(description="Pairing package format version.")
    cluster_id: UUID = Field(description="Stable identity of the target cluster.")
    cluster_name: str = Field(description="Operator-visible cluster name.")
    cluster_fingerprint: str = Field(
        description="Display fingerprint of the cluster identity key."
    )
    exchange_url: AnyHttpUrl = Field(
        description="Direct or relayed URL serving the pairing exchange."
    )
    expires_at: datetime = Field(description="UTC expiry of the pairing package.")
    nonce: str = Field(
        min_length=32,
        max_length=128,
        description="High-entropy single-use pairing capability.",
    )
    remote_access: OperatorRemoteAccessMaterial | None = Field(
        default=None,
        description=(
            "Relay and pinned inner-TLS material used only to reach the "
            "pairing routes when version is two."
        ),
    )

    @field_validator("expires_at")
    @classmethod
    def _expiry_is_timezone_aware(cls, value: datetime) -> datetime:
        """Reject ambiguous package expiries."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @field_validator("exchange_url")
    @classmethod
    def _exchange_url_protects_remote_credentials(
        cls,
        value: AnyHttpUrl,
    ) -> AnyHttpUrl:
        """Require HTTPS except for explicitly local development URLs."""

        return _validate_exchange_url(value)

    @model_validator(mode="after")
    def _version_matches_transport(self) -> "PairingPackage":
        """Keep legacy direct packages distinct from relay bootstrap packages."""

        if (self.version == 1) != (self.remote_access is None):
            raise ValueError("pairing package version does not match remote access")
        return self

    def as_url(self) -> str:
        """Return the package as a compact custom-scheme QR payload."""

        if self.version == 2:
            remote_access = cast(OperatorRemoteAccessMaterial, self.remote_access)
            compact_payload = {
                "v": self.version,
                "i": str(self.cluster_id),
                "n": self.cluster_name,
                "f": self.cluster_fingerprint,
                "u": str(self.exchange_url),
                "e": self.expires_at.isoformat(),
                "o": self.nonce,
                "r": {
                    "t": remote_access.transport,
                    "w": remote_access.app_websocket_url,
                    "l": remote_access.routing_locator,
                    "c": remote_access.app_carrier_credential,
                    "s": remote_access.gateway_server_name,
                    "p": remote_access.gateway_ca_certificate_pem,
                },
            }
            compressed = zlib.compress(
                json.dumps(
                    compact_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
                level=9,
            )
            return f"skulk://pair?z={_base64url_encode(compressed)}"

        encoded = _base64url_encode(
            self.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        )
        return f"skulk://pair?payload={encoded}"


class PairingInvitationPackage(FrozenModel):
    """Reusable public invitation encoded in a Skulk pairing QR code."""

    version: Literal[3] = Field(
        default=3,
        description="Pairing invitation format version.",
    )
    cluster_id: UUID = Field(description="Stable identity of the target cluster.")
    cluster_name: str = Field(description="Operator-visible cluster name.")
    cluster_fingerprint: str = Field(
        description="Display fingerprint of the cluster identity key."
    )
    exchange_url: AnyHttpUrl = Field(
        description="Direct or relayed URL serving the pairing exchange."
    )
    invitation_id: UUID = Field(
        description="Public identifier used to manage the invitation."
    )
    issued_at: datetime = Field(description="UTC creation time of the invitation.")
    expires_at: datetime = Field(description="UTC expiry of the invitation.")
    max_pairings: int = Field(
        ge=1,
        le=_MAXIMUM_INVITATION_PAIRINGS,
        description="Maximum successful device pairings allowed by the invitation.",
    )
    nonce: str = Field(
        min_length=32,
        max_length=128,
        description="High-entropy bearer capability for the invitation.",
    )
    remote_access: OperatorRemoteAccessMaterial | None = Field(
        default=None,
        description="Optional relay and pinned inner-TLS connection material.",
    )

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _timestamps_are_timezone_aware(cls, value: datetime) -> datetime:
        """Reject ambiguous invitation timestamps."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pairing invitation timestamps must include a timezone")
        return value

    @field_validator("exchange_url")
    @classmethod
    def _exchange_url_protects_remote_credentials(
        cls,
        value: AnyHttpUrl,
    ) -> AnyHttpUrl:
        """Require HTTPS except for explicitly local development URLs."""

        return _validate_exchange_url(value)

    @model_validator(mode="after")
    def _lifetime_is_bounded(self) -> "PairingInvitationPackage":
        """Keep invitation duration positive and bounded by product policy."""

        lifetime = self.expires_at - self.issued_at
        if lifetime < timedelta(minutes=1) or lifetime > _MAXIMUM_INVITATION_LIFETIME:
            raise ValueError(
                "pairing invitation lifetime must be from one minute to 90 days"
            )
        return self

    def as_url(self) -> str:
        """Return the invitation as one compact custom-scheme QR payload."""

        compact_payload: dict[str, object] = {
            "v": self.version,
            "i": str(self.cluster_id),
            "n": self.cluster_name,
            "f": self.cluster_fingerprint,
            "u": str(self.exchange_url),
            "d": str(self.invitation_id),
            "a": self.issued_at.isoformat(),
            "e": self.expires_at.isoformat(),
            "m": self.max_pairings,
            "o": self.nonce,
        }
        if self.remote_access is not None:
            compact_payload["r"] = {
                "t": self.remote_access.transport,
                "w": self.remote_access.app_websocket_url,
                "l": self.remote_access.routing_locator,
                "c": self.remote_access.app_carrier_credential,
                "s": self.remote_access.gateway_server_name,
                "p": self.remote_access.gateway_ca_certificate_pem,
            }
        compressed = zlib.compress(
            json.dumps(
                compact_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            level=9,
        )
        return f"skulk://pair?z={_base64url_encode(compressed)}"


class PairingChallengeRequest(FrozenModel):
    """Candidate device identity submitted before credential exchange."""

    nonce: str = Field(
        min_length=32,
        max_length=128,
        description="High-entropy bearer capability from the pairing QR.",
    )
    device_name: str = Field(
        min_length=1,
        max_length=80,
        description="Operator-visible name of the candidate device.",
    )
    device_public_key: str = Field(
        min_length=40,
        max_length=64,
        description="Unpadded URL-safe base64 Ed25519 device public key.",
    )
    invitation_id: UUID | None = Field(
        default=None,
        description="Reusable invitation identity, absent for legacy single-use pairing.",
    )

    @field_validator("device_name")
    @classmethod
    def _normalize_device_name(cls, value: str) -> str:
        """Normalize device names while rejecting whitespace-only input."""

        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("device_name must not be blank")
        return normalized

    @field_validator("invitation_id", mode="before")
    @classmethod
    def _parse_wire_invitation_id(cls, value: object) -> object:
        """Convert the optional JSON invitation UUID before strict validation."""

        return _parse_optional_wire_uuid(value, "invitation_id")


class PairingChallengeResponse(FrozenModel):
    """Cluster challenge that the candidate device must sign."""

    challenge: str = Field(description="Unpadded URL-safe base64 random challenge.")
    expires_at: datetime = Field(description="UTC expiry of this pairing attempt.")
    attempt_id: UUID | None = Field(
        default=None,
        description="Independent reusable-invitation attempt, absent for legacy pairing.",
    )


class PairingExchangeRequest(FrozenModel):
    """Device proof completing a pending pairing session."""

    nonce: str = Field(
        min_length=32,
        max_length=128,
        description="High-entropy bearer capability from the pairing QR.",
    )
    signature: str = Field(
        min_length=80,
        max_length=100,
        description="Ed25519 signature over the domain-separated pairing challenge.",
    )
    invitation_id: UUID | None = Field(
        default=None,
        description="Reusable invitation identity, absent for legacy pairing.",
    )
    attempt_id: UUID | None = Field(
        default=None,
        description="Challenge attempt identity, absent for legacy pairing.",
    )

    @model_validator(mode="after")
    def _invitation_fields_are_complete(self) -> "PairingExchangeRequest":
        """Reject partially specified invitation exchanges."""

        if (self.invitation_id is None) != (self.attempt_id is None):
            raise ValueError("invitation_id and attempt_id must be supplied together")
        return self

    @field_validator("invitation_id", "attempt_id", mode="before")
    @classmethod
    def _parse_wire_ids(cls, value: object, info: ValidationInfo) -> object:
        """Convert optional JSON UUIDs before strict validation."""

        return _parse_optional_wire_uuid(
            value,
            info.field_name or "pairing identifier",
        )


class PairingExchangeResponse(FrozenModel):
    """One-time credential response returned after successful pairing."""

    device_id: UUID = Field(description="Stable identifier assigned to the device.")
    cluster: ClusterPublicIdentity = Field(
        description="Validated cluster identity bound to the credential."
    )
    access_token: str = Field(
        description="Opaque short-lived bearer credential returned only once."
    )
    access_token_expires_at: datetime = Field(
        description="UTC expiry of the access credential."
    )
    refresh_token: str = Field(
        description="Opaque rotating refresh credential returned only once."
    )
    refresh_token_expires_at: datetime = Field(
        description="UTC expiry of the refresh credential."
    )
    scopes: tuple[OperatorScope, ...] = Field(
        description="Canonical API scopes granted to the paired device."
    )
    remote_access: OperatorRemoteAccessMaterial | None = Field(
        default=None,
        description=(
            "One-time relay and pinned inner-TLS material when this gateway "
            "has remote access configured."
        ),
    )


class OperatorTokenRequest(FrozenModel):
    """Rotating refresh credential presented for a new token pair."""

    device_id: UUID = Field(description="Stable paired-device identity.")
    refresh_token: str = Field(
        min_length=40,
        max_length=128,
        description="Current opaque refresh credential returned only once.",
    )

    @field_validator("device_id", mode="before")
    @classmethod
    def _parse_wire_device_id(cls, value: object) -> object:
        """Convert the JSON UUID representation before strict validation."""

        if not isinstance(value, str):
            return value
        try:
            return UUID(value)
        except ValueError as exc:
            raise ValueError("device_id must be a UUID") from exc


class OperatorTokenResponse(FrozenModel):
    """Fresh rotating access and refresh credentials."""

    access_token: str = Field(description="New short-lived bearer credential.")
    access_token_expires_at: datetime = Field(
        description="UTC expiry of the new access credential."
    )
    refresh_token: str = Field(description="New rotating refresh credential.")
    refresh_token_expires_at: datetime = Field(
        description="UTC expiry of the new refresh credential."
    )
    scopes: tuple[OperatorScope, ...] = Field(
        description="Canonical API scopes retained by the device."
    )


class OperatorAccessContext(FrozenModel):
    """Validated device identity and scopes for one bearer request."""

    device_id: UUID = Field(description="Stable paired-device identity.")
    device_name: str = Field(description="Operator-visible device name.")
    scopes: tuple[OperatorScope, ...] = Field(
        description="Canonical API scopes authorized for the request."
    )


class OperatorDevice(FrozenModel):
    """Safe operator-visible projection of one paired device."""

    device_id: UUID = Field(description="Stable paired-device identity.")
    name: str = Field(description="Operator-visible device name.")
    paired_at: datetime = Field(description="UTC pairing-session creation time.")
    refresh_expires_at: datetime | None = Field(
        description="UTC refresh expiry, absent after revocation."
    )
    state: Literal["active", "revoked"] = Field(
        description="Whether credentials can still authorize the device."
    )
    current: bool = Field(
        description="Whether this row represents the requesting device."
    )


class OperatorDevicesResponse(FrozenModel):
    """Paired devices visible to an authorized operator."""

    devices: tuple[OperatorDevice, ...] = Field(
        description="Stable paired-device projections in pairing order."
    )


class PluginGrant(FrozenModel):
    """Owner-visible explicit plugin privileges for one paired device."""

    device_id: UUID = Field(description="Stable paired operator identity.")
    device_name: str = Field(description="Owner-visible paired device name.")
    revision: int = Field(ge=0, description="Revision fencing owner grant edits.")
    scopes: tuple[PluginScope, ...] = Field(
        description="Explicit privileges; empty for existing and newly paired devices."
    )
    active: bool = Field(description="False when the device credential is revoked.")


class PluginGrantUpdate(FrozenModel):
    """Replace only plugin privileges after an owner observes their revision."""

    expected_revision: int = Field(ge=0, description="Last observed grant revision.")
    scopes: tuple[PluginScope, ...] = Field(
        max_length=3, description="Exact desired privileges; empty revokes all."
    )

    @field_validator("scopes", mode="before")
    @classmethod
    def _wire_scope_array(cls, value: object) -> object:
        """Accept a JSON array while retaining strict validation of each scope."""
        return tuple(cast(list[object], value)) if isinstance(value, list) else value

    @field_validator("scopes")
    @classmethod
    def _unique_scopes(cls, value: tuple[PluginScope, ...]) -> tuple[PluginScope, ...]:
        """Reject duplicate privileges and persist a canonical order."""
        if len(set(value)) != len(value):
            raise ValueError("plugin scopes must be unique")
        return tuple(scope for scope in PLUGIN_SCOPES if scope in value)


class _StoredPairingSession(FrozenModel):
    """Encrypted durable state for one pairing capability and resulting device."""

    state: Literal["pending", "challenged", "consumed", "revoked"]
    nonce_hash: str
    created_at: datetime
    expires_at: datetime
    exchange_url: str
    device_name: str | None = None
    device_public_key: str | None = None
    challenge: str | None = None
    device_id: UUID | None = None
    access_token_hash: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_hash: str | None = None
    refresh_token_expires_at: datetime | None = None
    scopes: tuple[OperatorScope, ...] = ()
    plugin_grant_revision: int = 0


class _StoredPairingInvitation(FrozenModel):
    """Encrypted durable state for one reusable host invitation."""

    state: Literal["active", "revoked"]
    invitation_id: UUID
    nonce_hash: str
    created_at: datetime
    expires_at: datetime
    exchange_url: str
    max_pairings: int = Field(ge=1, le=_MAXIMUM_INVITATION_PAIRINGS)


class _StoredPairingAttempt(FrozenModel):
    """Encrypted durable state for one invitation challenge and device."""

    state: Literal["challenged", "consumed", "revoked"]
    invitation_id: UUID
    attempt_id: UUID
    created_at: datetime
    expires_at: datetime
    device_name: str
    device_public_key: str
    challenge: str
    device_id: UUID | None = None
    access_token_hash: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_hash: str | None = None
    refresh_token_expires_at: datetime | None = None
    scopes: tuple[OperatorScope, ...] = ()
    plugin_grant_revision: int = 0


type _StoredDeviceCredential = _StoredPairingSession | _StoredPairingAttempt


type PairingInvitationState = Literal[
    "active",
    "expired",
    "exhausted",
    "attempt-limit",
    "revoked",
]


class PairingInvitationSummary(FrozenModel):
    """Safe host-visible status for one reusable pairing invitation."""

    invitation_id: UUID = Field(description="Public invitation identifier.")
    created_at: datetime = Field(description="UTC invitation creation time.")
    expires_at: datetime = Field(description="UTC invitation expiry.")
    successful_pairings: int = Field(
        ge=0,
        description="Number of devices that received credentials.",
    )
    max_pairings: int = Field(
        ge=1,
        le=_MAXIMUM_INVITATION_PAIRINGS,
        description="Maximum successful pairings allowed.",
    )
    active_attempts: int = Field(
        ge=0,
        description="Unexpired attempts currently awaiting proof.",
    )
    total_attempts: int = Field(
        ge=0,
        description="All attempts issued during the invitation lifetime.",
    )
    state: PairingInvitationState = Field(description="Current invitation state.")


class PairingInvitationCreateRequest(FrozenModel):
    """Bounded dashboard request for one reusable pairing invitation."""

    valid_for_seconds: int = Field(
        ge=60,
        le=int(_MAXIMUM_INVITATION_LIFETIME.total_seconds()),
        description="Invitation lifetime in whole seconds, from one minute to 90 days.",
    )
    max_pairings: int = Field(
        ge=1,
        le=_MAXIMUM_INVITATION_PAIRINGS,
        description="Maximum successful device pairings allowed, from one through 20.",
    )


class PairingInvitationCreateResponse(FrozenModel):
    """One-time dashboard response containing a protected pairing QR payload."""

    invitation: PairingInvitationSummary = Field(
        description="Safe status for the newly created invitation."
    )
    pairing_code: str = Field(
        min_length=1,
        max_length=_MAXIMUM_RELAY_PAIRING_URL_BYTES,
        description=(
            "Secret skulk:// bearer invitation returned once for immediate QR rendering."
        ),
    )


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(tz=timezone.utc)


def _base64url_encode(value: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    """Decode strict unpadded URL-safe base64."""

    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        f"{value}{padding}",
        altchars=b"-_",
        validate=True,
    )


def _opaque_digest(value: str) -> str:
    """Return a non-reversible identifier for a bearer value."""

    return _base64url_encode(hashlib.sha256(value.encode("utf-8")).digest())


def _parse_optional_wire_uuid(value: object, field_name: str) -> object:
    """Convert one optional JSON UUID representation for strict models."""

    if value is None or isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return value
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _validate_exchange_url(value: str | AnyHttpUrl) -> AnyHttpUrl:
    """Validate that a pairing exchange cannot expose credentials in cleartext."""

    url = value if isinstance(value, AnyHttpUrl) else AnyHttpUrl(value)
    if url.scheme == "https":
        return url
    if url.host is None:
        raise ValueError("pairing exchange_url must include a host")
    host = url.host.strip("[]")
    if host == "localhost":
        return url
    try:
        if ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise ValueError("remote pairing exchange_url must use HTTPS")


def pairing_signature_message(
    *,
    cluster_id: UUID,
    nonce: str,
    challenge: str,
) -> bytes:
    """Build the exact message a candidate device signs during pairing.

    Args:
        cluster_id: Target cluster identity from the QR package.
        nonce: Single-use pairing capability from the QR package.
        challenge: Random challenge returned by the gateway.

    Returns:
        Domain-separated bytes suitable for Ed25519 signing.
    """

    return (
        _PAIRING_SIGNATURE_CONTEXT
        + str(cluster_id).encode("ascii")
        + b"\x00"
        + nonce.encode("ascii")
        + b"\x00"
        + challenge.encode("ascii")
    )


def pairing_invitation_signature_message(
    *,
    cluster_id: UUID,
    invitation_id: UUID,
    nonce: str,
    attempt_id: UUID,
    challenge: str,
) -> bytes:
    """Build the proof message for one reusable-invitation attempt.

    Args:
        cluster_id: Target cluster identity from the invitation.
        invitation_id: Public identity of the reusable invitation.
        nonce: Bearer capability from the invitation QR.
        attempt_id: Independent attempt returned with the challenge.
        challenge: Random challenge returned by the gateway.

    Returns:
        Domain-separated bytes binding the proof to the exact attempt.
    """

    return (
        _PAIRING_INVITATION_SIGNATURE_CONTEXT
        + str(cluster_id).encode("ascii")
        + b"\x00"
        + str(invitation_id).encode("ascii")
        + b"\x00"
        + nonce.encode("ascii")
        + b"\x00"
        + str(attempt_id).encode("ascii")
        + b"\x00"
        + challenge.encode("ascii")
    )


@final
class OperatorPairingService:
    """Persist and validate pairing sessions on one designated gateway."""

    def __init__(
        self,
        store: EncryptedAuthorityStore,
        key_provider: LocalFileAuthorityKeyProvider,
        *,
        relay_repository: OperatorRelayConfigurationRepository | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Create a pairing service with injected persistence and time.

        Args:
            store: Encrypted local authority journal.
            key_provider: Local data-key provider used only during explicit
                gateway initialization.
            relay_repository: Encrypted designated-gateway relay repository.
            now: Time provider for deterministic expiry tests.
        """

        self._store = store
        self._key_provider = key_provider
        self._relay_repository = (
            relay_repository or OperatorRelayConfigurationRepository(store)
        )
        self._now = now

    @classmethod
    def from_default_paths(cls) -> "OperatorPairingService":
        """Create the production service for the designated gateway paths."""

        key_provider = LocalFileAuthorityKeyProvider()
        store = EncryptedAuthorityStore(key_provider)
        return cls(
            store,
            key_provider,
            relay_repository=OperatorRelayConfigurationRepository(store),
        )

    def initialize_gateway(
        self, cluster_name: str = "Cluster"
    ) -> ClusterPublicIdentity:
        """Load or create the single designated gateway identity.

        Args:
            cluster_name: Initial operator-visible name for a new gateway.

        Returns:
            Stable public cluster identity owned by this gateway.
        """

        if self._store.path.exists():
            self._key_provider.load_data_key(self._key_provider.active_key_id)
        else:
            self._key_provider.ensure_data_key()
        try:
            return self._store.cluster_identity()
        except AuthorityNotInitializedError:
            material = create_cluster_identity(cluster_name)
            self._store.initialize_cluster(
                material.public_identity,
                material.private_key,
            )
            return material.public_identity

    def configure_relay(
        self,
        provisioning: OperatorRelayProvisioning,
        *,
        operator_api_port: int,
        cluster_name: str = "Cluster",
    ) -> OperatorRelayConfiguration:
        """Persist one generated relay route for this designated gateway."""

        self.initialize_gateway(cluster_name)
        return self._relay_repository.configure(
            provisioning,
            operator_api_port=operator_api_port,
        )

    def relay_configuration(self) -> OperatorRelayConfiguration | None:
        """Return the configured designated-gateway route, when present."""

        return self._relay_repository.load()

    def create_session(
        self,
        *,
        exchange_url: str | None = None,
        cluster_name: str = "Cluster",
    ) -> PairingPackage:
        """Create one host-authorized five-minute pairing package.

        Args:
            exchange_url: Optional direct HTTPS base URL. When omitted, a
                configured relay supplies the inner gateway origin.
            cluster_name: Name used only when initializing a new gateway.

        Returns:
            Public package safe to render as a QR code.

        Raises:
            ValueError: No protected direct URL or configured relay is available.
        """

        relay_configuration = self._relay_repository.load()
        use_relay = exchange_url is None
        if exchange_url is None:
            if relay_configuration is None:
                raise ValueError(
                    "exchange_url is required until operator relay is configured"
                )
            exchange_url = f"https://{relay_configuration.gateway_server_name}"
        validated_exchange_url = _validate_exchange_url(exchange_url)
        identity = self.initialize_gateway(cluster_name)
        remote_access = (
            relay_configuration.device_material()
            if use_relay and relay_configuration is not None
            else None
        )

        now = self._now()
        nonce = secrets.token_urlsafe(32)
        nonce_hash = _opaque_digest(nonce)
        expires_at = now + _PAIRING_LIFETIME
        package = PairingPackage(
            version=2 if remote_access is not None else 1,
            cluster_id=UUID(str(identity.cluster_id)),
            cluster_name=identity.name,
            cluster_fingerprint=identity.fingerprint,
            exchange_url=validated_exchange_url,
            expires_at=expires_at,
            nonce=nonce,
            remote_access=remote_access,
        )
        if (
            package.version == 2
            and len(package.as_url().encode("utf-8")) > _MAXIMUM_RELAY_PAIRING_URL_BYTES
        ):
            raise PairingPackageTooLargeError(
                "relay pairing package exceeds the supported QR size"
            )
        session = _StoredPairingSession(
            state="pending",
            nonce_hash=nonce_hash,
            created_at=now,
            expires_at=expires_at,
            exchange_url=str(package.exchange_url),
        )
        self._append_session(nonce_hash, session)
        return package

    def create_invitation(
        self,
        *,
        lifetime: timedelta,
        max_pairings: int = _DEFAULT_INVITATION_PAIRINGS,
        exchange_url: str | None = None,
        cluster_name: str = "Cluster",
    ) -> PairingInvitationPackage:
        """Create one reusable, revocable host pairing invitation.

        Args:
            lifetime: Positive invitation lifetime no longer than 90 days.
            max_pairings: Successful device-pairing limit from one through 20.
            exchange_url: Optional direct HTTPS base URL. When omitted, a
                configured relay supplies the inner gateway origin.
            cluster_name: Name used only when initializing a new gateway.

        Returns:
            Version-three invitation safe to render as a QR code.

        Raises:
            ValueError: Policy bounds or protected reachability are invalid.
            PairingPackageTooLargeError: The resulting QR would be unreliable.
        """

        if lifetime < timedelta(minutes=1) or lifetime > _MAXIMUM_INVITATION_LIFETIME:
            raise ValueError(
                "pairing invitation lifetime must be from one minute to 90 days"
            )
        if not 1 <= max_pairings <= _MAXIMUM_INVITATION_PAIRINGS:
            raise ValueError(
                "pairing invitation max_pairings must be from one through 20"
            )

        relay_configuration = self._relay_repository.load()
        use_relay = exchange_url is None
        if exchange_url is None:
            if relay_configuration is None:
                raise ValueError(
                    "exchange_url is required until operator relay is configured"
                )
            exchange_url = f"https://{relay_configuration.gateway_server_name}"
        validated_exchange_url = _validate_exchange_url(exchange_url)
        identity = self.initialize_gateway(cluster_name)
        remote_access = (
            relay_configuration.device_material()
            if use_relay and relay_configuration is not None
            else None
        )

        now = self._now()
        invitation_id = uuid4()
        nonce = secrets.token_urlsafe(32)
        package = PairingInvitationPackage(
            cluster_id=UUID(str(identity.cluster_id)),
            cluster_name=identity.name,
            cluster_fingerprint=identity.fingerprint,
            exchange_url=validated_exchange_url,
            invitation_id=invitation_id,
            issued_at=now,
            expires_at=now + lifetime,
            max_pairings=max_pairings,
            nonce=nonce,
            remote_access=remote_access,
        )
        if len(package.as_url().encode("utf-8")) > _MAXIMUM_RELAY_PAIRING_URL_BYTES:
            raise PairingPackageTooLargeError(
                "pairing invitation package exceeds the supported QR size"
            )
        invitation = _StoredPairingInvitation(
            state="active",
            invitation_id=invitation_id,
            nonce_hash=_opaque_digest(nonce),
            created_at=now,
            expires_at=package.expires_at,
            exchange_url=str(package.exchange_url),
            max_pairings=max_pairings,
        )
        self._append_authority_record(
            record_type=_PAIRING_INVITATION_RECORD_TYPE,
            record_id=str(invitation_id),
            payload=invitation,
        )
        return package

    def create_challenge(
        self,
        request: PairingChallengeRequest,
    ) -> PairingChallengeResponse:
        """Bind a candidate device key and issue one random challenge.

        Args:
            request: Pairing capability, candidate device name, and public key.

        Returns:
            Challenge that expires with the pairing session.

        Raises:
            PairingSessionNotFoundError: The nonce is unknown.
            PairingSessionExpiredError: The session expired.
            PairingSessionStateError: A challenge was already issued or used.
            PairingProofError: The proposed public key is malformed.
        """

        if request.invitation_id is not None:
            return self._create_invitation_challenge(request)
        self._decode_device_public_key(request.device_public_key)
        nonce_hash = _opaque_digest(request.nonce)
        session, session_commit_index = self._load_session(nonce_hash)
        self._require_pending(session)
        challenge = _base64url_encode(secrets.token_bytes(32))
        updated = session.model_copy(
            update={
                "state": "challenged",
                "device_name": request.device_name,
                "device_public_key": request.device_public_key,
                "challenge": challenge,
            }
        )
        self._append_session(
            nonce_hash,
            updated,
            expected_record_commit_index=session_commit_index,
        )
        return PairingChallengeResponse(
            challenge=challenge,
            expires_at=session.expires_at,
        )

    def exchange(
        self,
        request: PairingExchangeRequest,
    ) -> PairingExchangeResponse:
        """Verify device-key possession and consume one pairing session.

        Args:
            request: Pairing capability and Ed25519 proof over the issued challenge.

        Returns:
            One-time access and refresh credentials.

        Raises:
            PairingProofError: Signature validation fails.
            PairingSessionStateError: The session is not awaiting exchange.
            PairingSessionExpiredError: The session expired.
        """

        if request.invitation_id is not None and request.attempt_id is not None:
            return self._exchange_invitation_proof(request)
        nonce_hash = _opaque_digest(request.nonce)
        session, session_commit_index = self._load_session(nonce_hash)
        self._require_unexpired(session)
        if (
            session.state != "challenged"
            or session.device_public_key is None
            or session.challenge is None
            or session.device_name is None
        ):
            raise PairingSessionStateError(
                "pairing session is not awaiting device proof"
            )
        public_key = self._decode_device_public_key(session.device_public_key)
        try:
            signature = _base64url_decode(request.signature)
            public_key.verify(
                signature,
                pairing_signature_message(
                    cluster_id=UUID(str(self._store.cluster_identity().cluster_id)),
                    nonce=request.nonce,
                    challenge=session.challenge,
                ),
            )
        except (InvalidSignature, ValueError, UnicodeError) as exc:
            raise PairingProofError("device pairing proof is invalid") from exc

        relay_configuration = self._relay_repository.load()
        remote_access = (
            relay_configuration.device_material()
            if relay_configuration is not None
            else None
        )

        now = self._now()
        device_id = uuid4()
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        access_expires_at = now + _ACCESS_TOKEN_LIFETIME
        refresh_expires_at = now + _REFRESH_TOKEN_LIFETIME
        consumed = session.model_copy(
            update={
                "state": "consumed",
                "device_id": device_id,
                "access_token_hash": _opaque_digest(access_token),
                "access_token_expires_at": access_expires_at,
                "refresh_token_hash": _opaque_digest(refresh_token),
                "refresh_token_expires_at": refresh_expires_at,
                "scopes": _DEFAULT_SCOPES,
            }
        )
        self._append_session(
            nonce_hash,
            consumed,
            expected_record_commit_index=session_commit_index,
        )
        return PairingExchangeResponse(
            device_id=device_id,
            cluster=self._store.cluster_identity(),
            access_token=access_token,
            access_token_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires_at,
            scopes=_DEFAULT_SCOPES,
            remote_access=remote_access,
        )

    def _create_invitation_challenge(
        self,
        request: PairingChallengeRequest,
    ) -> PairingChallengeResponse:
        """Create an independent five-minute attempt under an invitation."""

        self._decode_device_public_key(request.device_public_key)
        invitation_id = request.invitation_id
        if invitation_id is None:
            raise PairingSessionStateError("pairing invitation identity is required")
        for _ in range(_AUTHORITY_RETRY_LIMIT):
            invitation, _, attempts, expected_commit_index = self._invitation_snapshot(
                invitation_id
            )
            self._require_invitation_available(
                invitation,
                nonce=request.nonce,
                attempts=attempts,
                creating_attempt=True,
            )
            now = self._now()
            active_attempts = tuple(
                attempt
                for _, attempt, _ in attempts
                if attempt.state == "challenged" and now < attempt.expires_at
            )
            if len(active_attempts) >= _MAXIMUM_ACTIVE_INVITATION_ATTEMPTS:
                retry_at = min(attempt.expires_at for attempt in active_attempts)
                retry_seconds = max(1, int((retry_at - now).total_seconds()))
                raise PairingInvitationCapacityError(
                    "pairing invitation has too many active attempts",
                    retry_after_seconds=retry_seconds,
                )

            attempt_id = uuid4()
            challenge = _base64url_encode(secrets.token_bytes(32))
            expires_at = min(now + _PAIRING_LIFETIME, invitation.expires_at)
            attempt = _StoredPairingAttempt(
                state="challenged",
                invitation_id=invitation_id,
                attempt_id=attempt_id,
                created_at=now,
                expires_at=expires_at,
                device_name=request.device_name,
                device_public_key=request.device_public_key,
                challenge=challenge,
            )
            try:
                self._append_authority_record(
                    record_type=_PAIRING_ATTEMPT_RECORD_TYPE,
                    record_id=str(attempt_id),
                    payload=attempt,
                    expected_commit_index=expected_commit_index,
                )
            except AuthorityCommitConflictError:
                continue
            return PairingChallengeResponse(
                challenge=challenge,
                expires_at=expires_at,
                attempt_id=attempt_id,
            )
        raise PairingSessionStateError(
            "pairing state changed concurrently; restart pairing"
        )

    def _exchange_invitation_proof(
        self,
        request: PairingExchangeRequest,
    ) -> PairingExchangeResponse:
        """Verify and atomically consume one invitation attempt."""

        invitation_id = request.invitation_id
        attempt_id = request.attempt_id
        if invitation_id is None or attempt_id is None:
            raise PairingSessionStateError("pairing invitation attempt is required")
        for _ in range(_AUTHORITY_RETRY_LIMIT):
            invitation, _, attempts, expected_commit_index = self._invitation_snapshot(
                invitation_id
            )
            self._require_invitation_available(
                invitation,
                nonce=request.nonce,
                attempts=attempts,
                creating_attempt=False,
            )
            matched = next(
                (
                    (record_id, attempt, commit_index)
                    for record_id, attempt, commit_index in attempts
                    if attempt.attempt_id == attempt_id
                ),
                None,
            )
            if matched is None:
                raise PairingSessionNotFoundError("pairing attempt was not found")
            record_id, attempt, attempt_commit_index = matched
            if self._now() >= attempt.expires_at:
                raise PairingSessionExpiredError("pairing attempt expired")
            if attempt.state != "challenged":
                raise PairingSessionStateError("pairing attempt was already used")

            public_key = self._decode_device_public_key(attempt.device_public_key)
            try:
                signature = _base64url_decode(request.signature)
                public_key.verify(
                    signature,
                    pairing_invitation_signature_message(
                        cluster_id=UUID(str(self._store.cluster_identity().cluster_id)),
                        invitation_id=invitation_id,
                        nonce=request.nonce,
                        attempt_id=attempt_id,
                        challenge=attempt.challenge,
                    ),
                )
            except (InvalidSignature, ValueError, UnicodeError) as exc:
                raise PairingProofError("device pairing proof is invalid") from exc

            now = self._now()
            device_id = uuid4()
            access_token = secrets.token_urlsafe(32)
            refresh_token = secrets.token_urlsafe(48)
            access_expires_at = now + _ACCESS_TOKEN_LIFETIME
            refresh_expires_at = now + _REFRESH_TOKEN_LIFETIME
            consumed = attempt.model_copy(
                update={
                    "state": "consumed",
                    "device_id": device_id,
                    "access_token_hash": _opaque_digest(access_token),
                    "access_token_expires_at": access_expires_at,
                    "refresh_token_hash": _opaque_digest(refresh_token),
                    "refresh_token_expires_at": refresh_expires_at,
                    "scopes": _DEFAULT_SCOPES,
                }
            )
            try:
                self._append_authority_record(
                    record_type=_PAIRING_ATTEMPT_RECORD_TYPE,
                    record_id=record_id,
                    payload=consumed,
                    expected_commit_index=expected_commit_index,
                    expected_record_commit_index=attempt_commit_index,
                )
            except AuthorityCommitConflictError:
                continue

            relay_configuration = self._relay_repository.load()
            return PairingExchangeResponse(
                device_id=device_id,
                cluster=self._store.cluster_identity(),
                access_token=access_token,
                access_token_expires_at=access_expires_at,
                refresh_token=refresh_token,
                refresh_token_expires_at=refresh_expires_at,
                scopes=_DEFAULT_SCOPES,
                remote_access=(
                    relay_configuration.device_material()
                    if relay_configuration is not None
                    else None
                ),
            )
        raise PairingSessionStateError(
            "pairing state changed concurrently; restart pairing"
        )

    def invitations(self) -> tuple[PairingInvitationSummary, ...]:
        """Return safe status for every reusable host invitation."""

        try:
            records = self._store.read_latest_record_payloads(
                (
                    _PAIRING_INVITATION_RECORD_TYPE,
                    _PAIRING_ATTEMPT_RECORD_TYPE,
                )
            )
        except AuthorityNotInitializedError as exc:
            raise PairingGatewayNotInitializedError(
                "operator gateway is not initialized"
            ) from exc
        invitations: list[_StoredPairingInvitation] = []
        attempts_by_invitation: dict[
            UUID,
            list[tuple[str, _StoredPairingAttempt, int]],
        ] = {}
        for record, payload in records:
            encoded_payload = json.dumps(
                payload,
                separators=(",", ":"),
                allow_nan=False,
            )
            try:
                if record.record_type == _PAIRING_INVITATION_RECORD_TYPE:
                    invitations.append(
                        _StoredPairingInvitation.model_validate_json(encoded_payload)
                    )
                    continue
                attempt = _StoredPairingAttempt.model_validate_json(encoded_payload)
            except ValueError as exc:
                raise PairingError(
                    "stored pairing invitation state is invalid"
                ) from exc
            attempts_by_invitation.setdefault(attempt.invitation_id, []).append(
                (record.record_id, attempt, record.commit_index)
            )
        summaries = [
            self._invitation_summary(
                invitation,
                tuple(attempts_by_invitation.get(invitation.invitation_id, ())),
            )
            for invitation in invitations
        ]
        summaries.sort(key=lambda item: (item.created_at, str(item.invitation_id)))
        return tuple(summaries)

    def revoke_invitation(self, invitation_id: UUID) -> None:
        """Revoke an invitation without revoking its already paired devices."""

        invitation, invitation_commit_index, _, expected_commit_index = (
            self._invitation_snapshot(invitation_id)
        )
        if invitation.state == "revoked":
            return
        revoked = invitation.model_copy(update={"state": "revoked"})
        try:
            self._append_authority_record(
                record_type=_PAIRING_INVITATION_RECORD_TYPE,
                record_id=str(invitation_id),
                payload=revoked,
                expected_commit_index=expected_commit_index,
                expected_record_commit_index=invitation_commit_index,
            )
        except AuthorityCommitConflictError as exc:
            raise PairingSessionStateError(
                "pairing state changed concurrently; retry revocation"
            ) from exc

    def refresh(self, request: OperatorTokenRequest) -> OperatorTokenResponse:
        """Rotate one valid refresh credential and its access token.

        Args:
            request: Stable device identity and current refresh credential.

        Returns:
            A fresh access and refresh pair returned exactly once.

        Raises:
            OperatorCredentialInvalidError: The token is unknown or revoked.
            OperatorCredentialExpiredError: The refresh lifetime elapsed.
            OperatorDeviceNotFoundError: The device identity is unknown.
            PairingSessionStateError: Concurrent authority state changed.
        """

        record_type, record_id, session, session_commit_index = (
            self._load_device_session(request.device_id)
        )
        self._require_active_device(session)
        if session.refresh_token_hash is None or not compare_digest(
            session.refresh_token_hash,
            _opaque_digest(request.refresh_token),
        ):
            raise OperatorCredentialInvalidError("refresh credential is invalid")
        if (
            session.refresh_token_expires_at is None
            or self._now() >= session.refresh_token_expires_at
        ):
            raise OperatorCredentialExpiredError("refresh credential expired")

        now = self._now()
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        access_expires_at = now + _ACCESS_TOKEN_LIFETIME
        refresh_expires_at = now + _REFRESH_TOKEN_LIFETIME
        updated = session.model_copy(
            update={
                "access_token_hash": _opaque_digest(access_token),
                "access_token_expires_at": access_expires_at,
                "refresh_token_hash": _opaque_digest(refresh_token),
                "refresh_token_expires_at": refresh_expires_at,
            }
        )
        self._append_device_credential(
            record_type=record_type,
            record_id=record_id,
            credential=updated,
            expected_record_commit_index=session_commit_index,
        )
        return OperatorTokenResponse(
            access_token=access_token,
            access_token_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires_at,
            scopes=session.scopes,
        )

    def validate_access_token(
        self,
        token: str,
        *,
        required_scopes: Sequence[OperatorScope] = (),
    ) -> OperatorAccessContext:
        """Validate one bearer credential and enforce required scopes.

        Args:
            token: Opaque bearer access credential.
            required_scopes: Canonical scopes required by the target route.

        Returns:
            Validated device identity and granted scopes.

        Raises:
            OperatorCredentialInvalidError: The token is unknown or revoked.
            OperatorCredentialExpiredError: The access lifetime elapsed.
            OperatorScopeError: A required scope is absent.
        """

        token_hash = _opaque_digest(token)
        matched_session: _StoredDeviceCredential | None = None
        for _, _, session, _ in self._latest_device_sessions():
            if session.access_token_hash is not None and compare_digest(
                session.access_token_hash,
                token_hash,
            ):
                matched_session = session
        if matched_session is None or matched_session.state != "consumed":
            raise OperatorCredentialInvalidError("access credential is invalid")
        if (
            matched_session.access_token_expires_at is None
            or self._now() >= matched_session.access_token_expires_at
        ):
            raise OperatorCredentialExpiredError("access credential expired")
        missing_scopes = set(required_scopes).difference(matched_session.scopes)
        if missing_scopes:
            raise OperatorScopeError("access credential lacks a required scope")
        if matched_session.device_id is None or matched_session.device_name is None:
            raise PairingError("stored paired device is invalid")
        return OperatorAccessContext(
            device_id=matched_session.device_id,
            device_name=matched_session.device_name,
            scopes=matched_session.scopes,
        )

    def plugin_grants(self) -> tuple[PluginGrant, ...]:
        """Read explicit grants for the trusted owner control surface.

        No bearer authorizes this service method by itself. HTTP callers must
        pass the direct owner boundary before invoking it.
        """
        return tuple(
            self._plugin_grant(session)
            for _, _, session, _ in self._latest_device_sessions()
            if session.device_id is not None and session.device_name is not None
        )

    @staticmethod
    def _plugin_grant(session: _StoredDeviceCredential) -> PluginGrant:
        if session.device_id is None or session.device_name is None:
            raise PairingError("stored paired device is invalid")
        return PluginGrant(
            device_id=session.device_id,
            device_name=session.device_name,
            revision=session.plugin_grant_revision,
            scopes=tuple(scope for scope in PLUGIN_SCOPES if scope in session.scopes),
            active=session.state == "consumed",
        )

    def set_plugin_grant(
        self, device_id: UUID, update: PluginGrantUpdate
    ) -> PluginGrant:
        """Persist an explicit owner grant without rotating or exposing tokens.

        The encrypted credential append fences concurrent refresh, revocation
        and grant changes. Existing access tokens immediately use this record;
        neither an old token nor refresh can resurrect removed privileges.
        """
        record_type, record_id, session, commit_index = self._load_device_session(
            device_id
        )
        self._require_active_device(session)
        if session.plugin_grant_revision != update.expected_revision:
            raise PairingSessionStateError(
                "plugin grant changed; reload before editing"
            )
        scopes: tuple[OperatorScope, ...] = (
            *(scope for scope in session.scopes if scope not in PLUGIN_SCOPES),
            *update.scopes,
        )
        updated = session.model_copy(
            update={
                "scopes": scopes,
                "plugin_grant_revision": session.plugin_grant_revision + 1,
            }
        )
        self._append_device_credential(
            record_type=record_type,
            record_id=record_id,
            credential=updated,
            expected_record_commit_index=commit_index,
        )
        return self._plugin_grant(updated)

    def devices(self, access_token: str) -> OperatorDevicesResponse:
        """List safe paired-device projections for an authorized device.

        Args:
            access_token: Bearer credential requiring device-management scope.

        Returns:
            Active and revoked device records without credential material.
        """

        context = self.validate_access_token(
            access_token,
            required_scopes=("devices:manage",),
        )
        devices: list[OperatorDevice] = []
        for _, _, session, _ in self._latest_device_sessions():
            if session.device_id is None or session.device_name is None:
                continue
            devices.append(
                OperatorDevice(
                    device_id=session.device_id,
                    name=session.device_name,
                    paired_at=session.created_at,
                    refresh_expires_at=session.refresh_token_expires_at,
                    state="revoked" if session.state == "revoked" else "active",
                    current=session.device_id == context.device_id,
                )
            )
        devices.sort(key=lambda device: (device.paired_at, str(device.device_id)))
        return OperatorDevicesResponse(devices=tuple(devices))

    def revoke_device(self, access_token: str, device_id: UUID) -> None:
        """Revoke one paired device immediately and idempotently.

        Args:
            access_token: Bearer credential requiring device-management scope.
            device_id: Stable identity of the device to revoke.

        Raises:
            OperatorCredentialError: The requesting bearer is invalid.
            OperatorDeviceNotFoundError: The target device is unknown.
            PairingSessionStateError: Concurrent authority state changed.
        """

        self.validate_access_token(
            access_token,
            required_scopes=("devices:manage",),
        )
        record_type, record_id, session, session_commit_index = (
            self._load_device_session(device_id)
        )
        if session.state == "revoked":
            return
        revoked = session.model_copy(
            update={
                "state": "revoked",
                "access_token_hash": None,
                "access_token_expires_at": None,
                "refresh_token_hash": None,
                "refresh_token_expires_at": None,
            }
        )
        self._append_device_credential(
            record_type=record_type,
            record_id=record_id,
            credential=revoked,
            expected_record_commit_index=session_commit_index,
        )

    def _latest_device_sessions(
        self,
    ) -> tuple[tuple[str, str, _StoredDeviceCredential, int], ...]:
        """Return legacy and invitation-backed paired-device records."""

        latest_records: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            records = self._store.records()
        except AuthorityNotInitializedError as exc:
            raise PairingGatewayNotInitializedError(
                "operator gateway is not initialized"
            ) from exc
        for record in reversed(records):
            identity = (record.record_type, record.record_id)
            if (
                record.record_type
                not in {
                    _PAIRING_RECORD_TYPE,
                    _PAIRING_ATTEMPT_RECORD_TYPE,
                }
                or identity in seen
            ):
                continue
            seen.add(identity)
            latest_records.append(identity)
        sessions: list[tuple[str, str, _StoredDeviceCredential, int]] = []
        for record_type, record_id in reversed(latest_records):
            if record_type == _PAIRING_RECORD_TYPE:
                credential, commit_index = self._load_session(record_id)
            else:
                credential, commit_index = self._load_attempt(record_id)
            if credential.device_id is not None:
                sessions.append((record_type, record_id, credential, commit_index))
        return tuple(sessions)

    def _load_device_session(
        self,
        device_id: UUID,
    ) -> tuple[str, str, _StoredDeviceCredential, int]:
        """Load the newest credential state for one stable device identity."""

        for (
            record_type,
            record_id,
            session,
            commit_index,
        ) in self._latest_device_sessions():
            if session.device_id == device_id:
                return record_type, record_id, session, commit_index
        raise OperatorDeviceNotFoundError("paired device was not found")

    @staticmethod
    def _require_active_device(session: _StoredDeviceCredential) -> None:
        """Reject a revoked or structurally incomplete device record."""

        if session.state != "consumed":
            raise OperatorCredentialInvalidError("device credential is revoked")
        if session.device_id is None or session.device_name is None:
            raise PairingError("stored paired device is invalid")

    def _load_session(self, nonce_hash: str) -> tuple[_StoredPairingSession, int]:
        """Load and validate one encrypted session by its nonce digest."""

        try:
            record, payload = self._store.read_latest_record_payload(
                _PAIRING_RECORD_TYPE,
                nonce_hash,
            )
        except AuthorityNotInitializedError as exc:
            if not self._store.path.exists():
                raise PairingGatewayNotInitializedError(
                    "operator gateway is not initialized"
                ) from exc
            raise PairingSessionNotFoundError("pairing session was not found") from exc
        try:
            session = _StoredPairingSession.model_validate_json(
                json.dumps(payload, separators=(",", ":"), allow_nan=False)
            )
        except ValueError as exc:
            raise PairingError("stored pairing session is invalid") from exc
        return session, record.commit_index

    def _load_attempt(self, record_id: str) -> tuple[_StoredPairingAttempt, int]:
        """Load and validate one encrypted invitation attempt."""

        try:
            record, payload = self._store.read_latest_record_payload(
                _PAIRING_ATTEMPT_RECORD_TYPE,
                record_id,
            )
        except AuthorityNotInitializedError as exc:
            raise PairingSessionNotFoundError("pairing attempt was not found") from exc
        try:
            attempt = _StoredPairingAttempt.model_validate_json(
                json.dumps(payload, separators=(",", ":"), allow_nan=False)
            )
        except ValueError as exc:
            raise PairingError("stored pairing attempt is invalid") from exc
        return attempt, record.commit_index

    def _invitation_snapshot(
        self,
        invitation_id: UUID,
    ) -> tuple[
        _StoredPairingInvitation,
        int,
        tuple[tuple[str, _StoredPairingAttempt, int], ...],
        int,
    ]:
        """Read invitation usage against one globally fenced journal snapshot."""

        try:
            records = self._store.records()
        except AuthorityNotInitializedError as exc:
            raise PairingGatewayNotInitializedError(
                "operator gateway is not initialized"
            ) from exc
        if not records:
            raise PairingSessionNotFoundError("pairing invitation was not found")
        expected_commit_index = records[-1].commit_index
        invitation_record = next(
            (
                record
                for record in reversed(records)
                if record.record_type == _PAIRING_INVITATION_RECORD_TYPE
                and record.record_id == str(invitation_id)
            ),
            None,
        )
        if invitation_record is None:
            raise PairingSessionNotFoundError("pairing invitation was not found")
        try:
            _, invitation_payload = self._store.read_latest_record_payload(
                _PAIRING_INVITATION_RECORD_TYPE,
                str(invitation_id),
            )
            invitation = _StoredPairingInvitation.model_validate_json(
                json.dumps(invitation_payload, separators=(",", ":"), allow_nan=False)
            )
        except (AuthorityNotInitializedError, ValueError) as exc:
            raise PairingError("stored pairing invitation is invalid") from exc

        attempt_record_ids: list[str] = []
        seen: set[str] = set()
        for record in reversed(records):
            if record.record_type != _PAIRING_ATTEMPT_RECORD_TYPE:
                continue
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            attempt_record_ids.append(record.record_id)
        attempts: list[tuple[str, _StoredPairingAttempt, int]] = []
        for record_id in reversed(attempt_record_ids):
            attempt, commit_index = self._load_attempt(record_id)
            if attempt.invitation_id == invitation_id:
                attempts.append((record_id, attempt, commit_index))
        return (
            invitation,
            invitation_record.commit_index,
            tuple(attempts),
            expected_commit_index,
        )

    def _require_invitation_available(
        self,
        invitation: _StoredPairingInvitation,
        *,
        nonce: str,
        attempts: tuple[tuple[str, _StoredPairingAttempt, int], ...],
        creating_attempt: bool,
    ) -> None:
        """Reject unknown bearer material and unavailable invitation transitions."""

        if not compare_digest(invitation.nonce_hash, _opaque_digest(nonce)):
            raise PairingSessionNotFoundError("pairing invitation was not found")
        now = self._now()
        if invitation.state == "revoked":
            raise PairingSessionExpiredError("pairing invitation is revoked")
        if now >= invitation.expires_at:
            raise PairingSessionExpiredError("pairing invitation is expired")
        successful_pairings = sum(
            attempt.state in {"consumed", "revoked"} for _, attempt, _ in attempts
        )
        if successful_pairings >= invitation.max_pairings:
            raise PairingSessionExpiredError("pairing invitation is exhausted")
        if creating_attempt and len(attempts) >= _MAXIMUM_TOTAL_INVITATION_ATTEMPTS:
            raise PairingSessionExpiredError(
                "pairing invitation reached its attempt limit"
            )

    def _invitation_summary(
        self,
        invitation: _StoredPairingInvitation,
        attempts: tuple[tuple[str, _StoredPairingAttempt, int], ...],
    ) -> PairingInvitationSummary:
        """Derive safe status from invitation and attempt truth."""

        now = self._now()
        successful_pairings = sum(
            attempt.state in {"consumed", "revoked"} for _, attempt, _ in attempts
        )
        active_attempts = sum(
            attempt.state == "challenged" and now < attempt.expires_at
            for _, attempt, _ in attempts
        )
        state: PairingInvitationState
        if invitation.state == "revoked":
            state = "revoked"
        elif now >= invitation.expires_at:
            state = "expired"
        elif successful_pairings >= invitation.max_pairings:
            state = "exhausted"
        elif (
            len(attempts) >= _MAXIMUM_TOTAL_INVITATION_ATTEMPTS
            or active_attempts >= _MAXIMUM_ACTIVE_INVITATION_ATTEMPTS
        ):
            state = "attempt-limit"
        else:
            state = "active"
        return PairingInvitationSummary(
            invitation_id=invitation.invitation_id,
            created_at=invitation.created_at,
            expires_at=invitation.expires_at,
            successful_pairings=successful_pairings,
            max_pairings=invitation.max_pairings,
            active_attempts=active_attempts,
            total_attempts=len(attempts),
            state=state,
        )

    def _append_authority_record(
        self,
        *,
        record_type: str,
        record_id: str,
        payload: FrozenModel,
        expected_commit_index: int | None = None,
        expected_record_commit_index: int = 0,
    ) -> None:
        """Append one typed encrypted authority record with optional global fencing."""

        if expected_commit_index is None:
            records = self._store.records()
            expected_commit_index = records[-1].commit_index if records else 1
        serialized = cast(
            Mapping[str, object],
            payload.model_dump(mode="json", by_alias=True),
        )
        self._store.append(
            expected_commit_index=expected_commit_index,
            expected_record_commit_index=expected_record_commit_index,
            authority_term=1,
            record_type=record_type,
            record_id=record_id,
            payload=serialized,
        )

    def _append_device_credential(
        self,
        *,
        record_type: str,
        record_id: str,
        credential: _StoredDeviceCredential,
        expected_record_commit_index: int,
    ) -> None:
        """Append a legacy or invitation-backed credential transition."""

        try:
            self._append_authority_record(
                record_type=record_type,
                record_id=record_id,
                payload=credential,
                expected_record_commit_index=expected_record_commit_index,
            )
        except AuthorityCommitConflictError as exc:
            raise PairingSessionStateError(
                "pairing state changed concurrently; restart pairing"
            ) from exc

    def _append_session(
        self,
        nonce_hash: str,
        session: _StoredPairingSession,
        *,
        expected_record_commit_index: int = 0,
    ) -> None:
        """Append one encrypted session transition with local CAS."""

        try:
            self._append_authority_record(
                record_type=_PAIRING_RECORD_TYPE,
                record_id=nonce_hash,
                payload=session,
                expected_record_commit_index=expected_record_commit_index,
            )
        except AuthorityCommitConflictError as exc:
            raise PairingSessionStateError(
                "pairing state changed concurrently; restart pairing"
            ) from exc

    def _require_pending(self, session: _StoredPairingSession) -> None:
        """Require an unexpired session that has not issued a challenge."""

        self._require_unexpired(session)
        if session.state != "pending":
            raise PairingSessionStateError("pairing session was already used")

    def _require_unexpired(self, session: _StoredPairingSession) -> None:
        """Reject an expired session without modifying its durable record."""

        if self._now() >= session.expires_at:
            raise PairingSessionExpiredError("pairing session expired")

    @staticmethod
    def _decode_device_public_key(value: str) -> Ed25519PublicKey:
        """Decode and validate one raw Ed25519 device public key."""

        try:
            raw = _base64url_decode(value)
            if len(raw) != 32:
                raise ValueError("wrong key length")
            return Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, UnicodeError) as exc:
            raise PairingProofError("device public key is invalid") from exc
