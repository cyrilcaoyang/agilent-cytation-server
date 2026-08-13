# Skill catalog patch for `lab-skills`

This is a **drop-in patch** for the central server's
[`ac-organic-lab`](https://github.com/cyrilcaoyang/ac-organic-lab) monorepo.

The device PC's `ac-organic-lab/` checkout shares the central server's remote
and **may be edited and pushed from here** — an earlier version of this doc
called it a read-only mirror, which is no longer the workspace rule. Either
apply the patch from this PC and let the central server pull, or apply it
there; just coordinate so the two do not diverge. Contract changes are still
better made on the central server when practical.

It declares one `SkillDef` per `/control/*` verb the device exposes, so
workflows can `await session.role("plate_reader").read_absorbance(...)` instead
of hand-rolling HTTP.

> **Currency.** Regenerated 2026-08-12 against the live device. The earlier
> draft (`phase4_handoff.md`) predated the control-surface fixes and carried
> arg ranges the device no longer accepts — absorbance from 200 nm, a `gain`
> on the reads, focal height from 0 mm. Applying it now would produce a
> catalog that lets the SDK build requests the device rejects with 422. If you
> are reading a copy older than this line, re-derive from
> `agilent_cytation_server/control_args.py`, which is the source of truth.

The patch has four parts:

1. A new file `skills/src/lab_skills/skill_catalog/plate_reader.py`.
2. A one-line registration in `skills/src/lab_skills/skill_catalog/__init__.py`.
3. An `equipment.yaml` flip to `protocol: "1.2"`, removing `do_not_call_connect`.
4. A registration test.

**Sequencing.** Hardware verification per [`IMPLEMENTATION.md`](IMPLEMENTATION.md)
should land **first**: removing `do_not_call_connect` is what lets the SDK start
issuing real `/control/*` calls, and the read path has not yet completed on
hardware. Imaging, incubator and shaker are verified; the three `read.*` verbs
are not.

---

## What the device actually exposes

Sixteen verbs, of which thirteen are skills (the claim trio is protocol
machinery the SDK's `ClaimManager` handles, not catalog entries).

| Skill | Endpoint | Notes |
|---|---|---|
| `startup` / `shutdown` | `/control/{startup,shutdown}` | |
| `drawer.open` / `drawer.close` | `/control/drawer/{open,close}` | |
| `plate.load` / `plate.unload` | `/control/plate/{load,unload}` | load is a **precondition for every read** |
| `well.update` | `/control/well/update` | |
| `read.absorbance` | `/control/read/absorbance` | 230–999 nm |
| `read.fluorescence` | `/control/read/fluorescence` | ex/em 250–700 nm |
| `read.luminescence` | `/control/read/luminescence` | focal height required |
| `imaging.capture` | `/control/imaging/capture` | needs a plate **and** a live camera |
| `incubator.set_temperature` | `/control/incubator/set_temperature` | 4–45 °C |
| `incubator.stop` | `/control/incubator/stop` | |
| `shake.start` / `shake.stop` | `/control/shake/{start,stop}` | see the ceiling below |

Three device-side behaviours the catalog cannot express, and does not try to:

- **No `gain` on any read.** PyLabRobot's Cytation backend has no read-gain
  control, and the device returns 422 rather than silently ignoring the field
  (a dropped gain would mean a plausible number measured at some other gain).
  Do not add one to the catalog "for symmetry".
- **`read.*` requires a loaded plate.** This is not a component state, so
  `requires_components` cannot express it — exactly the division of labour
  `SKILLS_CATALOG.md` describes for plateloc's third interlock. The device
  withholds the verb from `allowed_actions` when no plate is loaded, and the
  SDK prefers that list, so nothing is needed here.
- **Reads are withheld while shaking.** Partly sample sense, mainly protocol
  safety: PyLabRobot's `send_command` has no internal lock and the shake task
  talks to the instrument on its own schedule, so a concurrent read would
  interleave writes on the serial link. Again handled by `allowed_actions`.

---

## 1) New file: `skills/src/lab_skills/skill_catalog/plate_reader.py`

```python
"""Skill catalog entries for ``kind=plate_reader``.

Reference device: :mod:`agilent_cytation_server` — STATUS_SPEC v1.2, full
``/control/*`` write surface (claim protocol, drawer, plate management, three
read types, imaging, incubator, shaker). Endpoint paths and arg ranges mirror
the device's Pydantic constraints in
``agilent_cytation_server/control_args.py``; keep them in step.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import SkillDef
from .registry import register


# Ranges are the driver's own limits, duplicated here so the SDK refuses a
# doomed request locally instead of round-tripping to a 422.
_ABS_NM = (230.0, 999.0)
_EX_NM = (250.0, 700.0)
_EM_NM = (250.0, 700.0)
_FOCAL_MM = (4.5, 13.88)


# ---------------------------------------------------------------------------
# Lifecycle / drawer
# ---------------------------------------------------------------------------


class StartupArgs(BaseModel):
    """Body for ``POST /control/startup`` (no parameters)."""


class ShutdownArgs(BaseModel):
    """Body for ``POST /control/shutdown`` (no parameters)."""


class DrawerArgs(BaseModel):
    """Body for ``POST /control/drawer/{open,close}`` (no parameters)."""


# ---------------------------------------------------------------------------
# Plate / well sample tracking
# ---------------------------------------------------------------------------


class WellSample(BaseModel):
    """One well of the currently-loaded plate. Mirrors the device-side
    ``agilent_cytation_server.models.WellSample``."""

    well: str
    sample_id: str | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    notes: str | None = None


class PlateLoadArgs(BaseModel):
    """Body for ``POST /control/plate/load``.

    More than bookkeeping: PyLabRobot addresses wells through the ``Plate``
    resource assigned to the reader, so loading is what makes any read
    possible at all.
    """

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
#
# NOTE: no `gain` field on any of these. The device exposes no read-gain
# control and returns 422 for the field rather than ignoring it.
# ---------------------------------------------------------------------------


class AbsorbanceArgs(BaseModel):
    """Body for ``POST /control/read/absorbance``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    wavelength_nm: float = Field(..., ge=_ABS_NM[0], le=_ABS_NM[1])


class FluorescenceArgs(BaseModel):
    """Body for ``POST /control/read/fluorescence``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    excitation_nm: float = Field(..., ge=_EX_NM[0], le=_EX_NM[1])
    emission_nm: float = Field(..., ge=_EM_NM[0], le=_EM_NM[1])
    focal_height_mm: float = Field(default=7.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])


class LuminescenceArgs(BaseModel):
    """Body for ``POST /control/read/luminescence``."""

    wells: list[str] = Field(..., min_length=1, max_length=96)
    focal_height_mm: float = Field(default=7.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])
    integration_time_s: float = Field(default=1.0, ge=0.1, le=60.0)


class ReadResult(BaseModel):
    """Response body for read.* skills (``dict[well, value]``)."""

    wells: dict[str, float]


# ---------------------------------------------------------------------------
# Incubator / shaker
# ---------------------------------------------------------------------------


class TemperatureArgs(BaseModel):
    """Body for ``POST /control/incubator/set_temperature``.

    The 4 °C floor comes from the driver assuming every Cytation can cool.
    Units without a cooling module may accept a low setpoint and not act on
    it, so treat sub-ambient as unverified per device.
    """

    celsius: float = Field(..., ge=4.0, le=45.0)


class TemperatureStopArgs(BaseModel):
    """Body for ``POST /control/incubator/stop`` (no parameters)."""


class ShakeArgs(BaseModel):
    """Body for ``POST /control/shake/start``.

    ``displacement_mm`` is PyLabRobot's ``frequency`` argument renamed: it is
    orbit displacement in mm and runs *inversely* to speed — 6 mm is ~360 CPM,
    1 mm is ~1096 CPM.
    """

    pattern: Literal["orbital", "linear"] = "orbital"
    displacement_mm: int = Field(default=3, ge=1, le=6)


class ShakeStopArgs(BaseModel):
    """Body for ``POST /control/shake/stop`` (no parameters)."""


# ---------------------------------------------------------------------------
# Imaging
# ---------------------------------------------------------------------------


class ImagingCaptureArgs(BaseModel):
    """Body for ``POST /control/imaging/capture``.

    ``gain`` here is the Spinnaker camera's analog gain in dB — unrelated to
    the PMT gain the reads deliberately do not expose. Fluorescence channels
    require the matching filter cube to be physically fitted; read
    ``details.imaging.installed_filters`` from ``/status`` before offering one.
    """

    well: str = Field(..., min_length=2, max_length=3)
    channel: str
    objective: str | None = None
    focal_height_mm: float = Field(default=5.0, ge=_FOCAL_MM[0], le=_FOCAL_MM[1])
    exposure_ms: float = Field(default=10.0, ge=0.01, le=10_000.0)
    gain: float = Field(default=0.0, ge=0.0, le=47.0)
    led_intensity: int = Field(default=10, ge=1, le=10)
    autofocus: bool = False
    auto_exposure: bool = False


class ImagingCaptureResult(BaseModel):
    """Response body for ``imaging.capture``.

    ``focal_height_mm`` / ``exposure_ms`` are the **resolved** values, which
    differ from the request when autofocus / auto-exposure ran.
    """

    well: str
    channel: str
    objective: str | None = None
    focal_height_mm: float
    exposure_ms: float
    gain: float
    image_path: str | None = None
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
            description="Connect to the Cytation and initialise optics + camera.",
            endpoint="/control/startup",
            args_schema=StartupArgs,
            requires_states=["requires_init", "ready", "dry_run"],
            estimated_duration_s=15.0,
        ),
        SkillDef(
            name="shutdown",
            kind="plate_reader",
            description="Disconnect from the Cytation.",
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
                "Register that a plate is on the stage. Required before any read: "
                "the driver addresses wells through the plate resource."
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
            description="Read absorbance at one wavelength (230-999 nm) for the named wells.",
            endpoint="/control/read/absorbance",
            args_schema=AbsorbanceArgs,
            returns_schema=ReadResult,
            requires_states=["ready", "dry_run"],
            # Motor idle is not enough — a plate must be loaded — but that is
            # not a component state, so the device's allowed_actions carries
            # it. The shaker gate IS expressible and matters: reads and the
            # shake task cannot share the serial link.
            requires_components={"shaker": "idle"},
            estimated_duration_s=15.0,
        ),
        SkillDef(
            name="read.fluorescence",
            kind="plate_reader",
            description="Read fluorescence (ex/em 250-700 nm) for the named wells.",
            endpoint="/control/read/fluorescence",
            args_schema=FluorescenceArgs,
            returns_schema=ReadResult,
            requires_states=["ready", "dry_run"],
            requires_components={"shaker": "idle"},
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
            requires_components={"shaker": "idle"},
            estimated_duration_s=30.0,
        ),
        SkillDef(
            name="imaging.capture",
            kind="plate_reader",
            description=(
                "Capture one image bottom-up. Channels: brightfield, phase_contrast, "
                "and any fluorescence channel whose filter cube is fitted. Optional "
                "autofocus / auto-exposure search."
            ),
            endpoint="/control/imaging/capture",
            args_schema=ImagingCaptureArgs,
            returns_schema=ImagingCaptureResult,
            requires_states=["ready", "dry_run"],
            # `imaging` reports state "disconnected" when the camera failed to
            # initialise, so this gate is a real camera check, not a config flag.
            requires_components={"imaging": "idle", "shaker": "idle"},
            estimated_duration_s=5.0,
        ),
        SkillDef(
            name="incubator.set_temperature",
            kind="plate_reader",
            description="Set the incubator setpoint (4-45 C) and begin ramping.",
            endpoint="/control/incubator/set_temperature",
            args_schema=TemperatureArgs,
            requires_states=["ready", "busy", "dry_run"],
            estimated_duration_s=1.0,
        ),
        SkillDef(
            name="incubator.stop",
            kind="plate_reader",
            description="End temperature control; the incubator drifts to ambient.",
            endpoint="/control/incubator/stop",
            args_schema=TemperatureStopArgs,
            requires_states=["ready", "busy", "dry_run"],
            estimated_duration_s=1.0,
        ),
        SkillDef(
            name="shake.start",
            kind="plate_reader",
            description=(
                "Start shaking (orbital or linear). Motion outlives this call: the "
                "driver re-issues the command every 16 minutes until stopped."
            ),
            endpoint="/control/shake/start",
            args_schema=ShakeArgs,
            requires_states=["ready", "dry_run"],
            requires_components={"shaker": "idle"},
            estimated_duration_s=2.0,
        ),
        SkillDef(
            name="shake.stop",
            kind="plate_reader",
            description="Stop shaking. Remains available while the plate is moving.",
            endpoint="/control/shake/stop",
            args_schema=ShakeStopArgs,
            requires_states=["ready", "busy", "dry_run"],
            estimated_duration_s=1.0,
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
    "ShakeArgs",
    "ShakeStopArgs",
    "ShutdownArgs",
    "StartupArgs",
    "TemperatureArgs",
    "TemperatureStopArgs",
    "WellSample",
    "WellUpdateArgs",
]
```

## 2) Register the new module

In `skills/src/lab_skills/skill_catalog/__init__.py`, add the eager import next
to the others:

```diff
 from . import fume_hood as _fume_hood  # noqa: F401
+from . import plate_reader as _plate_reader  # noqa: F401
 from . import plate_sealer as _plate_sealer  # noqa: F401
 from . import press as _press  # noqa: F401
 from . import robot_arm as _robot_arm  # noqa: F401
 from . import solid_doser as _solid_doser  # noqa: F401
```

## 3) Flip the `equipment.yaml` entry to v1.2

```diff
   - id: cytation_5
     name: BioTek Cytation 5
     kind: plate_reader
     adapter: http
-    protocol: "1.0"
+    protocol: "1.2"
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
+    # STATUS_SPEC v1.2. Full /control/* surface: claim/heartbeat/release,
+    # drawer, plate management, three reads, imaging, incubator, shaker.
+    # Catalog entry: lab_skills/skill_catalog/plate_reader.py.
     tiles:
       hte: { w: 2, h: 3 }
     pills: {}
```

The device has reported `protocol_version: "1.2"` on the wire since
2026-08-02, so this only removes registry drift.

## 4) Tests in the monorepo

Add to `skills/tests/test_skill_catalog.py`, mirroring the existing
`plate_sealer` / `press` assertions:

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
        "incubator.set_temperature", "incubator.stop",
        "shake.start", "shake.stop",
    } <= names


def test_plate_reader_reads_take_no_gain() -> None:
    """The device 422s a `gain` on any read rather than ignoring it, because a
    dropped gain yields a plausible number measured at some other gain."""
    from lab_skills.skill_catalog import SKILL_REGISTRY

    for skill in SKILL_REGISTRY["plate_reader"]:
        if skill.name.startswith("read."):
            assert "gain" not in skill.args_schema.model_fields
```

## 5) After applying

1. `uv run pytest skills/tests/test_skill_catalog.py -q` — confirm registration.
2. `uv run pytest skills/tests/test_registry.py -q` — placeholder-hostname guard.
3. `sudo systemctl restart ac-organic-lab-api.service` on the dashboard host.
4. `curl http://localhost:3000/api/equipment` — confirm `cytation_5` shows
   `protocol: "1.2"` and a non-empty `allowed_actions`.
5. **Only then** point a workflow at
   `await session.role("plate_reader").read_absorbance(...)`.

Expect `allowed_actions` to be *shorter* than the catalog: reads appear only
once a plate is loaded, and `imaging.capture` only when the camera initialised.
That is the §6.2 mirror working, not a missing skill — the SDK prefers
`allowed_actions` over `requires_states`, so it will report those skills
unavailable with a reason until their preconditions hold.

## See also

- [`IMPLEMENTATION.md`](IMPLEMENTATION.md) — the bench verification this is
  sequenced behind.
- [`../README.md`](../README.md) — the live control surface and per-capability
  status.
- [`PLATE_STATE.md`](PLATE_STATE.md) — how per-well sample tracking works.
- `SKILLS_CATALOG.md` in the monorepo — `SkillDef` semantics, and why
  `requires_components` is a hint rather than a mirror of the device's full
  precondition set.
