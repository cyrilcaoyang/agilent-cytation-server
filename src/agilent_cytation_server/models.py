"""STATUS_SPEC v1.2 Pydantic models for agilent-cytation-server.

Wire-contract types are imported from the shared ``sdl-lab-contract`` package
and re-exported. Device-specific models (``WellSample``, ``LoadedPlate``) remain
local.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sdl_lab_contract import (
    ClaimedBy,
    ClaimRejection,
    ClaimRequest,
    ClaimResponse,
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    ErrorSeverity,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)

PROTOCOL_VERSION = "1.2"


# ---------------------------------------------------------------------------
# Phase 2: per-well sample tracking models
# ---------------------------------------------------------------------------

WellId = str  # "A1" .. "H12"


class WellSample(BaseModel):
    """One well of the currently-loaded plate."""

    well: WellId
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None


class LoadedPlate(BaseModel):
    """The plate currently sitting on the Cytation's stage."""

    plate_id: str
    model: str
    loaded_at: datetime
    wells: list[WellSample] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 (v1.1): cooperative claim protocol — bodies already in contract
# ---------------------------------------------------------------------------


__all__ = [
    # Re-exported from sdl_lab_contract
    "PROTOCOL_VERSION",
    "ClaimedBy",
    "ClaimRejection",
    "ClaimRequest",
    "ClaimResponse",
    "ComponentStatus",
    "EquipmentKind",
    "EquipmentState",
    "EquipmentStatus",
    "ErrorInfo",
    "ErrorSeverity",
    "HealthResponse",
    "MetricValue",
    "ProbeResponse",
    # Local
    "WellId",
    "WellSample",
    "LoadedPlate",
]
