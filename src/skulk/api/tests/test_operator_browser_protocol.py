"""The browser's public pairing fixtures bind the production Python proof bytes."""

import base64
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from skulk.operator.pairing import (
    pairing_invitation_signature_message,
    pairing_signature_message,
)


def test_browser_fixture_uses_production_pairing_proofs() -> None:
    """Prevent browser and Python proof domains or field ordering from drifting."""
    fixture = TypeAdapter(dict[str, str]).validate_json(
        (
            Path(__file__).resolve().parents[4]
            / "dashboard-react/src/auth/operatorProtocol.fixture.json"
        ).read_bytes()
    )
    cluster = UUID(fixture["clusterId"])
    assert base64.b64decode(fixture["legacyProof"]) == pairing_signature_message(
        cluster_id=cluster, nonce="n" * 48, challenge=fixture["challenge"]
    )
    assert base64.b64decode(
        fixture["invitationProof"]
    ) == pairing_invitation_signature_message(
        cluster_id=cluster,
        invitation_id=UUID("22222222-2222-4222-8222-222222222222"),
        nonce="n" * 48,
        attempt_id=UUID(fixture["attemptId"]),
        challenge=fixture["challenge"],
    )
