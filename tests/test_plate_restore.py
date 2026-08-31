"""Re-assigning the persisted plate at startup (option A, decided 2026-08-31).

The store survives a restart; the reader's PyLabRobot `Plate` resource does
not. Without this the envelope contradicts itself — `details.loaded_plate`
names a plate while `plate_in_reader` is false and every optical action is
withheld — and the obvious operator fix, re-POSTing `plate.load` with just a
plate_id, replaces the wells with 96 empty ones and destroys the sample
metadata.

The restore trades that away for an assertion: nothing on this instrument
reports whether a plate is physically present, so the service is now
trusting a file about the state of the world. `plate_restored_at_startup`
is what keeps that trade visible rather than silent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agilent_cytation_server.plate_state import PlateStateStore, WellSample
from agilent_cytation_server.reader import StubCytationReader
from agilent_cytation_server.service import CytationService


def _service(tmp_path: Path) -> CytationService:
    return CytationService(
        dry_run=False,
        reader_factory=StubCytationReader,
        plate_state=PlateStateStore(state_path=tmp_path / "state.json"),
    )


def _restarted(tmp_path: Path) -> CytationService:
    """A second service over the same state.json — what a restart looks like."""
    svc = _service(tmp_path)
    asyncio.run(svc.startup())
    return svc


def test_a_restart_puts_the_plate_back(tmp_path: Path) -> None:
    first = _service(tmp_path)
    asyncio.run(first.startup())
    asyncio.run(
        first.load_plate(
            plate_id="P-1",
            model="custom_96",
            wells=[WellSample(well="D3", sample_id="kno3", volume_ul=100.0)],
        )
    )
    asyncio.run(first.shutdown())

    svc = _restarted(tmp_path)
    status = asyncio.run(svc.get_status())

    assert status.details["plate_in_reader"] is True
    assert "read.absorbance" in status.allowed_actions


def test_the_wells_survive_the_restart(tmp_path: Path) -> None:
    """The point of the whole exercise: the sample map is what was at risk."""

    first = _service(tmp_path)
    asyncio.run(first.startup())
    asyncio.run(
        first.load_plate(
            plate_id="P-1",
            model="custom_96",
            wells=[
                WellSample(well="D3", sample_id="kno3", volume_ul=100.0),
                WellSample(well="F6", sample_id="cuso4_5h2o", volume_ul=100.0),
            ],
        )
    )
    asyncio.run(first.shutdown())

    status = asyncio.run(_restarted(tmp_path).get_status())

    wells = status.details["loaded_plate"]["wells"]
    assert {w["well"]: w["sample_id"] for w in wells} == {
        "D3": "kno3",
        "F6": "cuso4_5h2o",
    }


def test_a_restored_plate_says_so(tmp_path: Path) -> None:
    """Asserted from disk is not the same as observed by a person.

    The instrument cannot report physical presence, so anything reasoning
    about a restored plate has to be able to tell it apart from one an
    operator loaded while standing at the machine.
    """

    first = _service(tmp_path)
    asyncio.run(first.startup())
    asyncio.run(first.load_plate(plate_id="P-1", model="custom_96"))
    # An operator did this one, so it is observed.
    assert asyncio.run(first.get_status()).details["plate_restored_at_startup"] is False
    asyncio.run(first.shutdown())

    status = asyncio.run(_restarted(tmp_path).get_status())
    assert status.details["plate_restored_at_startup"] is True


def test_an_operator_load_supersedes_the_assertion(tmp_path: Path) -> None:
    svc = _restarted_with_plate(tmp_path)
    assert asyncio.run(svc.get_status()).details["plate_restored_at_startup"] is True

    asyncio.run(svc.load_plate(plate_id="P-1", model="custom_96"))

    assert asyncio.run(svc.get_status()).details["plate_restored_at_startup"] is False


def _restarted_with_plate(tmp_path: Path) -> CytationService:
    first = _service(tmp_path)
    asyncio.run(first.startup())
    asyncio.run(first.load_plate(plate_id="P-1", model="custom_96"))
    asyncio.run(first.shutdown())
    return _restarted(tmp_path)


def test_nothing_to_restore_is_not_an_assertion(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    asyncio.run(svc.startup())

    status = asyncio.run(svc.get_status())
    assert status.details["plate_restored_at_startup"] is False
    assert status.details["plate_in_reader"] is False


def test_a_failed_restore_does_not_fail_startup(tmp_path: Path, monkeypatch) -> None:
    """A healthy connect must not be turned into a failed one.

    The fallback is exactly the old behaviour — no plate in the reader,
    optical actions withheld — which one `plate.load` recovers.
    """

    first = _service(tmp_path)
    asyncio.run(first.startup())
    asyncio.run(first.load_plate(plate_id="P-1", model="custom_96"))
    asyncio.run(first.shutdown())

    def _boom(*args, **kwargs):
        raise RuntimeError("reader refused the plate")

    monkeypatch.setattr(StubCytationReader, "load_plate", _boom)

    svc = _service(tmp_path)
    asyncio.run(svc.startup())  # must not raise

    status = asyncio.run(svc.get_status())
    assert status.equipment_status != "requires_init"
    assert status.details["plate_in_reader"] is False
    assert status.details["plate_restored_at_startup"] is False
    assert "read.absorbance" not in status.allowed_actions
