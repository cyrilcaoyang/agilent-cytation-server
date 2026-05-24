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


def test_read_absorbance(advisory_client) -> None:
    r = advisory_client.post(
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
    s = advisory_client.get("/status").json()
    assert s["metrics"]["read_count"]["value"] >= 1


def test_read_fluorescence_distinguishes_args(advisory_client) -> None:
    r1 = advisory_client.post(
        "/control/read/fluorescence",
        json={
            "wells": ["A1"],
            "excitation_nm": 485,
            "emission_nm": 520,
            "gain": 50.0,
            "focal_height_mm": 7.0,
        },
    ).json()["wells"]["A1"]
    r2 = advisory_client.post(
        "/control/read/fluorescence",
        json={
            "wells": ["A1"],
            "excitation_nm": 485,
            "emission_nm": 520,
            "gain": 100.0,  # different gain -> different value
            "focal_height_mm": 7.0,
        },
    ).json()["wells"]["A1"]
    assert r1 != r2


def test_read_luminescence(advisory_client) -> None:
    r = advisory_client.post(
        "/control/read/luminescence",
        json={"wells": ["A1", "H12"], "integration_time_s": 2.0, "gain": 60.0},
    )
    assert r.status_code == 200
    assert set(r.json()["wells"]) == {"A1", "H12"}


def test_read_rejects_invalid_wavelength(advisory_client) -> None:
    r = advisory_client.post(
        "/control/read/absorbance",
        json={"wells": ["A1"], "wavelength_nm": 50.0},  # below the 200 nm floor
    )
    assert r.status_code == 422


def test_read_rejects_empty_well_list(advisory_client) -> None:
    r = advisory_client.post(
        "/control/read/absorbance", json={"wells": [], "wavelength_nm": 260.0}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


def test_imaging_capture(advisory_client) -> None:
    r = advisory_client.post(
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
