"""Inert planning can outlast a read without extending the read tool deadline."""

import asyncio
from typing import final

import pytest
from pydantic import JsonValue

from skulk.extensions import ExtensionContext
from skulk.extensions.steward import (
    StewardTool,
    collect_steward_tools,
    invoke_steward_tool,
)
from skulk.extensions.tests.test_steward_tools import Adapter, context


@final
class SlowPreparation:
    """Exercise actual cancellation rather than mirroring timeout constants."""

    def __init__(self, proposal: bool) -> None:
        self.fixture = Adapter(proposal)
        self.completed = False
        self.finished = False

    async def steward_tools(self, context: ExtensionContext) -> tuple[StewardTool, ...]:
        """Return a bounded inert fixture tool."""
        return self.fixture.tools

    async def handle_steward_tool(
        self,
        context: ExtensionContext,
        tool: StewardTool,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Model nonbillable planning work that is slower than a permitted read."""
        try:
            await asyncio.sleep(5.1)
            self.completed = True
            return {"prepared": True}
        finally:
            self.finished = True


@pytest.mark.parametrize("proposal", [False, True])
async def test_inert_preparation_deadline_does_not_extend_reads(proposal: bool) -> None:
    """Reads cancel; authorized proposal preparation finishes without execution."""
    provider = SlowPreparation(proposal)
    host = context()
    bindings = await collect_steward_tools([provider], host, proposals_allowed=True)
    assert len(bindings) == 1
    result = await invoke_steward_tool(
        bindings[0], host, {"text": "prepare"}, proposals_allowed=True
    )
    assert provider.finished and provider.completed is proposal
    assert ("prepared" in result) is proposal
    assert ("error" in result) is not proposal
