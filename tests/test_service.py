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
