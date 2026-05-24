"""Request/response models for the v1.1 ``/control/*`` surface.

Kept separate from :mod:`agilent_cytation_server.models` so the
spec-mandated envelope models stay tightly scoped to STATUS_SPEC. The
shapes here are owned by *this* repo (until a future SDK-side catalog
graduates them to a shared package).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import WellSample


# ---------------------------------------------------------------------------
# Plate management
# ---------------------------------------------------------------------------


class PlateLoadArgs(BaseModel):
    plate_id: str = Field(..., min_length=1, max_length=128)
    model: str | None = None  # defaults to [plates].default_model
    wells: list[WellSample] | None = None  # defaults to 96 empty wells


class WellUpdateArgs(BaseModel):
    well: str = Field(..., min_length=2, max_length=3, description="e.g. A1, H12")
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None
    clear_sample_id: bool = False
    clear_notes: bool = False


# ---------------------------------------------------------------------------
# Drawer
# ---------------------------------------------------------------------------


class DrawerArgs(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class AbsorbanceArgs(BaseModel):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    wavelength_nm: float = Field(..., ge=200.0, le=999.0)


class FluorescenceArgs(BaseModel):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    excitation_nm: float = Field(..., ge=200.0, le=999.0)
    emission_nm: float = Field(..., ge=200.0, le=999.0)
    gain: float = Field(default=50.0, ge=0.0, le=255.0)
    focal_height_mm: float = Field(default=7.0, ge=0.0, le=30.0)


class LuminescenceArgs(BaseModel):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    integration_time_s: float = Field(default=1.0, ge=0.1, le=60.0)
    gain: float = Field(default=50.0, ge=0.0, le=255.0)


class ReadResponse(BaseModel):
    wells: dict[str, float]


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


class ImagingCaptureArgs(BaseModel):
    well: str = Field(..., min_length=2, max_length=3)
    channel: str = Field(
        ...,
        description="Channel id: brightfield, phase_contrast, dapi, gfp, rfp, cy5.",
    )
    focal_height_mm: float = Field(default=5.0, ge=0.0, le=30.0)
    exposure_ms: float = Field(default=10.0, ge=0.01, le=10_000.0)
    gain: float = Field(default=1.0, ge=0.0, le=255.0)


class ImagingCaptureResponse(BaseModel):
    well: str
    channel: str
    focal_height_mm: float
    exposure_ms: float
    gain: float
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "PlateLoadArgs",
    "WellUpdateArgs",
    "DrawerArgs",
    "AbsorbanceArgs",
    "FluorescenceArgs",
    "LuminescenceArgs",
    "ReadResponse",
    "ImagingCaptureArgs",
    "ImagingCaptureResponse",
]
