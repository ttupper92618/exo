"""Provider-neutral setup observations for managed capability nodes."""

from typing import Protocol, runtime_checkable

from pydantic import Field

from skulk.extensions.configuration import ConfigurationDigest, ConfigurationNodeId
from skulk.utils.pydantic_ext import FrozenModel


class PreflightCheck(FrozenModel):
    """One safe prerequisite result supplied by the installed plugin."""

    code: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
        description="Stable prerequisite identifier.",
    )
    passed: bool = Field(description="Whether this prerequisite passed when checked.")
    corrective_action: str | None = Field(
        default=None,
        max_length=512,
        description="Safe guidance for an unsuccessful check.",
    )


class NodePreflight(FrozenModel):
    """Observed settings and prerequisite results; never an admission token."""

    node_id: ConfigurationNodeId = Field(
        description="Persistent installed node identity."
    )
    revision: int = Field(ge=0, description="Observed configuration revision.")
    schema_digest: ConfigurationDigest = Field(description="Checked settings schema.")
    values_digest: ConfigurationDigest = Field(
        description="Digest of the ordinary settings checked."
    )
    credential_revision: int | None = Field(
        default=None, ge=0, description="Observed credential revision if applicable."
    )
    observed_at: int = Field(description="UTC seconds when the owner began checks.")
    checks: tuple[PreflightCheck, ...] = Field(
        min_length=1, max_length=32, description="Individual prerequisite results."
    )


@runtime_checkable
class NodePreflightProvider(Protocol):
    """Optional management facet available independently of child readiness."""

    async def node_preflight(self, node_id: str) -> NodePreflight:
        """Inspect exact installed prerequisites without approval or billable work."""
        ...
