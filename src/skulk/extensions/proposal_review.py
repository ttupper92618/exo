"""Provider-owned proposal references and safe review data, without approval power."""

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from skulk.extensions.configuration import ConfigurationDigest, ConfigurationNodeId
from skulk.utils.pydantic_ext import FrozenModel


class ProposalReference(FrozenModel):
    """Resolve immutable intent in one installed provider's own journal."""

    plugin_id: str = Field(
        pattern=r"^managed\.[a-z0-9][a-z0-9._-]{0,80}$",
        description="Exact installed managed plugin identity.",
    )
    node_id: ConfigurationNodeId = Field(description="Stable installed node identity.")
    proposal_id: ConfigurationNodeId = Field(
        description="Provider-owned opaque identifier, never a path or canonical input."
    )
    proposal_digest: ConfigurationDigest = Field(
        description="Digest of the provider's complete immutable proposal intent."
    )


class ProposalSummary(FrozenModel):
    """Safe plain-text listing metadata; status never grants execution authority."""

    reference: ProposalReference = Field(description="Exact journal lookup reference.")
    summary: str = Field(
        min_length=1,
        max_length=1024,
        description="Plain-text operation, selected resources and spending summary.",
    )
    expires_at: int = Field(gt=0, description="Proposal expiry as UTC Unix seconds.")
    state: Literal[
        "pending_approval", "approved", "execution_recorded", "expired", "unavailable"
    ] = Field(
        description="Observed journal state; execution_recorded does not imply success."
    )


class ProposalField(FrozenModel):
    """One plain-text fact for owner review; no markup or credential values."""

    label: str = Field(
        min_length=1, max_length=64, description="Owner-facing fact label."
    )
    value: str = Field(
        min_length=1, max_length=2048, description="Safe plain-text value."
    )


class ProposalReview(FrozenModel):
    """Fresh review of exact retained intent, without executable input."""

    proposal: ProposalSummary = Field(
        description="Retained proposal and current state."
    )
    observed_at: int = Field(gt=0, description="UTC Unix seconds of this review read.")
    approval_revision: ConfigurationDigest | None = Field(
        default=None,
        description="Current approval fence, or null when approval is unavailable.",
    )
    fields: tuple[ProposalField, ...] = Field(
        min_length=1,
        max_length=32,
        description=(
            "Safe facts including model, context, resource, price, limits and "
            "cleanup terms as applicable."
        ),
    )


class ProposalPage(FrozenModel):
    """Bounded advisory listing; each selected proposal must be freshly reviewed."""

    proposals: tuple[ProposalSummary, ...] = Field(
        max_length=16, description="Up to sixteen retained proposal summaries."
    )
    next_offset: int | None = Field(
        default=None,
        ge=1,
        le=127,
        description=(
            "Next listing offset, or null at the current end; concurrent "
            "changes may move entries."
        ),
    )


@runtime_checkable
class NodeProposalReviewProvider(Protocol):
    """Optional read-only proposal facet, separate from approval and execution."""

    async def node_proposals(self, node_id: str, offset: int = 0) -> ProposalPage:
        """List bounded retained metadata without preparing or replaying an effect."""
        ...

    async def node_proposal(self, reference: ProposalReference) -> ProposalReview:
        """Require exact ID and digest match; never accept caller canonical replacements."""
        ...
