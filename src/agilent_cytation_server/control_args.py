"""Request/response models for the v1.1 ``/control/*`` surface.

Kept separate from :mod:`agilent_cytation_server.models` so the
spec-mandated envelope models stay tightly scoped to STATUS_SPEC. The
shapes here are owned by *this* repo (until a future SDK-side catalog
graduates them to a shared package).
"""

from __future__ import annotations

from typing import Any, Literal

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


# Bounds below mirror the limits PyLabRobot's BioTek backend enforces
# itself (`abs_wavelength_range`, `excitation_range`, `emission_range`,
# `focal_height_range`). Duplicating them here is deliberate: it converts
# what would be a 500 from deep inside the driver into a 422 that names the
# offending field. Widen them only alongside the driver.
_ABS_NM = (230.0, 999.0)
_EX_NM = (250.0, 700.0)
_EM_NM = (250.0, 700.0)
_FOCAL_MM = (4.5, 13.88)


class _StrictArgs(BaseModel):
    """Reject unknown fields rather than ignoring them.

    Pydantic's default is to drop extras silently, which for this device
    would mean a caller passing `gain` to a read gets a *plausible number
    measured at some other gain* — a wrong result that looks right. PLR's
    Cytation backend exposes no gain control on any read, so the only safe
    response is to refuse the request.
    """

    model_config = {"extra": "forbid"}


class AbsorbanceArgs(_StrictArgs):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    wavelength_nm: float = Field(..., ge=_ABS_NM[0], le=_ABS_NM[1])


class FluorescenceArgs(_StrictArgs):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    excitation_nm: float = Field(..., ge=_EX_NM[0], le=_EX_NM[1])
    emission_nm: float = Field(..., ge=_EM_NM[0], le=_EM_NM[1])
    focal_height_mm: float = Field(default=7.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])


class LuminescenceArgs(_StrictArgs):
    wells: list[str] = Field(..., min_length=1, max_length=96)
    # Required by the driver for luminescence, not optional as the previous
    # shape implied.
    focal_height_mm: float = Field(default=7.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])
    integration_time_s: float = Field(default=1.0, ge=0.1, le=60.0)


class ReadResponse(BaseModel):
    wells: dict[str, float]


# ---------------------------------------------------------------------------
# Incubator + shaker
# ---------------------------------------------------------------------------


class TemperatureArgs(_StrictArgs):
    # Bounds are the driver's (4-45 C). Note the driver assumes every Cytation
    # can cool, which is what produces the 4 C floor — a low setpoint may be
    # accepted and then ignored by a unit with no cooling fitted.
    celsius: float = Field(..., ge=4.0, le=45.0)


class ShakeArgs(_StrictArgs):
    pattern: Literal["orbital", "linear"] = "orbital"
    # PyLabRobot calls this `frequency`, but it is the orbit displacement in
    # mm and runs *inversely* to speed: 6 mm is ~360 CPM, 1 mm is ~1096 CPM.
    # Renamed here so a caller cannot read it as "shake faster with a bigger
    # number".
    displacement_mm: int = Field(default=3, ge=1, le=6)


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


class ImagingCaptureArgs(_StrictArgs):
    well: str = Field(..., min_length=2, max_length=3)
    channel: str = Field(
        ...,
        description=(
            "Channel id: brightfield, phase_contrast, dapi, gfp, rfp, cy5, "
            "texas_red, cfp, yfp. Fluorescence channels require the matching "
            "filter cube; see details.imaging.installed_filters on /status."
        ),
    )
    focal_height_mm: float = Field(default=5.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])
    exposure_ms: float = Field(default=10.0, ge=0.01, le=10_000.0)
    # Camera analog gain in dB (a Spinnaker camera setting) — unrelated to the
    # PMT gain the reads do not expose.
    gain: float = Field(default=0.0, ge=0.0, le=47.0)
    objective: str | None = Field(
        default=None,
        description=(
            "PyLabRobot Objective name, e.g. O_4X_PL_FL. Defaults to the "
            "lowest-magnification installed objective (widest field)."
        ),
    )
    led_intensity: int = Field(default=10, ge=1, le=10)
    autofocus: bool = Field(
        default=False,
        description=(
            "Search focal height for maximum sharpness before capturing. "
            "Costs extra exposures on the sample."
        ),
    )
    auto_exposure: bool = Field(
        default=False,
        description=(
            "Search exposure until the peak pixel sits near 80% of full "
            "scale. Costs extra exposures on the sample."
        ),
    )


class ImagingCaptureResponse(BaseModel):
    well: str
    channel: str
    focal_height_mm: float
    exposure_ms: float
    gain: float
    objective: str | None = None
    image_path: str | None = None
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
    "ShakeArgs",
    "TemperatureArgs",
]
