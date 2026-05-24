# Phase 4 — Skill catalog handoff

This is a **drop-in patch** for the central server's
[`ac-organic-lab`](https://github.com/cyrilcaoyang/ac-organic-lab)
monorepo. The Cytation device PC carries a read-only mirror of that
repo, so the changes here cannot be committed from this PC — they
need to be applied from the central server (or via a PR upstream).

The patch has three parts, all small:

1. A new file `skills/src/lab_skills/skill_catalog/plate_reader.py`
   that declares one `SkillDef` per `/control/*` verb the device now
   exposes.
2. A one-line registration in
   `skills/src/lab_skills/skill_catalog/__init__.py`.
3. A `protocol: "1.0"` → `protocol: "1.1"` flip in
   `equipment.yaml`, plus removal of `do_not_call_connect: true`.

After applying, restart the dashboard API
(`sudo systemctl restart ac-organic-lab-api.service`) and confirm
`await session.role("plate_reader").read_absorbance(...)` resolves
on a workflow against the Cytation. Hardware verification per
`RUNBOOK.md` §3-§4 in this repo should land **first**, because the
SDK will start calling real `/control/*` on the device the moment
`do_not_call_connect` is removed.

---

## 1) New file: `skills/src/lab_skills/skill_catalog/plate_reader.py`

```python
"""Skill catalog entries for ``kind=plate_reader``.

Reference device: :mod:`agilent_cytation_server` — implements STATUS_SPEC
v1.1 and exposes the full ``/control/*`` write surface (claim protocol,
drawer, plate management, three read types, imaging). Endpoint paths and
arg ranges mirror the device's Pydantic ``Field(ge=, le=)`` constraints
in ``agilent_cytation_server/control_args.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import SkillDef
from .registry import register


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class StartupArgs(BaseModel):
    """Body for ``POST /control/startup`` (no parameters)."""


class ShutdownArgs(BaseModel):
    """Body for ``POST /control/shutdown`` (no parameters)."""


# ---------------------------------------------------------------------------
# Drawer
# ---------------------------------------------------------------------------


class DrawerArgs(BaseModel):
    """Body for ``POST /control/drawer/{open,close}`` (no parameters)."""


# ---------------------------------------------------------------------------
# Plate / well sample tracking (Phase 2)
# ---------------------------------------------------------------------------


class WellSample(BaseModel):
    """One well of the currently-loaded plate. Mirrors the device-side
    ``agilent_cytation_server.models.WellSample``.
    """

    well: str
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None


class PlateLoadArgs(BaseModel):
    """Body for ``POST /control/plate/load``."""

    plate_id: str = Field(..., min_length=1, max_length=128)
    model: str | None = None  # defaults to device-configured default_model
    wells: list[WellSample] | None = None  # defaults to 96 empty wells


class PlateUnloadArgs(BaseModel):
    """Body for ``POST /control/plate/unload`` (no parameters)."""


class WellUpdateArgs(BaseModel):
    """Body for ``POST /control/well/update``."""

    well: str = Field(..., min_length=2, max_length=3)
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None
    clear_sample_id: bool = False
    clear_notes: bool = False


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class AbsorbanceArgs(BaseModel):
    """Body for ``POST /control/read/absorbance``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    wavelength_nm: float = Field(..., ge=200.0, le=999.0)


class FluorescenceArgs(BaseModel):
    """Body for ``POST /control/read/fluorescence``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    excitation_nm: float = Field(..., ge=200.0, le=999.0)
    emission_nm: float = Field(..., ge=200.0, le=999.0)
    gain: float = Field(default=50.0, ge=0.0, le=255.0)
    focal_height_mm: float = Field(default=7.0, ge=0.0, le=30.0)


class LuminescenceArgs(BaseModel):
    """Body for ``POST /control/read/luminescence``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    integration_time_s: float = Field(default=1.0, ge=0.1, le=60.0)
    gain: float = Field(default=50.0, ge=0.0, le=255.0)


class ReadResult(BaseModel):
    """Response body for read.* skills (``dict[well, value]``)."""

    wells: dict[str, float]


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


class ImagingCaptureArgs(BaseModel):
    """Body for ``POST /control/imaging/capture``."""

    well: str = Field(..., min_length=2, max_length=3)
    channel: str
    focal_height_mm: float = Field(default=5.0, ge=0.0, le=30.0)
    exposure_ms: float = Field(default=10.0, ge=0.01, le=10_000.0)
    gain: float = Field(default=1.0, ge=0.0, le=255.0)


class ImagingCaptureResult(BaseModel):
    """Response body for ``imaging.capture``."""

    well: str
    channel: str
    focal_height_mm: float
    exposure_ms: float
    gain: float
    details: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


register(
    "plate_reader",
    [
        SkillDef(
            name="startup",
            kind="plate_reader",
            description="Connect to the Cytation 5 and initialise the optics + incubator.",
            endpoint="/control/startup",
            args_schema=StartupArgs,
            requires_states=["requires_init", "ready", "dry_run"],
            estimated_duration_s=15.0,
        ),
        SkillDef(
            name="shutdown",
            kind="plate_reader",
            description="Disconnect from the Cytation 5.",
            endpoint="/control/shutdown",
            args_schema=ShutdownArgs,
            requires_states=["ready", "busy", "degraded", "error", "dry_run"],
            estimated_duration_s=1.0,
        ),
        SkillDef(
            name="drawer.open",
            kind="plate_reader",
            description="Eject the plate stage so a robot can place / retrieve a plate.",
            endpoint="/control/drawer/open",
            args_schema=DrawerArgs,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=4.0,
        ),
        SkillDef(
            name="drawer.close",
            kind="plate_reader",
            description="Retract the plate stage into the reader.",
            endpoint="/control/drawer/close",
            args_schema=DrawerArgs,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=4.0,
        ),
        SkillDef(
            name="plate.load",
            kind="plate_reader",
            description=(
                "Register that a plate is physically on the stage. The orchestrator "
                "owns plate_id; the device persists per-well sample/volume state."
            ),
            endpoint="/control/plate/load",
            args_schema=PlateLoadArgs,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=0.5,
        ),
        SkillDef(
            name="plate.unload",
            kind="plate_reader",
            description="Clear the currently-loaded plate record.",
            endpoint="/control/plate/unload",
            args_schema=PlateUnloadArgs,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=0.5,
        ),
        SkillDef(
            name="well.update",
            kind="plate_reader",
            description="Mutate one well's sample_id / volume_ul / notes.",
            endpoint="/control/well/update",
            args_schema=WellUpdateArgs,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=0.2,
        ),
        SkillDef(
            name="read.absorbance",
            kind="plate_reader",
            description="Read absorbance at one wavelength for the named wells.",
            endpoint="/control/read/absorbance",
            args_schema=AbsorbanceArgs,
            returns_schema=ReadResult,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=15.0,
        ),
        SkillDef(
            name="read.fluorescence",
            kind="plate_reader",
            description="Read fluorescence (ex/em pair) for the named wells.",
            endpoint="/control/read/fluorescence",
            args_schema=FluorescenceArgs,
            returns_schema=ReadResult,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=20.0,
        ),
        SkillDef(
            name="read.luminescence",
            kind="plate_reader",
            description="Read luminescence (no external excitation) for the named wells.",
            endpoint="/control/read/luminescence",
            args_schema=LuminescenceArgs,
            returns_schema=ReadResult,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=30.0,
        ),
        SkillDef(
            name="imaging.capture",
            kind="plate_reader",
            description=(
                "Capture one image on the imaging path. Channels: brightfield, "
                "phase_contrast, dapi, gfp, rfp, cy5 (device-configured)."
            ),
            endpoint="/control/imaging/capture",
            args_schema=ImagingCaptureArgs,
            returns_schema=ImagingCaptureResult,
            requires_states=["ready", "dry_run"],
            estimated_duration_s=3.0,
        ),
    ],
)


__all__ = [
    "AbsorbanceArgs",
    "DrawerArgs",
    "FluorescenceArgs",
    "ImagingCaptureArgs",
    "ImagingCaptureResult",
    "LuminescenceArgs",
    "PlateLoadArgs",
    "PlateUnloadArgs",
    "ReadResult",
    "ShutdownArgs",
    "StartupArgs",
    "WellSample",
    "WellUpdateArgs",
]
```

## 2) Register the new module

In `skills/src/lab_skills/skill_catalog/__init__.py`, add the eager
import next to the others:

```diff
 from . import fume_hood as _fume_hood  # noqa: F401
+from . import plate_reader as _plate_reader  # noqa: F401
 from . import plate_sealer as _plate_sealer  # noqa: F401
 from . import press as _press  # noqa: F401
 from . import robot_arm as _robot_arm  # noqa: F401
 from . import solid_doser as _solid_doser  # noqa: F401
```

## 3) Flip the `equipment.yaml` entry to v1.1

Apply this diff to `equipment.yaml` in the monorepo:

```diff
   - id: cytation_5
     name: BioTek Cytation 5
     kind: plate_reader
     adapter: http
-    protocol: "1.0"
+    protocol: "1.1"
     base_url: http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8040
     status_path: /status
     poll_timeout_seconds: 3.0
-    # The agilent-cytation-server repo conforms to STATUS_SPEC v1.0
-    # (read-only). It does not yet expose `/control/*` or
-    # claim/heartbeat/release. The SDK keeps the device read-only by
-    # leaving `do_not_call_connect: true` in place; specific unit
-    # operations (read.absorbance, read.fluorescence, read.luminescence,
-    # imaging.capture, plate.load/unload, drawer.open/close) graduate
-    # to /control/* in a follow-up release.
-    do_not_call_connect: true
+    # The agilent-cytation-server repo conforms to STATUS_SPEC v1.1.
+    # /control/* is wired (claim/heartbeat/release, drawer, plate
+    # management, reads, imaging). The skill catalog entry lives in
+    # lab_skills/skill_catalog/plate_reader.py.
     tiles:
       hte: { w: 2, h: 3 }
     pills: {}
```

## 4) Tests in the monorepo

The catalog has a regression suite at
`skills/tests/test_skill_catalog.py`. After step 1+2, add a tiny test
to confirm the new kind is registered (mirrors the assertions that
already exist for `plate_sealer` / `press`):

```python
def test_plate_reader_catalog_registered() -> None:
    from lab_skills.skill_catalog import SKILL_REGISTRY

    names = {d.name for d in SKILL_REGISTRY["plate_reader"]}
    assert {
        "startup", "shutdown",
        "drawer.open", "drawer.close",
        "plate.load", "plate.unload", "well.update",
        "read.absorbance", "read.fluorescence", "read.luminescence",
        "imaging.capture",
    } <= names
```

## 5) After applying

1. `uv run pytest skills/tests/test_skill_catalog.py -q` — confirm registration.
2. `uv run pytest skills/tests/test_registry.py -q` — confirm the
   placeholder-hostname guard still passes.
3. `sudo systemctl restart ac-organic-lab-api.service` on the
   dashboard host.
4. `curl http://localhost:3000/api/equipment` and confirm the
   cytation_5 entry shows `protocol: "1.1"` and
   `status.allowed_actions` is non-empty.
5. **Only then** flip a workflow over to `await
   session.role("plate_reader").read_absorbance(...)`. Hardware
   verification per `RUNBOOK.md` §3-§4 must have completed first.
