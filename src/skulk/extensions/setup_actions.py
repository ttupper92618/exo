"""Optional nonbillable owner setup actions with durable progress observations."""

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from skulk.extensions.configuration import ConfigurationDigest, ConfigurationNodeId
from skulk.utils.pydantic_ext import FrozenModel

SetupOperationId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


def _omit_false(value: object) -> bool:
    # Preserve serialized legacy intents across the additive authorization field.
    return value is False


class SetupAction(FrozenModel):
    """A fixed installed action form containing ordinary external inputs only."""

    action_id: ConfigurationNodeId = Field(description="Stable installed action ID.")
    title: str = Field(min_length=1, max_length=128, description="Owner-facing label.")
    description: str = Field(min_length=1, max_length=512, description="Setup effect.")
    requires_approval: bool = Field(
        default=False,
        description="Setup also requires explicit plugins:approve authority.",
    )
    parameters_schema: dict[str, JsonValue] = Field(
        description="Ordinary-input JSON Schema; credentials are supplied separately."
    )
    schema_digest: ConfigurationDigest = Field(description="Exact action schema fence.")


class SetupOperation(FrozenModel):
    """Retained safe progress; completion does not imply enablement or approval."""

    operation_id: SetupOperationId = Field(description="Durable accepted operation ID.")
    node_id: ConfigurationNodeId = Field(description="Persistent installed node ID.")
    action_id: ConfigurationNodeId = Field(description="Selected installed action ID.")
    requires_approval: bool = Field(
        default=False, description="Retained approval requirement for original intent."
    )
    phase: Literal["queued", "running", "complete", "failed"] = Field(
        description="Last durable setup observation; unfinished work is reconciled."
    )
    code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$", description="Safe error code."
    )
    corrective_action: str | None = Field(
        default=None, max_length=512, description="Safe owner recovery guidance."
    )


class SetupActions(FrozenModel):
    """Read-only installed forms, revision fences and bounded retained progress."""

    node_id: ConfigurationNodeId = Field(description="Persistent installed node ID.")
    revision: int = Field(ge=0, description="Current configuration revision.")
    schema_digest: ConfigurationDigest = Field(
        description="Configuration schema fence."
    )
    credential_revision: int = Field(ge=0, description="Current credential revision.")
    credential_schema_digest: ConfigurationDigest = Field(
        description="Credential declaration fence; never a secret fingerprint."
    )
    actions: tuple[SetupAction, ...] = Field(
        max_length=8, description="Installed forms."
    )
    operations: tuple[SetupOperation, ...] = Field(
        default=(), max_length=32, description="Retained safe progress after reconnect."
    )


class SetupMutation(FrozenModel):
    """Exact nonbillable setup intent accepted once before asynchronous work."""

    operation_id: SetupOperationId = Field(
        description="Caller-generated retry identity."
    )
    action_id: ConfigurationNodeId = Field(description="Exact installed action ID.")
    expected_revision: int = Field(ge=0, description="Reviewed configuration revision.")
    expected_schema_digest: ConfigurationDigest = Field(
        description="Reviewed configuration schema."
    )
    expected_credential_revision: int = Field(
        ge=0, description="Reviewed credential revision."
    )
    expected_credential_schema_digest: ConfigurationDigest = Field(
        description="Reviewed credential declarations."
    )
    expected_action_schema_digest: ConfigurationDigest = Field(
        description="Reviewed action input schema."
    )
    expected_requires_approval: bool = Field(
        default=False,
        exclude_if=_omit_false,
        description="Reviewed authorization requirement, never a grant.",
    )
    values: dict[str, JsonValue] = Field(description="Ordinary external inputs only.")


class SetupResume(FrozenModel):
    """Optional empty resume body; original intent cannot be replaced by new inputs."""


@runtime_checkable
class NodeSetupActionsProvider(Protocol):
    """Optional management facet; setup runs independently of an admitted child call."""

    async def node_setup_actions(self, node_id: str) -> SetupActions:
        """Read forms and retained observations without initializing or doing effects."""
        ...

    async def start_node_setup(
        self, node_id: str, mutation: SetupMutation
    ) -> SetupOperation:
        """Persist exact intent and return promptly; client loss must not abandon work."""
        ...

    async def node_setup_operation(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Read one retained observation independently of ongoing setup work."""
        ...

    async def resume_node_setup(
        self, node_id: str, operation_id: str
    ) -> SetupOperation:
        """Resume the original accepted intent without accepting replacement inputs."""
        ...
