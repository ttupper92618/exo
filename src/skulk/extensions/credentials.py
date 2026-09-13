"""Optional write-only credential management for installed capability nodes."""

from typing import Annotated, Literal, Protocol, final, runtime_checkable

from pydantic import Field, SecretStr

from skulk.extensions.configuration import ConfigurationDigest, ConfigurationNodeId
from skulk.utils.pydantic_ext import FrozenModel

CredentialIdentifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")]


@final
class CredentialStatus(FrozenModel):
    """Plugin-declared reference and readiness; no secret value or value fingerprint."""

    credential_id: CredentialIdentifier = Field(
        description="Stable node-local reference."
    )
    title: str = Field(
        min_length=1, max_length=128, description="Owner-facing credential label."
    )
    description: str = Field(
        max_length=2048, description="Credential purpose and provisioning guidance."
    )
    required: bool = Field(description="Whether setup requires this credential.")
    ready: bool = Field(
        description="Whether the selected backend resolves this reference."
    )


@final
class NodeCredentials(FrozenModel):
    """Credential metadata for one exact installed node, separate from ordinary settings."""

    node_id: ConfigurationNodeId = Field(
        description="Persistent installed capability-node identity."
    )
    revision: int = Field(
        ge=0, description="Shared credential revision, including terminal changes."
    )
    schema_digest: ConfigurationDigest = Field(
        description="Exact installed credential declaration digest."
    )
    credentials: tuple[CredentialStatus, ...] = Field(
        max_length=16,
        description="Declared reference readiness without secret material.",
    )


@final
class CredentialMutation(FrozenModel):
    """Revision-fenced write-only action; does not grant spending or erase cleanup history."""

    operation: Literal["replace", "retire"] = Field(
        description="Replace a value or retire its future use."
    )
    operation_id: str = Field(
        pattern=r"^[a-f0-9]{32}$",
        description="Durable operation identity retained across an unconfirmed response.",
    )
    credential_id: CredentialIdentifier = Field(
        description="One plugin-declared credential reference."
    )
    expected_revision: int = Field(ge=0, description="Observed credential revision.")
    expected_schema_digest: ConfigurationDigest = Field(
        description="Observed credential declaration digest."
    )
    value: SecretStr | None = Field(
        default=None,
        description="Write-only UTF-8 credential, required only for replacement.",
    )


@runtime_checkable
class NodeCredentialProvider(Protocol):
    """Optional management facet; providers own credential storage and retained history."""

    async def node_credentials(self, node_id: str) -> NodeCredentials:
        """Read declared references and readiness for an exact installed node."""
        ...

    async def change_node_credential(
        self, node_id: str, mutation: CredentialMutation
    ) -> NodeCredentials:
        """Apply one bounded revision-fenced mutation and return metadata only."""
        ...
