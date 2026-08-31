"""The drawer interlock: reads and captures need the carrier in.

`_drawer` was tracked from the moment drawer control shipped and read by
nothing, so a read issued with the carrier out went all the way to the
driver, whose acknowledgement assertion fails with an *empty*
``AssertionError``. `_operation` recorded that as an operational failure —
driving the device to `error` and reddening the tile for a reader that never
broke, which §6.3 says must not happen — and told the operator only that
something had failed. Chasing exactly that assertion is what cost the
2026-08-23 bench session an hour.

The interlock is deliberately one-sided: it blocks on a *known-open* drawer
and never on `"unknown"`. There is no position query anywhere in the driver's
command set, so `_drawer` is dead reckoning, and an interlock that guesses
wrong in the blocking direction cannot be argued with by the operator.
"""

from __future__ import annotations

import pytest

READ_BODY = {"wells": ["A1"], "wavelength_nm": 260.0}
CAPTURE_BODY = {"well": "A1", "channel": "brightfield"}


def _open_drawer(client) -> None:
    assert client.post("/control/drawer/open").status_code in (200, 204)


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_read_with_the_carrier_out_is_refused_with_412(loaded_client) -> None:
    _open_drawer(loaded_client)

    r = loaded_client.post("/control/read/absorbance", json=READ_BODY)

    assert r.status_code == 412
    # FastAPI nests an HTTPException's structured body under `detail`.
    body = r.json()["detail"]
    # §6.1: distinguishable by *shape*, not by string-matching prose.
    assert body["precondition"] == "drawer_open"
    assert body["drawer_state"] == "out"
    assert body["required"] == "in"
    assert body["required_action"] == "drawer.close"
    # Closing the drawer is an action, not a wait, so §6.1 wants the field
    # omitted rather than guessed.
    assert "retry_after_s" not in body


@pytest.mark.parametrize(
    "path,body",
    [
        ("/control/read/absorbance", READ_BODY),
        ("/control/read/fluorescence", {"wells": ["A1"], "excitation_nm": 485.0, "emission_nm": 528.0}),
        ("/control/read/luminescence", {"wells": ["A1"]}),
        ("/control/imaging/capture", CAPTURE_BODY),
    ],
)
def test_every_optical_action_is_gated(loaded_client, path, body) -> None:
    _open_drawer(loaded_client)
    assert loaded_client.post(path, json=body).status_code == 412


def test_closing_the_drawer_clears_it(loaded_client) -> None:
    _open_drawer(loaded_client)
    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 412

    assert loaded_client.post("/control/drawer/close").status_code in (200, 204)

    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 200


def test_an_unknown_drawer_does_not_block(loaded_client) -> None:
    """Fail open on uncertainty — the deliberate half of the design.

    A stale `"in"` costs the same driver assertion we already got. A stale
    `"out"` would refuse every read on a perfectly loaded instrument, and the
    operator has no way to correct it: nothing reports where the carrier
    actually is.
    """

    service = loaded_client.app.state.service
    service._drawer = "unknown"

    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 200


# ---------------------------------------------------------------------------
# §6.2 — the two surfaces must not disagree
# ---------------------------------------------------------------------------


def test_allowed_actions_withholds_the_optical_actions(loaded_client) -> None:
    before = loaded_client.get("/status").json()["allowed_actions"]
    assert "read.absorbance" in before

    _open_drawer(loaded_client)
    after = loaded_client.get("/status").json()["allowed_actions"]

    for action in (
        "read.absorbance",
        "read.fluorescence",
        "read.luminescence",
        "imaging.capture",
    ):
        assert action not in after
    # The verb that clears the interlock has to stay reachable, or the
    # operator is stuck exactly where the tile can't help them.
    assert "drawer.close" in after


@pytest.mark.parametrize("drawer", ["in", "out", "unknown"])
def test_advertised_iff_not_refused(loaded_client, drawer: str) -> None:
    """The property §6.2 actually asks for, over every drawer state.

    "A client that reads /status, sees <X> in allowed_actions, and
    immediately POSTs /control/<X> must not get a 412 — that's a contract
    violation." Both surfaces read one helper, so this holds by construction;
    the test is here to keep it that way.
    """

    service = loaded_client.app.state.service
    service._drawer = drawer

    advertised = "read.absorbance" in loaded_client.get("/status").json()["allowed_actions"]
    refused = (
        loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 412
    )

    assert advertised is not refused


# ---------------------------------------------------------------------------
# §6.3 — a refusal is not a failure
# ---------------------------------------------------------------------------


def test_the_refusal_leaves_last_error_and_the_state_alone(loaded_client) -> None:
    _open_drawer(loaded_client)
    assert loaded_client.post("/control/read/absorbance", json=READ_BODY).status_code == 412

    s = loaded_client.get("/status").json()
    assert s["last_error"] is None
    assert s["equipment_status"] != "error"


def test_the_refusal_opens_no_activity_span(loaded_client) -> None:
    """A refusal is not an operation, so it must not stamp activity_since.

    Raising inside `_operation` would have been simpler and wrong: a poll
    landing between the two edges would see `running` for a read that never
    started, and the span's start would move for a request the instrument
    never heard about.
    """

    _open_drawer(loaded_client)
    before = loaded_client.get("/status").json()

    loaded_client.post("/control/read/absorbance", json=READ_BODY)

    after = loaded_client.get("/status").json()
    assert after["activity"] == "idle"
    assert after["activity_since"] == before["activity_since"]


def test_cycles_total_does_not_count_a_refusal(loaded_client) -> None:
    before = loaded_client.get("/status").json()["metrics"]["cycles_total"]["value"]

    _open_drawer(loaded_client)
    loaded_client.post("/control/read/absorbance", json=READ_BODY)

    after = loaded_client.get("/status").json()["metrics"]["cycles_total"]["value"]
    assert after == before
