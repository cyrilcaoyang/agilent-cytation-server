"""STATUS_SPEC v1.2 conformance: observed `activity`, spans, `cycles_total`.

v1.2's premise is that health and activity are *independent* answers (§2.3).
The first migration bolted `activity` on as a function of `equipment_status`
— which §2.3 explicitly forbids, because it adds no information — stamped
`activity_since` with the poll instant, and reported `requires_init` +
`unknown`, a violation of the invariant table. These tests pin the corrected
behaviour, plus the property that makes any of it observable at all: a status
poll must stay answerable while an operation is in flight.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agilent_cytation_server.models import EquipmentStatus
from agilent_cytation_server.reader import StubCytationReader
from agilent_cytation_server.service import CytationService

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# §2.3's invariant table, for the states this device reaches. `error`,
# `dry_run` and `unknown` accept any activity.
_INVARIANTS = {
    "busy": "running",
    "ready": "idle",
    "requires_init": "idle",
}

# Anything that would start a second concurrent operation.
_OPERATION_ACTIONS = {
    "drawer.open",
    "drawer.close",
    "read.absorbance",
    "read.fluorescence",
    "read.luminescence",
    "imaging.capture",
}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _started_service(*, with_plate: bool = False) -> CytationService:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    _run(svc.startup())
    if with_plate:
        # Loaded straight onto the reader rather than through
        # `svc.load_plate`, which would persist to the PlateStateStore and so
        # write this repo's real state.json. The read precondition is
        # reader-side anyway, so this is the state under test.
        svc._reader.load_plate(plate_id="test_plate")  # type: ignore[union-attr]
    return svc


def test_requires_init_is_idle_not_unknown() -> None:
    """The invariant table requires `requires_init` ⇒ `idle`. With no session
    open, "not performing its primary operation" is a certainty, not an
    unanswerable question — `unknown` is the answer of last resort (§2.1)."""

    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)

    status = _run(svc.get_status())

    assert status.equipment_status == "requires_init"
    assert status.activity == "idle"
    assert status.activity_since is not None


@pytest.mark.parametrize("busy", [False, True])
def test_activity_is_observed_not_derived(busy: bool) -> None:
    """`activity` must come from the hardware flag every operation sets, not
    from `equipment_status` (§2.3)."""

    svc = _started_service()
    svc._busy_state = busy

    status = _run(svc.get_status())

    assert status.activity == ("running" if busy else "idle")
    assert _INVARIANTS[status.equipment_status] == status.activity


def test_degraded_reader_still_reports_real_activity() -> None:
    """The motivating v1.2 case: a subsystem fault must not erase the activity
    answer. The old code reported `unknown` for anything outside ready/busy."""

    svc = _started_service()

    def broken_temperature():
        raise RuntimeError("sensor offline")

    svc._reader.get_temperature = broken_temperature  # type: ignore[method-assign]
    svc._readings_at = None  # force the cache to refresh through the failure

    status = _run(svc.get_status())

    assert status.equipment_status == "degraded"
    assert status.activity == "idle"


def test_activity_since_is_the_span_start_not_the_poll() -> None:
    svc = _started_service()

    first = _run(svc.get_status())
    second = _run(svc.get_status())

    assert first.activity == second.activity == "idle"
    # Previously this re-stamped every poll, so every span looked zero-length.
    assert first.activity_since == second.activity_since


def test_activity_since_restamps_on_transition() -> None:
    svc = _started_service()
    idle_since = _run(svc.get_status()).activity_since

    async def slow_read(**kwargs):
        # Windows' wall clock ticks at ~15.6 ms; a stub read returns inside a
        # single tick, which would make the new span indistinguishable from
        # the old one for reasons that have nothing to do with the contract.
        await asyncio.sleep(0.05)
        return {"A1": 0.5}

    svc._reader.read_absorbance = slow_read  # type: ignore[method-assign]

    _run(svc.read_absorbance(wells=["A1"], wavelength_nm=600.0))
    after = _run(svc.get_status())

    # Back to idle, but a *new* idle span that started when the read ended.
    assert after.activity == "idle"
    assert after.activity_since > idle_since


def test_status_stays_answerable_while_an_operation_runs() -> None:
    """The property everything else depends on.

    `/status` used to take the same lock every operation holds, so a poll
    returned only after the read finished — with `_busy_state` already back to
    False. `busy` and `activity: "running"` were therefore unobservable from
    outside, which defeats the purpose of the field.
    """

    async def scenario():
        svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
        await svc.startup()

        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_read(**kwargs):
            entered.set()
            await release.wait()
            return {"A1": 0.5}

        svc._reader.read_absorbance = slow_read  # type: ignore[method-assign]

        task = asyncio.create_task(
            svc.read_absorbance(wells=["A1"], wavelength_nm=600.0)
        )
        await entered.wait()

        # Mid-operation poll: must return promptly, not block on the read.
        status = await asyncio.wait_for(svc.get_status(), timeout=1.0)

        release.set()
        await task
        return status

    status = _run(scenario())

    assert status.equipment_status == "busy"
    assert status.activity == "running"
    # §2.3: no second concurrent operation may be advertised.
    assert not _OPERATION_ACTIONS.intersection(status.allowed_actions)


def test_cycles_total_counts_reads_and_captures() -> None:
    """A read finishes well inside the dashboard's 60 s poll, so the counter —
    not a sampled activity series — is what makes the work accountable
    (§2.3.1)."""

    svc = _started_service(with_plate=True)
    assert _run(svc.get_status()).metrics["cycles_total"].value == 0

    _run(svc.read_absorbance(wells=["A1"], wavelength_nm=600.0))
    _run(svc.read_luminescence(wells=["A1"]))
    _run(svc.capture_image(well="A1", channel="brightfield"))

    metrics = _run(svc.get_status()).metrics
    assert metrics["cycles_total"].value == 3
    assert metrics["cycles_total"].unit == "count"
    # The measurement-only legacy counter stays measurement-only.
    assert metrics["read_count"].value == 2


def test_cycles_total_does_not_count_a_failed_read() -> None:
    svc = _started_service()

    async def boom(**kwargs):
        raise RuntimeError("lamp failure")

    svc._reader.read_absorbance = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        _run(svc.read_absorbance(wells=["A1"], wavelength_nm=600.0))

    status = _run(svc.get_status())
    assert status.metrics["cycles_total"].value == 0
    # The failure ends the span and is reported as a fault, not as work.
    assert status.activity == "idle"
    assert status.equipment_status == "error"


def test_drawer_move_is_running_but_not_a_cycle() -> None:
    """A drawer move is a commanded operation (so `running`, and `busy` ⇒
    `running` holds) but it is stage motion, not a measurement."""

    svc = _started_service()

    _run(svc.open_drawer())

    status = _run(svc.get_status())
    assert status.activity == "idle"
    assert status.metrics["cycles_total"].value == 0
    assert status.details["drawer"] == "out"


@pytest.mark.parametrize(
    "name",
    ["status_ready.json", "status_busy.json", "status_requires_init.json", "status_dry_run.json"],
)
def test_fixtures_are_v1_2_shaped(name: str) -> None:
    status = EquipmentStatus(**json.loads((_FIXTURES / name).read_text()))

    assert status.protocol_version == "1.2"
    assert status.activity_since is not None

    required = _INVARIANTS.get(status.equipment_status)
    if required is not None:
        assert status.activity == required
    if status.activity == "running":
        assert not _OPERATION_ACTIONS.intersection(status.allowed_actions)
    if status.equipment_status != "requires_init":
        assert status.metrics["cycles_total"].unit == "count"
