"""Direct unit tests for :class:`CytationService` state-machine transitions.

These exercise the service without a TestClient so we can poke at
``_busy_state``, ``_last_error``, and ``shutdown()`` directly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agilent_cytation_server.models import ErrorInfo
from agilent_cytation_server.reader import StubCytationReader
from agilent_cytation_server.service import CytationService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_service_starts_in_requires_init() -> None:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    status = _run(svc.get_status())
    assert status.equipment_status == "requires_init"
    assert "startup" in status.required_actions


def test_service_dry_run_short_circuits_to_dry_run() -> None:
    svc = CytationService(dry_run=True, reader_factory=StubCytationReader)
    _run(svc.startup())
    status = _run(svc.get_status())
    assert status.equipment_status == "dry_run"
    assert status.details["dry_run"] is True


def test_service_ready_then_busy() -> None:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    _run(svc.startup())
    status = _run(svc.get_status())
    assert status.equipment_status == "ready"

    svc._busy_state = True
    status = _run(svc.get_status())
    assert status.equipment_status == "busy"


def test_service_error_window() -> None:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    _run(svc.startup())
    svc._last_error = ErrorInfo(
        code="ut",
        message="synthetic error",
        severity="error",
        timestamp=datetime.now(timezone.utc),
    )
    status = _run(svc.get_status())
    assert status.equipment_status == "error"
    assert status.message == "synthetic error"


def test_service_shutdown_returns_to_requires_init() -> None:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    _run(svc.startup())
    assert _run(svc.get_status()).equipment_status == "ready"
    _run(svc.shutdown())
    assert _run(svc.get_status()).equipment_status == "requires_init"


def test_imaging_component_toggle() -> None:
    svc = CytationService(dry_run=False, reader_factory=StubCytationReader)
    _run(svc.startup())
    svc.imaging_enabled = True
    assert "imaging" in _run(svc.get_status()).components
    svc.imaging_enabled = False
    assert "imaging" not in _run(svc.get_status()).components


@pytest.mark.parametrize("dry_run", [True, False])
def test_status_is_always_http200_shape(dry_run: bool) -> None:
    """Spec rule: /status always 200 unless the process is broken.

    The service-level guarantee mirrors that: get_status() must never
    raise once the service is constructed, regardless of whether the
    underlying reader is connected.
    """
    svc = CytationService(dry_run=dry_run, reader_factory=StubCytationReader)
    status = _run(svc.get_status())
    assert status.equipment_id == "cytation_5"
    assert status.equipment_kind == "plate_reader"


class _FailingSetupReader(StubCytationReader):
    """A reader whose ``setup()`` opens the link and then fails.

    Models the real failure: ``BioTekPlateReaderBackend.setup`` opens the
    USB handle *before* the first command times out, so a raising ``setup()``
    still leaves the device claimed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.stopped = False

    async def setup(self) -> None:  # type: ignore[override]
        self.opened = True
        raise TimeoutError("Timeout while waiting for response")

    async def stop(self) -> None:  # type: ignore[override]
        self.stopped = True


def test_failed_startup_releases_the_reader() -> None:
    """A failed ``setup()`` must not strand the USB handle.

    Regression for 2026-08-25: ``startup()`` recorded the error and re-raised
    without closing the half-open reader, so the handle stayed open for the
    life of the process. The device then enumerated with a blank identity and
    every later ``POST /control/startup`` failed the same way — recoverable
    only by restarting the service, which needs an administrator.
    """
    made: list[_FailingSetupReader] = []

    def factory() -> _FailingSetupReader:
        made.append(_FailingSetupReader())
        return made[-1]

    svc = CytationService(dry_run=False, reader_factory=factory)

    with pytest.raises(TimeoutError):
        _run(svc.startup())

    assert made[0].stopped, "failed setup() left the link open"
    assert svc._reader is None, "half-open reader was kept on the service"

    # The whole point: the next attempt gets a clean open rather than
    # inheriting the previous failure.
    with pytest.raises(TimeoutError):
        _run(svc.startup())
    assert len(made) == 2, "retry reused the dead reader instead of remaking it"
