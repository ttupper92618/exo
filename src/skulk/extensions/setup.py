"""Public setup artifacts supplied by an installed plugin without secret export."""

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from skulk.extensions.configuration import ConfigurationDigest, ConfigurationNodeId
from skulk.utils.pydantic_ext import FrozenModel


class SetupArtifact(FrozenModel):
    """One inert public text file; never credential values or executable content."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$", description="Safe filename.")
    title: str = Field(min_length=1, max_length=128, description="Owner-facing label.")
    media_type: Literal["application/json", "text/plain"] = Field(
        description="Inert file type."
    )
    content: str = Field(max_length=16384, description="Public setup data only.")


class NodeSetup(FrozenModel):
    """Current public setup exports; no secret, command, path or spending authority."""

    node_id: ConfigurationNodeId = Field(
        description="Persistent installed node identity."
    )
    revision: int = Field(ge=0, description="Observed configuration revision.")
    schema_digest: ConfigurationDigest = Field(
        description="Current configuration schema."
    )
    credential_revision: int = Field(ge=0, description="Observed credential revision.")
    artifacts: tuple[SetupArtifact, ...] = Field(
        min_length=1, max_length=8, description="Public setup files."
    )


@runtime_checkable
class NodeSetupProvider(Protocol):
    """Optional read-only setup facet available independently of child readiness."""

    async def node_setup(self, node_id: str) -> NodeSetup:
        """Read public setup files without generating keys or performing effects."""
        ...
