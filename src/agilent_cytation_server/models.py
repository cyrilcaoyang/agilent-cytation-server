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


# Incubator limits, read from the instrument itself rather than assumed.
#
# PyLabRobot hardcodes (4.0, 45.0) for every Cytation: `supports_cooling` is a
# property returning True unconditionally, which manufactures the 4 C floor,
# and 45.0 is commented `# default BioTek max`. Neither is a hardware query.
#
# Queried over Gen5 on 2026-08-23 (unit serial 23030927, `GetReaderCharacteristics`):
#   eTemperatureControlOption = True
#   eTemperatureMin           = 18
#   eTemperatureMax           = 65
#   eTemperatureGradientMax   = 2   (spatial lid gradient, not a ramp in time)
#
# Note 18 C is the *declared* floor; whether this unit can actually hold a
# setpoint below ambient is still unverified - BioTek incubators are commonly
# ambient+4 upward, and no low setpoint has ever been commanded on this one.
TEMPERATURE_MIN_C = 18.0
TEMPERATURE_MAX_C = 65.0


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
