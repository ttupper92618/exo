"""Explicit plugin privileges, separate from ordinary cluster operations."""

from typing import Final, Literal

type PluginScope = Literal["plugins:read", "plugins:manage", "plugins:approve"]

PLUGIN_SCOPES: Final[tuple[PluginScope, ...]] = (
    "plugins:read",
    "plugins:manage",
    "plugins:approve",
)


def required_plugin_scope(method: str, path: str) -> PluginScope | None:
    """Return the privilege for a canonical plugin route, or none outside it.

    Approval and signed resource release have distinct routes so an ordinary
    management request cannot smuggle approval authority in its body.
    """
    parts = path.rstrip("/").split("/")
    if parts[:3] != ["", "v1", "plugins"]:
        return None
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return "plugins:read"
    if parts[-1] in {"approve", "release"} or (
        len(parts) == 9
        and parts[4] == "nodes"
        and parts[6] == "proposal-operations"
        and parts[8] == "resume"
    ):
        return "plugins:approve"
    return "plugins:manage"
