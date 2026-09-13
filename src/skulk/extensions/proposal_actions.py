"""Distinct owner approval actions with durable, safe operation observations."""

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field

from skulk.extensions.configuration import ConfigurationDigest
from skulk.extensions.proposal_review import ProposalReference
from skulk.utils.pydantic_ext import FrozenModel

ProposalOperationId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class ProposalApproval(FrozenModel):
    """Approve exactly the reviewed provider intent, with no executable replacements."""

    operation_id: ProposalOperationId = Field(description="Durable owner action ID.")
    reference: ProposalReference = Field(
        description="Exact reviewed journal reference."
    )
    review_revision: ConfigurationDigest = Field(
        description="Provider revision binding the reviewed policy and terms."
    )


class ProposalReconciliation(FrozenModel):
    """Receipt evidence separate from submission history and inference readiness."""

    state: Literal["pending", "active", "releasing", "absent", "attention", "unknown"] = Field(
        description="Correlated lifecycle state; absence requires retained evidence."
    )
    observed_at: int | None = Field(
        default=None, gt=0, le=253402300799,
        description="Cleanup journal read time, UTC seconds; null before observation."
    )
    stale: bool = Field(description="Current cleanup access or worker health is unconfirmed.")
    code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$",
        description="Safe reconciliation status code, separate from submission errors."
    )


class ProposalOperation(FrozenModel):
    """Safe durable progress; reconnect never replays an uncertain dispatch."""

    operation_id: ProposalOperationId = Field(description="Accepted owner action ID.")
    reference: ProposalReference = Field(
        description="Exact retained proposal reference."
    )
    phase: Literal[
        "accepted",
        "approving",
        "approved",
        "dispatching",
        "acknowledged",
        "succeeded",
        "refused",
        "approval_interrupted",
        "uncertain",
    ] = Field(description="Last durable observation, not a new execution grant.")
    updated_at: int = Field(gt=0, description="Observation time as UTC Unix seconds.")
    reconciliation: ProposalReconciliation | None = Field(
        default=None,
        description="Later cleanup evidence; never changes submission history.",
    )
    code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
        description="Safe status code.",
    )
    corrective_action: str | None = Field(
        default=None, max_length=512, description="Safe owner recovery guidance."
    )


@runtime_checkable
class NodeProposalActionsProvider(Protocol):
    """Optional owner-only approval/execution facet, never exposed as a steward tool."""

    async def approve_node_proposal(
        self, mutation: ProposalApproval, operator_id: str
    ) -> ProposalOperation:
        """Persist reviewed intent before work; client disconnect does not abandon it."""
        ...

    async def node_proposal_operation(
        self, node_id: str, operation_id: str
    ) -> ProposalOperation:
        """Observe a durable action without approval, dispatch or replay."""
        ...

    async def resume_node_proposal(
        self, node_id: str, operation_id: str, operator_id: str
    ) -> ProposalOperation:
        """Explicitly resume interrupted approval only; never replay submitted work."""
        ...
