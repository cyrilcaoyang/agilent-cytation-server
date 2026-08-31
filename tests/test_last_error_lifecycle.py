"""`last_error` clearing (STATUS_SPEC §6.4).

The field is defined as "the most recent operational failure **since the
last successful action**", not "since process start". Until now only
`startup` cleared it, so one transient fault sat on the dashboard tile
through every subsequent successful read until somebody restarted the
service — the operator is left wondering why a demonstrably working
instrument is still complaining.

The negative halves matter as much as the positive one: a refusal must
neither populate `last_error` (§6.3) nor clear it (§6.4's table gives no
clear for any 4xx), and reading `/status` must never clear it either.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agilent_cytation_server.models import ErrorInfo

READ_BODY = {"wells": ["A1"], "wavelength_nm": 260.0}


def _plant_error(client, code: str = "read.absorbance") -> None:
    """Put a stale operational failure on the device, as a real one would."""
    client.app.state.service._last_error = ErrorInfo(
        code=code,
        message="something broke earlier",
        severity="error",
        timestamp=datetime.now(timezone.utc),
    )


def test_a_successful_read_clears_a_stale_error(loaded_client) -> None:
    _plant_error(loaded_client)
    assert loaded_client.get("/status").json()["last_error"] is not None

    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 200

    assert loaded_client.get("/status").json()["last_error"] is None


def test_a_successful_drawer_move_clears_it_too(loaded_client) -> None:
    """Any operational action, not just the one that failed (§6.4's table)."""
    _plant_error(loaded_client)

    assert loaded_client.post("/control/drawer/open").status_code in (200, 204)

    assert loaded_client.get("/status").json()["last_error"] is None


def test_shake_clears_it_although_it_is_not_bracketed(loaded_client) -> None:
    """shake.start runs outside `_operation`, so it needs the clear by hand.

    Worth pinning precisely because it is the easy one to forget: the shake
    command returns as soon as motion begins and the span is observed from
    the driver's flag instead, so it never passes through the bracket that
    clears everything else.
    """

    _plant_error(loaded_client)

    assert loaded_client.post("/control/shake/start", json={}).status_code in (200, 204)

    assert loaded_client.get("/status").json()["last_error"] is None


def test_a_refusal_does_not_clear_it(loaded_client) -> None:
    """§6.4's table: no clear on any 4xx.

    A 412 is the device saying "not now", which is no evidence at all that
    whatever broke earlier has been fixed.
    """

    loaded_client.post("/control/drawer/open")
    _plant_error(loaded_client)

    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 412

    assert loaded_client.get("/status").json()["last_error"] is not None


def test_reading_status_does_not_clear_it(loaded_client) -> None:
    """`/status` is side-effect-free (§4 best practice #1)."""
    _plant_error(loaded_client)

    for _ in range(3):
        assert loaded_client.get("/status").status_code == 200

    assert loaded_client.get("/status").json()["last_error"] is not None


def test_claim_verbs_do_not_clear_it(client) -> None:
    """§6.4 lists claim/heartbeat/release as non-clearing — they are
    infrastructure, not evidence that the hardware is working again."""

    _plant_error(client)
    r = client.post(
        "/control/claim", json={"owner": "test", "session_id": "s-1", "ttl_s": 30.0}
    )
    assert r.status_code == 200
    token = r.json()["claim_token"]
    client.post("/control/heartbeat", headers={"X-Claim-Token": token})

    assert client.get("/status").json()["last_error"] is not None
