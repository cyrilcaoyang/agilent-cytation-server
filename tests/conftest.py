"""Shared pytest fixtures.

The API tests run the FastAPI app with ``dry_run=True`` so no Windows /
PyLabRobot / pyusb dependencies are required. ``conftest.py`` keeps that
switch out of every individual test.

Phase 2 added per-well sample tracking persisted to a JSON file, so we
also give every test its own ``tmp_path``-scoped ``PlateStateStore`` —
otherwise the on-disk state bleeds across tests in the same pytest
session.

Phase 3 added the cooperative claim protocol. Most tests use the
``advisory_client`` fixture (claims disabled) so they can call
``/control/*`` without the X-Claim-Token dance; tests that exercise
the claim protocol itself use the ``client`` fixture (enforced).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agilent_cytation_server.api import create_app
from agilent_cytation_server.claims import ClaimManager
from agilent_cytation_server.plate_state import PlateStateStore


@pytest.fixture
def plate_state(tmp_path: Path) -> PlateStateStore:
    """Isolated PlateStateStore per test, backed by ``tmp_path/state.json``."""
    return PlateStateStore(state_path=tmp_path / "state.json")


@pytest.fixture
def client(plate_state: PlateStateStore) -> Iterator[TestClient]:
    """A `TestClient` whose lifespan auto-connects the dry-run stub.

    Claims are *enforced* on this client — Phase 3 claim-protocol tests
    use it. Uses an isolated ``PlateStateStore`` so plate.load /
    plate.unload side effects do not leak between tests.
    """
    app = create_app(dry_run=True, plate_state=plate_state)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def advisory_client(plate_state: PlateStateStore) -> Iterator[TestClient]:
    """A `TestClient` with claims set to advisory (not enforced).

    Useful for tests that only care about the ``/control/*`` verbs and
    don't want to claim/release on every call.
    """
    app = create_app(
        dry_run=True,
        plate_state=plate_state,
        claim_manager=ClaimManager(enforce=False),
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def loaded_client(advisory_client: TestClient) -> TestClient:
    """An advisory client with a plate already loaded.

    Reads and captures are gated on a plate being resident in the reader —
    PyLabRobot addresses wells through the ``Plate`` resource and raises
    ``NoPlateError`` without one — so every optical test needs this. Kept as
    a fixture so the precondition is stated once, and so the tests that
    assert the *refusal* can still use the bare ``advisory_client``.
    """

    r = advisory_client.post("/control/plate/load", json={"plate_id": "test_plate"})
    assert r.status_code == 200, r.text
    return advisory_client
