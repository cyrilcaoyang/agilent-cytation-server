"""Lab equipment status spec v1.1.

Verbatim copy of the unified status contract defined in the
ac-organic-lab monorepo (``docs/STATUS_SPEC.md``). MUST stay in sync
with that document until a shared ``lab-status-contract`` Python
package is published; once it is, replace this file with::

    from lab_status_contract import (
        EquipmentStatus, ProbeResponse, HealthResponse, ...
    )

Conformance: agilent-cytation-server REST API conforms to lab status
spec v1.1 — cooperative claims (``POST /control/{claim,heartbeat,
release}``) plus ``/control/*`` writes for drawer, reads, plate
load/unload, and imaging capture. ``allowed_actions`` is populated
from the current state machine; ``details.claimed_by`` is populated
while a claim is held. v1.0 is a subset and remains valid for
read-only consumers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.1"


EquipmentKind = Literal[
    "solid_doser",
    "liquid_handler",
    "press",
    "fume_hood",
    "robot_arm",
    "environmental_sensor",
    "hplc",
    "plate_reader",
    "plate_sealer",
    "plate_stacker",
    "other",
]

EquipmentState = Literal[
    "ready",          # initialized, idle, can accept commands
    "busy",           # performing an operation
    "requires_init",  # service up but hardware not initialized
    "degraded",       # running but a sub-component is unhealthy
    "dry_run",        # simulation mode, no hardware connected
    "error",          # hardware reported an error
    "e_stop",         # emergency stopped
    "unknown",        # state cannot be determined
]

ErrorSeverity = Literal["info", "warning", "error", "critical"]


class ComponentStatus(BaseModel):
    connected: bool
    state: str
    message: str | None = None
    last_event_at: datetime | None = None


class MetricValue(BaseModel):
    value: float | int | str | bool
    unit: str | None = None
    timestamp: datetime | None = None


class ErrorInfo(BaseModel):
    code: str | None = None
    message: str
    severity: ErrorSeverity
    timestamp: datetime


class EquipmentStatus(BaseModel):
    """Unified equipment status envelope (spec v1.0).

    The :attr:`allowed_actions` field is an optional v1.1 forward-compat
    hook: a v1.0 device may leave it empty (the SDK then falls back to
    catalog-declared ``requires_states``); a future v1.1 migration will
    populate it with skill names the device will currently honor on
    ``/control/*``.
    """

    protocol_version: str = PROTOCOL_VERSION

    # Identity
    equipment_id: str
    equipment_name: str
    equipment_kind: EquipmentKind
    equipment_version: str | None = None
    host: str | None = None  # local hostname only

    # Operational state
    equipment_status: EquipmentState
    message: str | None = None
    required_actions: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)

    # Timing
    device_time: datetime
    uptime_seconds: float | None = None

    # Sub-equipment / measurements
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    last_error: ErrorInfo | None = None

    # Free-form per-equipment data; safe to display in a debug/details panel.
    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResponse(BaseModel):
    """Body of ``GET /`` -- the cheapest possible identity probe."""

    equipment_id: str
    equipment_name: str
    protocol_version: str = PROTOCOL_VERSION


class HealthResponse(BaseModel):
    """Body of ``GET /health`` -- service liveness."""

    status: Literal["healthy"] = "healthy"


# ---------------------------------------------------------------------------
# Phase 2: per-well sample tracking models
#
# Surfaced under ``EquipmentStatus.details.loaded_plate`` so they ride
# the v1.0 envelope without requiring a schema bump. The orchestrator
# owns ``sample_id`` (workflows assign it on plate.load); the device
# is the source of truth for ``volume_ul`` (mutated by reads / dosing).
# ---------------------------------------------------------------------------


WellId = str  # "A1" .. "H12"


class WellSample(BaseModel):
    """One well of the currently-loaded plate."""

    well: WellId
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None


class LoadedPlate(BaseModel):
    """The plate currently sitting on the Cytation's stage.

    ``model`` is one of the keys in ``[plates.*]`` in ``config.toml`` /
    ``agilent_cytation_server.plates.PLATE_FACTORIES``. ``plate_id`` is
    a free-form orchestrator-assigned identifier (typically a barcode
    or run-prefixed UUID).
    """

    plate_id: str
    model: str
    loaded_at: datetime
    wells: list[WellSample] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 (v1.1): cooperative claim protocol
#
# Cooperative, not authenticated. Any client could ignore the protocol;
# this server enforces it by checking X-Claim-Token on /control/* and
# rejecting mismatches with HTTP 423 Locked.
# ---------------------------------------------------------------------------


class ClaimedBy(BaseModel):
    """Identity of the current claim holder. Surfaced on /status under
    ``details.claimed_by`` so every reader sees who controls the device."""

    session_id: str
    owner: str
    expires_at: datetime


class ClaimRequest(BaseModel):
    owner: str = Field(..., min_length=1, max_length=128)
    session_id: str = Field(..., min_length=1, max_length=128)
    ttl_s: float = Field(default=30.0, ge=1.0, le=600.0)


class ClaimResponse(BaseModel):
    claim_token: str
    heartbeat_interval_s: float
    expires_at: datetime


class ClaimRejection(BaseModel):
    detail: str
    claimed_by: ClaimedBy | None = None
    retry_after_s: float | None = None


__all__ = [
    "PROTOCOL_VERSION",
    "EquipmentKind",
    "EquipmentState",
    "ErrorSeverity",
    "ComponentStatus",
    "MetricValue",
    "ErrorInfo",
    "EquipmentStatus",
    "ProbeResponse",
    "HealthResponse",
    "WellId",
    "WellSample",
    "LoadedPlate",
    "ClaimedBy",
    "ClaimRequest",
    "ClaimResponse",
    "ClaimRejection",
]
