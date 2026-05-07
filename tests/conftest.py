"""Shared pytest fixtures.

The API tests run the FastAPI app with ``dry_run=True`` so no Windows /
PyLabRobot / pyusb dependencies are required. ``conftest.py`` keeps that
switch out of every individual test.

Phase 1 has no claim protocol and no /control/* surface, so a single
unauthenticated TestClient is enough; the v1.1 graduation will add
``unclaimed_client`` / ``advisory_client`` fixtures mirroring
``agilent_plateloc/tests/conftest.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agilent_cytation_server.api import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A `TestClient` whose lifespan auto-connects the dry-run stub."""
    app = create_app(dry_run=True)
    with TestClient(app) as c:
        yield c
