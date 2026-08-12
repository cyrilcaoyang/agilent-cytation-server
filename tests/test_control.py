"""Phase 3 tests for the /control/* operational surface.

All tests use the ``advisory_client`` fixture (claims disabled) so we
exercise the verbs themselves rather than the claim protocol (which
has its own coverage in ``test_claims.py``).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Drawer
# ---------------------------------------------------------------------------


def test_drawer_open_close(advisory_client) -> None:
    r = advisory_client.post("/control/drawer/open", json={})
    assert r.status_code == 204
    s = advisory_client.get("/status").json()
    assert s["details"]["drawer"] == "out"

    r = advisory_client.post("/control/drawer/close", json={})
    assert r.status_code == 204
    s = advisory_client.get("/status").json()
    assert s["details"]["drawer"] == "in"


# ---------------------------------------------------------------------------
# Plate / wells
# ---------------------------------------------------------------------------


def test_plate_load_unload_and_well_update(advisory_client) -> None:
    r = advisory_client.post(
        "/control/plate/load",
        json={"plate_id": "P-1", "model": "custom_96"},
    )
    assert r.status_code == 200
    plate = r.json()
    assert plate["plate_id"] == "P-1"
    assert plate["model"] == "custom_96"
    assert len(plate["wells"]) == 96

    r = advisory_client.post(
        "/control/well/update",
        json={"well": "A1", "sample_id": "sample-7", "volume_ul": 125.0},
    )
    assert r.status_code == 200
    well = r.json()
    assert well["well"] == "A1"
    assert well["sample_id"] == "sample-7"
    assert well["volume_ul"] == 125.0

    s = advisory_client.get("/status").json()
    wells = s["details"]["loaded_plate"]["wells"]
    a1 = next(w for w in wells if w["well"] == "A1")
    assert a1["sample_id"] == "sample-7"

    r = advisory_client.post("/control/plate/unload")
    assert r.status_code == 200
    s = advisory_client.get("/status").json()
    assert s["details"]["loaded_plate"] is None


def test_well_update_without_plate_409(advisory_client) -> None:
    # No plate loaded -> LookupError -> 409 Conflict.
    r = advisory_client.post(
        "/control/well/update", json={"well": "A1", "sample_id": "x"}
    )
    assert r.status_code == 409


def test_plate_load_unknown_model_422(advisory_client) -> None:
    r = advisory_client.post(
        "/control/plate/load", json={"plate_id": "P-1", "model": "no_such_plate"}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_read_absorbance(loaded_client) -> None:
    r = loaded_client.post(
        "/control/read/absorbance",
        json={"wells": ["A1", "B2"], "wavelength_nm": 260.0},
    )
    assert r.status_code == 200
    wells = r.json()["wells"]
    assert set(wells) == {"A1", "B2"}
    # Deterministic + non-negative.
    assert wells["A1"] >= 0.0
    assert wells["B2"] >= 0.0
    # The dashboard observes the read counter ticked.
    s = loaded_client.get("/status").json()
    assert s["metrics"]["read_count"]["value"] >= 1


def test_read_fluorescence_distinguishes_args(loaded_client) -> None:
    r1 = loaded_client.post(
        "/control/read/fluorescence",
        json={
            "wells": ["A1"],
            "excitation_nm": 485,
            "emission_nm": 520,
            "focal_height_mm": 7.0,
        },
    ).json()["wells"]["A1"]
    r2 = loaded_client.post(
        "/control/read/fluorescence",
        json={
            "wells": ["A1"],
            "excitation_nm": 485,
            "emission_nm": 520,
            "focal_height_mm": 9.0,  # different focal height -> different value
        },
    ).json()["wells"]["A1"]
    assert r1 != r2


def test_read_luminescence(loaded_client) -> None:
    r = loaded_client.post(
        "/control/read/luminescence",
        json={
            "wells": ["A1", "H12"],
            "integration_time_s": 2.0,
            "focal_height_mm": 7.0,
        },
    )
    assert r.status_code == 200
    assert set(r.json()["wells"]) == {"A1", "H12"}


def test_read_rejects_invalid_wavelength(advisory_client) -> None:
    r = advisory_client.post(
        "/control/read/absorbance",
        json={"wells": ["A1"], "wavelength_nm": 50.0},  # below the 230 nm floor
    )
    assert r.status_code == 422


def test_read_rejects_gain_rather_than_ignoring_it(loaded_client) -> None:
    """PyLabRobot's Cytation backend exposes no gain control on any read.

    Silently dropping the field would hand back a plausible number measured
    at some *other* gain — a wrong result that looks right. Refusing is the
    only safe answer, which is why the read arg models forbid extras.
    """

    r = loaded_client.post(
        "/control/read/fluorescence",
        json={
            "wells": ["A1"],
            "excitation_nm": 485,
            "emission_nm": 520,
            "gain": 100.0,
        },
    )
    assert r.status_code == 422


def test_refusals_do_not_populate_last_error(advisory_client) -> None:
    """§6.3: a refusal is not an operational failure.

    Recording one drives the device to `error` and reddens the dashboard tile
    for something that never broke. Observed live on 2026-08-12: refusing a
    DAPI capture (no filter cube fitted) left a scary `last_error` behind and
    briefly reported `equipment_status: error`.
    """

    before = advisory_client.get("/status").json()
    assert before["last_error"] is None

    # A precondition refusal (no plate) ...
    assert (
        advisory_client.post(
            "/control/read/absorbance",
            json={"wells": ["A1"], "wavelength_nm": 260.0},
        ).status_code
        == 412
    )
    s = advisory_client.get("/status").json()
    assert s["last_error"] is None
    assert s["equipment_status"] != "error"

    # ... and an unavailable-channel refusal.
    advisory_client.post("/control/plate/load", json={"plate_id": "p1"})
    assert (
        advisory_client.post(
            "/control/imaging/capture",
            json={"well": "ZZ", "channel": "brightfield"},
        ).status_code
        == 422
    )
    s = advisory_client.get("/status").json()
    assert s["last_error"] is None
    assert s["equipment_status"] != "error"


def test_read_without_a_plate_is_412_not_500(advisory_client) -> None:
    """§6.1: an inapplicable request is a precondition failure, and the body
    must be branchable by shape rather than by prose."""

    r = advisory_client.post(
        "/control/read/absorbance",
        json={"wells": ["A1"], "wavelength_nm": 260.0},
    )
    assert r.status_code == 412
    detail = r.json()["detail"]
    assert detail["precondition"] == "plate_not_loaded"
    assert detail["required_action"] == "plate.load"


def test_reads_absent_from_allowed_actions_without_a_plate(advisory_client) -> None:
    """§6.2: an action that would 412 must not be advertised."""

    s = advisory_client.get("/status").json()
    assert "read.absorbance" not in s["allowed_actions"]
    assert "imaging.capture" not in s["allowed_actions"]
    # Loading a plate is the way out of that state, so it stays offered.
    assert "plate.load" in s["allowed_actions"]

    advisory_client.post("/control/plate/load", json={"plate_id": "p1"})
    s = advisory_client.get("/status").json()
    assert "read.absorbance" in s["allowed_actions"]
    assert "imaging.capture" in s["allowed_actions"]


def test_read_rejects_empty_well_list(advisory_client) -> None:
    r = advisory_client.post(
        "/control/read/absorbance", json={"wells": [], "wavelength_nm": 260.0}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


def test_imaging_capture(loaded_client) -> None:
    r = loaded_client.post(
        "/control/imaging/capture",
        json={"well": "A1", "channel": "brightfield"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["well"] == "A1"
    assert body["channel"] == "brightfield"
    # The stub returns a synthetic image data URI under details.
    assert "image_data_uri" in body["details"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_shutdown_then_status_is_requires_init(advisory_client) -> None:
    r = advisory_client.post("/control/shutdown")
    assert r.status_code == 204
    s = advisory_client.get("/status").json()
    assert s["equipment_status"] == "requires_init"
    # allowed_actions reflects the new state.
    assert "startup" in s["allowed_actions"]


def test_startup_recovers(advisory_client) -> None:
    advisory_client.post("/control/shutdown")
    r = advisory_client.post("/control/startup")
    assert r.status_code == 204
    s = advisory_client.get("/status").json()
    # Back to dry_run because the underlying reader factory is the stub.
    assert s["equipment_status"] == "dry_run"


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


def test_bare_assertion_from_driver_still_gets_a_message(loaded_client) -> None:
    """PyLabRobot validates instrument replies with bare `assert`, so a
    rejected command arrives as an AssertionError with an empty str().

    Passed through verbatim that becomes `{"detail": ""}` and an empty
    `last_error.message` — the operator learns only that *something* broke.
    Observed live on 2026-08-12 against the real reader.
    """

    service = loaded_client.app.state.service

    async def boom(**kwargs):
        raise AssertionError()  # exactly what biotek_backend.py raises

    service._reader.read_absorbance = boom  # type: ignore[method-assign]

    r = loaded_client.post(
        "/control/read/absorbance",
        json={"wells": ["A1"], "wavelength_nm": 260.0},
    )
    assert r.status_code == 503
    assert r.json()["detail"].strip(), "an empty detail tells the operator nothing"
    assert "rejected the command" in r.json()["detail"]

    s = loaded_client.get("/status").json()
    assert s["last_error"]["message"].strip()
