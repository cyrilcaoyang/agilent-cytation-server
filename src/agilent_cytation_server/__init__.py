"""BioTek (Agilent) Cytation 5 — Python driver + STATUS_SPEC REST API.

This package wraps the PyLabRobot ``PlateReader`` + ``Cytation5Backend``
behind a thin asyncio service and exposes it over HTTP as a STATUS_SPEC
v1.0 read-only device. The driver, service, and FastAPI app live in
sibling modules; nothing is imported eagerly so callers that only want
the data model (e.g. SDK consumers) can do::

    from agilent_cytation_server.models import EquipmentStatus

without pulling in fastapi / uvicorn / pylabrobot.

Conformance: lab status spec v1.0 (read-only). Claim/heartbeat/release
and ``/control/*`` writes graduate to v1.1 in a follow-up release.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
