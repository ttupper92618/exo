"""Provider-neutral configuration for stable installed capability nodes."""

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from skulk.utils.pydantic_ext import FrozenModel

ConfigurationNodeId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:@-]+$")
]
ConfigurationDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ConfigurableNode(FrozenModel):
    """One installed node; several capabilities may share its settings."""

    node_id: ConfigurationNodeId = Field(
        description="Persistent capability-node identity."
    )
    bundle_id: str = Field(max_length=128, description="Installed bundle identifier.")
    version: str = Field(max_length=64, description="Installed bundle version.")
    status: str = Field(max_length=64, description="Current node lifecycle state.")
    configurable: bool = Field(
        description="Whether this node declares a configuration schema."
    )
    credentials_configurable: bool = Field(
        default=False,
        description="Whether this node declares separate write-only credential inputs.",
    )

    preflight_available: bool = Field(
        default=False, description="Whether this node provides managed setup checks."
    )
    setup_actions_available: bool = Field(
        default=False,
        description="Whether this node exposes durable nonbillable setup actions.",
    )
    setup_available: bool = Field(
        default=False, description="Whether this node exposes public setup files."
    )
    proposals_available: bool = Field(
        default=False,
        description="Whether this node exposes retained proposal reviews.",
    )
    proposal_actions_available: bool = Field(
        default=False,
        description="Whether distinct owner approval actions are supported.",
    )


class NodeConfiguration(FrozenModel):
    """Schema and ordinary settings for one node; credential values are excluded."""

    node_id: ConfigurationNodeId = Field(
        description="Persistent capability-node identity."
    )
    revision: int = Field(
        ge=0, description="Compare-and-set revision of these settings."
    )
    schema_digest: ConfigurationDigest = Field(
        description="Digest fencing the exact schema."
    )
    configuration_schema: dict[str, JsonValue] = Field(
        description="Plugin-declared ordinary-settings JSON Schema."
    )
    values: dict[str, JsonValue] = Field(
        description="Current ordinary settings, never secret values."
    )
    enabled: bool = Field(description="Whether the owner enabled this node.")


class ConfigurationMutation(FrozenModel):
    """Revision-fenced ordinary configuration action; it cannot grant paid authority."""

    operation: Literal["validate", "edit", "enable", "disable"] = Field(
        description="Requested node configuration action."
    )
    expected_revision: int = Field(ge=0, description="Observed configuration revision.")
    expected_schema_digest: ConfigurationDigest = Field(
        description="Observed schema digest."
    )
    values: dict[str, JsonValue] | None = Field(
        default=None, description="Proposed ordinary values for validate/edit only."
    )


class ConfigurationResult(FrozenModel):
    """Validated or persisted settings returned without credential material."""

    configuration: NodeConfiguration = Field(
        description="Current settings after the operation."
    )
    validated: bool = Field(
        description="Whether the proposed values passed validation."
    )


@runtime_checkable
class NodeConfigurationProvider(Protocol):
    """Optional owner-management facet, independent of capability readiness.

    Implementations retain management of disabled or failed children. Providers
    own schema validation and revision fencing; all values are ordinary settings.
    Remote callers never receive local paths or credential values through this facet.
    """

    async def configuration_nodes(self) -> tuple[ConfigurableNode, ...]:
        """List persistent installed children, including disabled/failed nodes."""
        ...

    async def node_configuration(self, node_id: str) -> NodeConfiguration:
        """Read ordinary settings and their exact schema for one owned node."""
        ...

    async def configure_node(
        self, node_id: str, mutation: ConfigurationMutation
    ) -> ConfigurationResult:
        """Validate or atomically update the named node after revision checks."""
        ...
