"""Proposal review requires explicit read authority and exact installed references."""

from pathlib import Path
from typing import final

import httpx
import pytest
from fastapi import FastAPI

from skulk.api.plugins import create_plugins_router
from skulk.api.tests.test_operator_gateway import paired_service
from skulk.api.tests.test_plugins import ManagedExtension
from skulk.extensions import LoadedExtensions
from skulk.extensions.proposal_review import (
    ProposalField,
    ProposalPage,
    ProposalReference,
    ProposalReview,
    ProposalSummary,
)
from skulk.operator.pairing import PluginGrantUpdate


@final
class ReviewProvider(ManagedExtension):
    """Keep canonical intent outside the core while returning safe display data."""

    name = "managed.fixture"

    def __init__(self) -> None:
        super().__init__()
        self.reference = ProposalReference(
            plugin_id=self.name,
            node_id="stable-node",
            proposal_id="vs-0123456789abcdef",
            proposal_digest="a" * 64,
        )
        self.foreign = False

    def summary(self) -> ProposalSummary:
        """An opaque ID intentionally differs from the canonical proposal digest."""
        return ProposalSummary(
            reference=self.reference,
            summary="Render approved model at 720p",
            expires_at=1_900_000_000,
            state="pending_approval",
        )

    async def node_proposals(self, node_id: str, offset: int = 0) -> ProposalPage:
        """Return one retained summary; no effect or approval is performed."""
        self.calls += 1
        if node_id != self.reference.node_id:
            raise LookupError("private diagnostic detail")
        return ProposalPage(proposals=(self.summary(),) if offset == 0 else ())

    async def node_proposal(self, reference: ProposalReference) -> ProposalReview:
        """Match both opaque ID and digest before projecting the provider journal."""
        self.calls += 1
        if reference != self.reference:
            raise ValueError("private canonical-input detail")
        summary = self.summary()
        if self.foreign:
            summary = summary.model_copy(
                update={
                    "reference": self.reference.model_copy(
                        update={"node_id": "foreign"}
                    )
                }
            )
        return ProposalReview(
            proposal=summary,
            observed_at=1_800_000_000,
            fields=(ProposalField(label="Model", value="example/model"),),
        )


async def test_proposal_review_scope_revocation_and_exact_reference(
    tmp_path: Path,
) -> None:
    """Broad grants, revoked readers and response substitution cannot expose a review."""
    service, exchange = paired_service(tmp_path)
    provider = ReviewProvider()
    app = FastAPI()
    app.include_router(create_plugins_router(LoadedExtensions([provider]), service))
    listing = f"/v1/plugins/{provider.name}/nodes/stable-node/proposals"
    review = listing + "/" + provider.reference.proposal_id
    bearer = {"Authorization": f"Bearer {exchange.access_token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://localhost"
    ) as client:
        assert (await client.get(listing, headers=bearer)).status_code == 403
        assert provider.calls == 0
        service.set_plugin_grant(
            exchange.device_id,
            PluginGrantUpdate(expected_revision=0, scopes=("plugins:read",)),
        )
        page = await client.get(listing, headers=bearer)
        assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
        assert (
            page.json()["proposals"][0]["reference"]["proposalId"]
            == provider.reference.proposal_id
        )
        response = await client.get(
            review, headers=bearer, params={"proposal_digest": "a" * 64}
        )
        assert response.status_code == 200 and "example/model" in response.text
        assert "canonical" not in response.text and "approval" not in response.json()
        mismatch = await client.get(
            review, headers=bearer, params={"proposal_digest": "b" * 64}
        )
        assert mismatch.status_code == 409 and "private" not in mismatch.text
        assert (await client.get(review, headers=bearer)).status_code == 422
        assert (
            await client.get(listing, headers=bearer, params={"offset": 128})
        ).status_code == 422
        provider.foreign = True
        assert (
            await client.get(
                review, headers=bearer, params={"proposal_digest": "a" * 64}
            )
        ).status_code == 409
        service.set_plugin_grant(
            exchange.device_id, PluginGrantUpdate(expected_revision=1, scopes=())
        )
        before = provider.calls
        assert (
            await client.get(
                review, headers=bearer, params={"proposal_digest": "a" * 64}
            )
        ).status_code == 403
        assert provider.calls == before
        assert (
            await client.get(
                listing,
                headers={
                    "Origin": "https://foreign.invalid",
                    "X-Skulk-Dashboard": "pairing-v1",
                },
            )
        ).status_code == 403


def test_reference_rejects_canonical_input_and_unbounded_or_path_identifiers() -> None:
    """A proposal reference cannot replace canonical provider input or become a URL."""
    reference = ReviewProvider().reference
    for changed in (
        {"proposal_id": "../../private"},
        {"proposal_id": "https://example.invalid"},
        {"proposal_id": "a" * 129},
        {"workflow": {"execute": True}},
    ):
        with pytest.raises(ValueError):
            ProposalReference.model_validate(reference.model_dump() | changed)
