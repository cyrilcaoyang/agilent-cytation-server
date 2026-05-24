"""Phase 2 tests for the per-well sample tracker.

Covers :class:`PlateStateStore` directly (JSON persistence, validation,
mutation semantics) and the :class:`CytationService` wrappers that
expose it under ``details.loaded_plate``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agilent_cytation_server.models import WellSample
from agilent_cytation_server.plate_state import PlateStateStore, well_ids_96
from agilent_cytation_server.reader import StubCytationReader
from agilent_cytation_server.service import CytationService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# PlateStateStore -- direct
# ---------------------------------------------------------------------------


def test_well_ids_96_layout() -> None:
    ids = well_ids_96()
    assert len(ids) == 96
    assert ids[0] == "A1"
    assert ids[11] == "A12"
    assert ids[12] == "B1"
    assert ids[-1] == "H12"


def test_load_plate_defaults_to_empty_wells(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    plate = store.load_plate(plate_id="P1", model="custom_96")
    assert plate.plate_id == "P1"
    assert plate.model == "custom_96"
    assert len(plate.wells) == 96
    assert plate.wells[0].well == "A1"
    assert plate.wells[0].sample_id is None
    assert plate.wells[0].volume_ul is None


def test_load_plate_rejects_unknown_model(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    with pytest.raises(ValueError, match="Unknown plate model"):
        store.load_plate(plate_id="P1", model="not_a_plate")


def test_load_plate_rejects_duplicate_wells(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    wells = [WellSample(well="A1"), WellSample(well="A1")]
    with pytest.raises(ValueError, match="Duplicate well id"):
        store.load_plate(plate_id="P1", model="custom_96", wells=wells)


def test_load_plate_rejects_invalid_well_id(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    with pytest.raises(ValueError, match="Invalid well id"):
        store.load_plate(
            plate_id="P1",
            model="custom_96",
            wells=[WellSample(well="Z99")],
        )


def test_update_well_mutates_then_persists(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = PlateStateStore(state_path=state_path)
    store.load_plate(plate_id="P1", model="custom_96")
    updated = store.update_well("A1", sample_id="sample-xyz", volume_ul=125.5)
    assert updated.sample_id == "sample-xyz"
    assert updated.volume_ul == 125.5

    # Reload from disk -- changes survive process restart.
    store2 = PlateStateStore(state_path=state_path)
    plate = store2.get()
    assert plate is not None
    a1 = next(w for w in plate.wells if w.well == "A1")
    assert a1.sample_id == "sample-xyz"
    assert a1.volume_ul == 125.5


def test_update_well_clear_flags(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    store.load_plate(plate_id="P1", model="custom_96")
    store.update_well("A1", sample_id="x", notes="hello")
    cleared = store.update_well("A1", clear_sample_id=True, clear_notes=True)
    assert cleared.sample_id is None
    assert cleared.notes is None


def test_update_well_rejects_negative_volume(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    store.load_plate(plate_id="P1", model="custom_96")
    with pytest.raises(ValueError, match="Negative volume"):
        store.update_well("A1", volume_ul=-1.0)


def test_update_well_when_no_plate_raises(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    with pytest.raises(LookupError, match="No plate is currently loaded"):
        store.update_well("A1", sample_id="x")


def test_update_unknown_well_raises(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    store.load_plate(plate_id="P1", model="custom_96")
    # H13 is past column 12; valid wells are A1..H12.
    with pytest.raises(LookupError, match="not in loaded plate"):
        store.update_well("H13", sample_id="x")


def test_unload_returns_previous_and_clears(tmp_path: Path) -> None:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    store.load_plate(plate_id="P1", model="custom_96")
    previous = store.unload_plate()
    assert previous is not None
    assert previous.plate_id == "P1"
    assert store.get() is None


def test_state_path_relative_anchors_to_project_root(tmp_path: Path) -> None:
    # Relative paths should be made absolute (anchored to the project root)
    # so a child process started in a different cwd still finds the same file.
    store = PlateStateStore(state_path="state.json")
    assert store.state_path.is_absolute()


def test_corrupt_state_file_is_ignored(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json!", encoding="utf-8")
    store = PlateStateStore(state_path=state_path)
    assert store.get() is None  # malformed file -> empty state, no crash


def test_persisted_file_format(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = PlateStateStore(state_path=state_path)
    store.load_plate(plate_id="P1", model="custom_96")
    store.update_well("B2", sample_id="s1", volume_ul=42.0)

    body = json.loads(state_path.read_text(encoding="utf-8"))
    assert "plate" in body
    plate_raw = body["plate"]
    assert plate_raw["plate_id"] == "P1"
    assert plate_raw["model"] == "custom_96"
    assert any(
        w["well"] == "B2" and w["sample_id"] == "s1" and w["volume_ul"] == 42.0
        for w in plate_raw["wells"]
    )


# ---------------------------------------------------------------------------
# CytationService -- thin wrappers serialise correctly
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> CytationService:
    store = PlateStateStore(state_path=tmp_path / "state.json")
    svc = CytationService(
        dry_run=False, reader_factory=StubCytationReader, plate_state=store
    )
    _run(svc.startup())
    return svc


def test_service_load_unload_visible_in_status(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    status = _run(svc.get_status())
    assert status.details["loaded_plate"] is None

    _run(svc.load_plate(plate_id="P-001", model="custom_96"))
    status = _run(svc.get_status())
    loaded = status.details["loaded_plate"]
    assert loaded is not None
    assert loaded["plate_id"] == "P-001"
    assert loaded["model"] == "custom_96"
    assert len(loaded["wells"]) == 96

    _run(svc.unload_plate())
    status = _run(svc.get_status())
    assert status.details["loaded_plate"] is None


def test_service_update_well_visible_in_status(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    _run(svc.load_plate(plate_id="P-002", model="custom_96"))
    _run(svc.update_well("A1", sample_id="sample-7", volume_ul=210.0))

    status = _run(svc.get_status())
    wells = status.details["loaded_plate"]["wells"]
    a1 = next(w for w in wells if w["well"] == "A1")
    assert a1["sample_id"] == "sample-7"
    assert a1["volume_ul"] == 210.0


def test_service_uses_default_plate_model(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    plate = _run(svc.load_plate(plate_id="P-003"))
    assert plate.model == svc.default_plate_model
