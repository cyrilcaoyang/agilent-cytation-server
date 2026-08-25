"""Service layer that exposes the Cytation 5 driver as a spec-compliant
``EquipmentStatus`` source.

Why this exists
---------------
The PyLabRobot ``PlateReader`` is asynchronous but not concurrency-safe:
only one caller may talk to the backend at a time. The dashboard polls
``GET /status`` every 2-3 seconds while operators may concurrently fire
control commands (Phase 3+). The service owns:

* a single reader instance (real :class:`CytationReader` or
  :class:`StubCytationReader`),
* an ``asyncio.Lock`` that serialises every call into the reader,
* a small in-memory state machine (``_busy_state``, ``_last_error``,
  ``_drawer``, ``_last_read_at``) used to compute the spec
  ``equipment_status`` field,
* a :meth:`get_status` method that produces a fresh
  :class:`EquipmentStatus` envelope without ever issuing a write to
  the device.

If PyLabRobot cannot be loaded (non-Windows host, missing pyusb,
hardware off) ``dry_run=True`` swaps in :class:`StubCytationReader`
so the API surface stays identical and the dashboard tile can be
exercised end-to-end on macOS / Linux.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from . import config as _config
from .claims import ClaimManager
from .errors import PreconditionNotMet, describe
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
    LoadedPlate,
    MetricValue,
    WellSample,
)
from .plate_state import PlateStateStore
from .reader import StubCytationReader, make_reader

logger = logging.getLogger(__name__)


# Window during which a recent error keeps the device in `error` state.
# After this, if no further failures land, the device falls back to
# `ready` / `degraded` per STATUS_SPEC v1.1 state machine.
_RECENT_ERROR_WINDOW_S = 60.0

# How long a cached instrument readback (temperature) stays fresh. `/status`
# serves the cache rather than reading the instrument on the request path.
_READBACK_TTL_S = 3.0

# Hard bound on how long `/status` will wait for the reader lock in order to
# refresh that cache. A poll must NEVER queue behind an operation: a plate read
# holds the lock for seconds to minutes, so a blocking poll would return only
# after the read finished — with `_busy_state` already back to False. That made
# `busy` (and therefore v1.2 `activity: "running"`) unobservable from outside,
# which is the whole point of the field. On timeout we serve the stale cache.
_READBACK_LOCK_WAIT_S = 0.05

# How close the readback must sit to the commanded setpoint before the
# incubator is called `at_setpoint` rather than `heating` / `cooling`.
_TEMPERATURE_TOLERANCE_C = 0.5


class CytationService:
    """Wraps a :class:`CytationReader` (or :class:`StubCytationReader`)
    and produces spec-compliant :class:`EquipmentStatus` snapshots.

    Concurrency: all driver I/O happens inside ``self._lock``. ``get_status``
    deliberately does **not** take that lock — it composes the envelope from
    in-memory state plus a short-TTL readback cache, and will wait no longer
    than :data:`_READBACK_LOCK_WAIT_S` to refresh that cache. A status poll
    that blocks for the duration of a plate read cannot observe the read
    happening, which is exactly what ``activity`` (STATUS_SPEC v1.2 §2.3)
    exists to report.
    """

    def __init__(
        self,
        *,
        dry_run: bool = False,
        reader_factory: Callable[[], Any] | None = None,
        plate_state: PlateStateStore | None = None,
        claim_manager: ClaimManager | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._reader_factory = reader_factory
        self._reader: Any | None = None
        self._lock = asyncio.Lock()
        self._started_at = time.monotonic()
        self._last_error: ErrorInfo | None = None
        self._busy_state: bool = False
        self._drawer: str = "unknown"
        # Commanded incubator setpoint, or None when temperature control is
        # off. Tracked here because the instrument reports only the measured
        # temperature, and "22 C" means something entirely different with a
        # setpoint of 22 than with the incubator idle.
        self._setpoint_c: float | None = None
        self._last_read_at: float | None = None  # time.monotonic()
        self._read_count: int = 0
        # Reserved monotonic counter (§2.3.1). Reads and captures routinely
        # finish between two 60 s dashboard polls, so a sampled `activity`
        # series does not undercount them — it misses them entirely. The
        # poll-to-poll delta of this counter is the accountable number.
        self._cycles_total: int = 0
        # Activity span tracking (§2.3). `_activity` is the last observed
        # value; `_activity_since` is the instant it last changed — the start
        # of the CURRENT span, never the poll instant. Operations stamp both
        # edges; `get_status` only reconciles.
        self._activity: str = "idle"
        self._activity_since: datetime | None = datetime.now(timezone.utc)
        # Cached instrument readback, served by `/status` so a poll never
        # issues hardware I/O on the request path. See `_refresh_readings`.
        self._readings: dict[str, Any] = {}
        self._readback_errors: list[str] = []
        self._readings_at: float | None = None  # time.monotonic()

        # Identity (configurable so a deployment can override).
        self.equipment_id: str = _config.get("dashboard", "equipment_id", "cytation_5")
        self.equipment_name: str = _config.get(
            "dashboard", "equipment_name", "BioTek Cytation 5"
        )
        self.equipment_kind = "plate_reader"
        self.equipment_version: str | None = _config.get(
            "dashboard", "equipment_version", None
        )
        self.imaging_enabled: bool = bool(_config.get("imaging", "enabled", True))
        self.usb_serial: str = str(_config.get("instrument", "usb_serial", "") or "")
        self.default_plate_model: str = str(
            _config.get("plates", "default_model", "custom_96")
        )

        # Phase 2: per-well sample tracking.
        if plate_state is None:
            state_path = _config.get("plates", "state_path", "./state.json")
            plate_state = PlateStateStore(state_path=state_path)
        self.plate_state = plate_state

        # Phase 3: cooperative claim manager.
        if claim_manager is None:
            enforce_claims = bool(_config.get("service", "enforce_claims", True))
            claim_manager = ClaimManager(enforce=enforce_claims)
        self.claims = claim_manager

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _create_reader(self) -> Any:
        if self._reader_factory is not None:
            return self._reader_factory()
        return make_reader(dry_run=self.dry_run, usb_serial=self.usb_serial or None)

    async def startup(self) -> None:
        """Create (or reuse) the reader and connect.

        On failure, leaves the service in ``requires_init`` and re-raises
        so callers (lifespan / a future ``/control/startup``) can decide
        whether to log-and-continue or surface a 503.
        """
        async with self._lock:
            if self._reader is not None and self._reader.is_connected():
                return
            self._reader = self._create_reader()
            try:
                await self._reader.setup()
                self._last_error = None
                # Reader assumes drawer is in unless it tells us otherwise.
                self._drawer = "in"
                # `equipment_version` was null on every envelope because
                # nothing ever filled it. The instrument knows its own
                # firmware revision, so prefer that over silence — but let an
                # explicit config value win, since a deployment may want to
                # report the service version instead.
                if not _config.get("dashboard", "equipment_version", None):
                    probe = getattr(self._reader, "firmware_version", None)
                    if probe is not None:
                        self.equipment_version = probe() or self.equipment_version
            except Exception as exc:
                # A failed setup() has usually already opened the USB
                # handle. Leaving the half-open reader on self._reader keeps
                # that handle for the life of the process: the device then
                # enumerates with a blank serial/description and *every*
                # later startup fails identically, so one transient
                # non-response becomes permanently unrecoverable without a
                # service restart (2026-08-25). Drop it so a retry re-opens
                # cleanly.
                try:
                    await self._reader.stop()
                except Exception:
                    logger.debug(
                        "Discarding reader after failed setup", exc_info=True
                    )
                finally:
                    self._reader = None
                self._record_error(exc, "startup")
                raise

    async def shutdown(self) -> None:
        """Best-effort disconnect. Never raises."""
        async with self._lock:
            if self._reader is None:
                return
            try:
                await self._reader.stop()
            except Exception:
                logger.exception("Error while stopping reader")
            finally:
                self._reader = None
                self._busy_state = False
                self._drawer = "unknown"
                # The cached readback describes a reader we no longer hold;
                # keeping it would let /status report a temperature for a
                # disconnected instrument.
                self._readings = {}
                self._readback_errors = []
                self._readings_at = None
                self._note_activity(self._observed_activity())

    # ------------------------------------------------------------------
    # Status (side-effect-free)
    # ------------------------------------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot. MUST NOT mutate hardware state.

        The spec requires this endpoint to be safe to call every 2-3
        seconds and to always return HTTP 200 unless the process itself
        is broken. Per-getter failures fold into ``degraded`` rather
        than raise.

        Does not hold the reader lock while composing the envelope: see the
        class docstring for why a poll must stay answerable mid-operation.
        """

        await self._refresh_readings()
        return self._build_status()

    async def _refresh_readings(self) -> None:
        """Best-effort refresh of the cached instrument readback.

        Skipped entirely while an operation owns the reader, and bounded by
        :data:`_READBACK_LOCK_WAIT_S` otherwise, so the caller is never made
        to wait on hardware. A stale cache is strictly better than a poll that
        cannot answer — and ``_readings_at`` records how stale.
        """

        if self._reader is None or not self._reader.is_connected():
            return
        fresh_until = (self._readings_at or 0.0) + _READBACK_TTL_S
        if self._readings_at is not None and time.monotonic() < fresh_until:
            return
        if self._lock.locked():
            return
        try:
            await asyncio.wait_for(self._lock.acquire(), _READBACK_LOCK_WAIT_S)
        except (asyncio.TimeoutError, TimeoutError):
            return
        try:
            errors: list[str] = []
            self._readings["actual_temperature"] = await self._safe_read(
                "actual_temperature", self._reader.get_temperature, errors
            )
            self._readback_errors = errors
            self._readings_at = time.monotonic()
        finally:
            self._lock.release()

    def _build_status(self) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = _safe_hostname()
        # Health (§2.2) and activity (§2.3) are answered independently. This
        # only reconciles the span for transitions no operation edge stamped.
        activity = self._sync_activity()

        # ---- not connected: requires_init ------------------------------
        if self._reader is None or not self._reader.is_connected():
            return EquipmentStatus(
                protocol_version=PROTOCOL_VERSION,
                equipment_id=self.equipment_id,
                equipment_name=self.equipment_name,
                equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
                equipment_version=self.equipment_version,
                host=host,
                equipment_status="requires_init",
                # §2.3's invariant table: requires_init ⇒ idle. Reporting
                # `unknown` here was a contract violation — with no session
                # open, "not performing its primary operation" is not merely
                # the honest answer, it is a certainty.
                activity=activity,
                activity_since=self._activity_since,
                message=(
                    "Driver not connected. POST /control/startup to "
                    "initialise the reader."
                ),
                required_actions=["startup"],
                allowed_actions=self._allowed_actions("requires_init", activity),
                device_time=now,
                uptime_seconds=uptime,
                components=self._disconnected_components(),
                # Stage state is unknown when disconnected; keep details
                # in sync with components.plate_stage.state.
                details=self._base_details(drawer_override="unknown"),
                last_error=self._last_error,
            )

        # ---- serve the cached readback; never read hardware from here ----
        metrics: dict[str, MetricValue] = {}
        details: dict[str, Any] = self._base_details()
        readback_errors: list[str] = list(self._readback_errors)

        actual_temp = self._readings.get("actual_temperature")
        if self._readings_at is not None:
            details["readback_age_s"] = round(time.monotonic() - self._readings_at, 3)
        if actual_temp is not None:
            metrics["actual_temperature"] = MetricValue(
                value=float(actual_temp), unit="C", timestamp=now
            )
        if self._setpoint_c is not None:
            metrics["setpoint_temperature"] = MetricValue(
                value=float(self._setpoint_c), unit="C", timestamp=now
            )

        if self._last_read_at is not None:
            metrics["last_read_seconds_ago"] = MetricValue(
                value=round(time.monotonic() - self._last_read_at, 3),
                unit="s",
            )
        # `read_count` is this repo's original, measurement-only counter and
        # stays for existing readers; `cycles_total` is the spec's reserved
        # key (§2.3.1) and additionally counts image captures, matching what
        # `activity` calls the primary operation. Same pattern as plateloc
        # publishing its odometer under both names.
        metrics["read_count"] = MetricValue(value=int(self._read_count), unit="count")
        metrics["cycles_total"] = MetricValue(value=int(self._cycles_total), unit="count")

        # ---- top-level equipment_status --------------------------------
        if self.dry_run:
            state: str = "dry_run"
            message: str | None = "Dry-run mode - no hardware connected"
            details["dry_run"] = True
        elif self._busy_state:
            state = "busy"
            message = "Plate reader operation in progress"
        elif self._last_error is not None and (
            (now - self._last_error.timestamp).total_seconds()
            < _RECENT_ERROR_WINDOW_S
        ):
            state = "error"
            message = self._last_error.message
        elif readback_errors:
            state = "degraded"
            message = "; ".join(readback_errors)
        else:
            state = "ready"
            message = "Idle, ready to read"

        components = self._connected_components(actual_temp)

        return EquipmentStatus(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=self.equipment_id,
            equipment_name=self.equipment_name,
            equipment_kind=self.equipment_kind,  # type: ignore[arg-type]
            equipment_version=self.equipment_version,
            host=host,
            equipment_status=state,  # type: ignore[arg-type]
            activity=activity,  # type: ignore[arg-type]
            activity_since=self._activity_since,
            message=message,
            allowed_actions=self._allowed_actions(state, activity),
            device_time=now,
            uptime_seconds=uptime,
            components=components,
            metrics=metrics,
            last_error=self._last_error,
            details=details,
        )

    # ------------------------------------------------------------------
    # Phase 2: per-well sample tracking
    # ------------------------------------------------------------------

    async def load_plate(
        self,
        *,
        plate_id: str,
        model: str | None = None,
        wells: list[WellSample] | None = None,
    ) -> LoadedPlate:
        """Register a plate as physically loaded on the stage.

        Persisted to ``state.json`` so service restarts keep the
        orchestrator's view of "what's loaded" coherent. Does **not**
        move the drawer — that's a separate operation (Phase 3
        ``/control/drawer/{open,close}``).
        """
        chosen = model or self.default_plate_model
        async with self._lock:
            # The reader's own resource tree must learn about the plate too,
            # not just our sample-tracking store: PyLabRobot routes every
            # read through `PlateReader.get_plate()` and raises NoPlateError
            # when nothing is assigned. Loading here is what makes
            # `read.absorbance` and friends reachable at all.
            if self._reader is not None and self._reader.is_connected():
                chosen = self._reader.load_plate(plate_id=plate_id, model=chosen)
            return self.plate_state.load_plate(
                plate_id=plate_id, model=chosen, wells=wells
            )

    async def unload_plate(self) -> LoadedPlate | None:
        """Clear the currently-loaded plate. Returns the prior plate (if any)."""
        async with self._lock:
            if self._reader is not None:
                self._reader.unload_plate()
            return self.plate_state.unload_plate()

    async def update_well(
        self,
        well: str,
        *,
        sample_id: str | None = None,
        volume_ul: float | None = None,
        notes: str | None = None,
        clear_sample_id: bool = False,
        clear_notes: bool = False,
    ) -> WellSample:
        """Mutate a single well's sample / volume / notes."""
        async with self._lock:
            return self.plate_state.update_well(
                well,
                sample_id=sample_id,
                volume_ul=volume_ul,
                notes=notes,
                clear_sample_id=clear_sample_id,
                clear_notes=clear_notes,
            )

    # ------------------------------------------------------------------
    # Phase 3 (v1.1): /control/* operations
    #
    # Each method assumes the API layer has already validated the
    # X-Claim-Token; the service does NOT re-check it. Hardware errors
    # are recorded via ``_record_error`` so they land on /status and
    # the device falls into ``error`` for the recent-error window.
    # ------------------------------------------------------------------

    async def open_drawer(self) -> None:
        async with self._lock:
            self._require_connected()
            async with self._operation("drawer.open"):
                await self._reader.open_drawer()
                self._drawer = "out"

    async def close_drawer(self) -> None:
        async with self._lock:
            self._require_connected()
            async with self._operation("drawer.close"):
                await self._reader.close_drawer()
                self._drawer = "in"

    async def read_absorbance(
        self,
        *,
        wells: list[str],
        wavelength_nm: float,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            async with self._operation("read.absorbance", counts_as_cycle=True):
                result = await self._reader.read_absorbance(
                    wells=wells, wavelength_nm=wavelength_nm
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            async with self._operation("read.fluorescence", counts_as_cycle=True):
                result = await self._reader.read_fluorescence(
                    wells=wells,
                    excitation_nm=excitation_nm,
                    emission_nm=emission_nm,
                    focal_height_mm=focal_height_mm,
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        focal_height_mm: float = 7.0,
        integration_time_s: float = 1.0,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            async with self._operation("read.luminescence", counts_as_cycle=True):
                result = await self._reader.read_luminescence(
                    wells=wells,
                    focal_height_mm=focal_height_mm,
                    integration_time_s=integration_time_s,
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result

    # ------------------------------------------------------------------
    # Incubator + shaker
    # ------------------------------------------------------------------

    async def set_temperature(self, celsius: float) -> None:
        async with self._lock:
            self._require_connected()
            async with self._operation("incubator.set_temperature"):
                await self._reader.set_temperature(celsius)
                self._setpoint_c = celsius
                # The cached readback describes the pre-setpoint instrument;
                # force the next poll to go and look.
                self._readings_at = None

    async def stop_temperature_control(self) -> None:
        async with self._lock:
            self._require_connected()
            async with self._operation("incubator.stop"):
                await self._reader.stop_temperature_control()
                self._setpoint_c = None
                self._readings_at = None

    async def shake(self, *, pattern: str = "orbital", displacement_mm: int = 3) -> None:
        """Start shaking. Returns once motion has begun, not when it ends.

        Shaking is not bracketed by ``_operation``: the driver's shake runs as
        a background task that outlives this call, so an activity span opened
        and closed here would report `running` for a few milliseconds and
        `idle` for the minutes the plate is actually moving. Activity is
        observed from the driver's own flag instead — see
        :meth:`_observed_activity`.
        """

        async with self._lock:
            self._require_connected()
            try:
                await self._reader.shake(
                    pattern=pattern, displacement_mm=displacement_mm
                )
            except (PreconditionNotMet, ValueError) as exc:
                logger.info("Cytation refused shake.start: %s", exc)
                raise
            except Exception as exc:
                self._record_error(exc, "shake.start")
                raise
            self._note_activity(self._observed_activity())

    async def stop_shaking(self) -> None:
        async with self._lock:
            self._require_connected()
            try:
                await self._reader.stop_shaking()
            except Exception as exc:
                self._record_error(exc, "shake.stop")
                raise
            self._note_activity(self._observed_activity())

    async def capture_image(
        self,
        *,
        well: str,
        channel: str,
        focal_height_mm: float = 5.0,
        exposure_ms: float = 10.0,
        gain: float = 1.0,
        objective: str | None = None,
        led_intensity: int = 10,
        autofocus: bool = False,
        auto_exposure: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_connected()
            if not self.imaging_enabled:
                raise RuntimeError(
                    "Imaging is disabled in config.toml ([imaging].enabled=false)"
                )
            async with self._operation("imaging.capture", counts_as_cycle=True):
                return await self._reader.capture_image(
                    well=well,
                    channel=channel,
                    focal_height_mm=focal_height_mm,
                    exposure_ms=exposure_ms,
                    gain=gain,
                    objective=objective,
                    led_intensity=led_intensity,
                    autofocus=autofocus,
                    auto_exposure=auto_exposure,
                )

    # ------------------------------------------------------------------
    # Activity (STATUS_SPEC v1.2 §2.3)
    # ------------------------------------------------------------------

    def _observed_activity(self) -> str:
        """Is the instrument executing a commanded operation right now?

        Observed from ``_busy_state``, which every operation sets around its
        own call into the reader — never computed from ``equipment_status``,
        which §2.3 forbids because it would add no information.

        "Primary operation" for this plate reader is a **measurement**
        (absorbance / fluorescence / luminescence) or an **image capture**.
        A drawer move also reports ``running``: the instrument is executing a
        commanded operation and cannot start a read until it finishes. Drawer
        moves are not counted in ``cycles_total`` — see the README table.

        **Shaking also counts**, and is observed from the driver's own flag
        rather than from a span we open. The shake command returns as soon as
        motion starts and a background task keeps it going, so the plate is
        moving long after the request completed — bracketing it would report
        milliseconds of `running` for minutes of motion. Holding a temperature
        setpoint deliberately does *not* count: that is a maintained
        condition, not an operation in progress.
        """

        if self._busy_state:
            return "running"
        probe = getattr(self._reader, "is_shaking", None) if self._reader else None
        if probe is not None:
            try:
                if probe():
                    return "running"
            except Exception:  # pragma: no cover - defensive
                pass
        return "idle"

    def _temperature_range(self) -> tuple[float | None, float | None]:
        probe = getattr(self._reader, "temperature_range", None) if self._reader else None
        if probe is None:
            return (None, None)
        try:
            lo, hi = probe()
            return (lo, hi)
        except Exception:  # pragma: no cover - defensive
            return (None, None)

    def _is_shaking(self) -> bool:
        probe = getattr(self._reader, "is_shaking", None) if self._reader else None
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - defensive
            return False

    def _note_activity(self, activity: str) -> None:
        """Record an observed activity, stamping ``activity_since`` only when
        the value changes (§2.3: the start of the CURRENT span, not of the
        enclosing request — the previous implementation stamped every poll,
        which made every span look zero-length)."""

        if activity != self._activity:
            self._activity = activity
            self._activity_since = datetime.now(timezone.utc)

    def _sync_activity(self) -> str:
        self._note_activity(self._observed_activity())
        return self._activity

    @asynccontextmanager
    async def _operation(self, code: str, *, counts_as_cycle: bool = False):
        """Bracket one instrument operation: activity span, error capture,
        and the reserved cycle counter.

        The caller must already hold ``self._lock``. Stamping the span here
        (rather than letting the next poll notice) is what gives
        ``activity_since`` the true start of an in-flight read.
        """

        self._busy_state = True
        self._note_activity("running")
        try:
            yield
            if counts_as_cycle:
                self._cycles_total += 1
        except (PreconditionNotMet, ValueError) as exc:
            # §6.3: a refusal is NOT an operational failure. The equipment is
            # healthy and simply declined an inapplicable request (no plate
            # loaded, camera down, a channel whose filter cube is not fitted,
            # a well not on the plate). Recording these would drive the device
            # to `error` and light up the dashboard tile for something that
            # never broke — and would corrupt the meaning of `last_error` as
            # "the most recent thing that actually went wrong".
            logger.info("Cytation refused %s: %s", code, exc)
            raise
        except Exception as exc:
            self._record_error(exc, code)
            raise
        finally:
            self._busy_state = False
            self._note_activity(self._observed_activity())

    def _require_connected(self) -> None:
        if self._reader is None or not self._reader.is_connected():
            raise RuntimeError(
                "Cytation reader not initialised. POST /control/startup first."
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _safe_read(
        self,
        label: str,
        coro_fn: Callable[[], Any],
        readback_errors: list[str],
    ) -> Any:
        """Run an ``async`` getter and capture failures into
        ``readback_errors`` instead of raising. Synchronous getters work
        too: we ``await`` them iff they return an awaitable."""
        try:
            value = coro_fn()
            if asyncio.iscoroutine(value):
                value = await value
            return value
        except Exception as exc:
            readback_errors.append(f"{label}: {exc}")
            return None

    def _disconnected_components(self) -> dict[str, ComponentStatus]:
        comps: dict[str, ComponentStatus] = {
            "optics": ComponentStatus(connected=False, state="disconnected"),
            "incubator": ComponentStatus(connected=False, state="disconnected"),
            "plate_stage": ComponentStatus(connected=False, state="unknown"),
            "shaker": ComponentStatus(connected=False, state="disconnected"),
        }
        if self.imaging_enabled:
            comps["imaging"] = ComponentStatus(connected=False, state="disconnected")
        return comps

    def _connected_components(
        self, actual_temp: float | None
    ) -> dict[str, ComponentStatus]:
        optics_state = "busy" if self._busy_state else "idle"
        # Keyed on the setpoint we commanded rather than on a bare temperature
        # threshold. The old `actual >= 30 -> at_setpoint` heuristic called a
        # warm room "at setpoint" with the incubator off, and could never
        # report "ramping".
        incubator_message: str | None = None
        if self._setpoint_c is None:
            incubator_state = "off"
        elif actual_temp is None:
            incubator_state = "unknown"
            # Say *why*. The instrument does not answer the temperature query
            # while shaking (measured 2026-08-24), and a bare "unavailable"
            # here is what made the 2026-08-21 run's six-hour gap look like a
            # fault rather than a known limitation.
            incubator_message = (
                "Temperature not readable while shaking — pause the shaker to sample it"
                if self._is_shaking()
                else "Temperature readback unavailable"
            )
        elif abs(actual_temp - self._setpoint_c) <= _TEMPERATURE_TOLERANCE_C:
            incubator_state = "at_setpoint"
        else:
            incubator_state = (
                "heating" if actual_temp < self._setpoint_c else "cooling"
            )
            incubator_message = f"Ramping to {self._setpoint_c} C"

        comps: dict[str, ComponentStatus] = {
            "optics": ComponentStatus(connected=True, state=optics_state),
            "incubator": ComponentStatus(
                connected=True, state=incubator_state, message=incubator_message
            ),
            "plate_stage": ComponentStatus(connected=True, state=self._drawer),
            "shaker": ComponentStatus(
                connected=True, state="shaking" if self._is_shaking() else "idle"
            ),
        }
        if self.imaging_enabled:
            # `connected` tracks the camera, not the config flag. Reporting a
            # connected imager because someone wrote `enabled = true` told
            # every reader the opposite of the truth whenever PySpin or the
            # Blackfly was missing, and §2.2 forbids hiding a subsystem fault.
            camera_ready = self._camera_ready()
            comps["imaging"] = ComponentStatus(
                connected=camera_ready,
                state=("busy" if self._busy_state else "idle")
                if camera_ready
                else "disconnected",
                message=None if camera_ready else self._camera_error(),
            )
        return comps

    def _camera_ready(self) -> bool:
        if self._reader is None or not self.imaging_enabled:
            return False
        probe = getattr(self._reader, "camera_ready", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - defensive
            return False

    def _camera_error(self) -> str | None:
        probe = getattr(self._reader, "camera_error", None)
        if probe is None:
            return "Camera state unknown"
        try:
            return probe() or "Camera not initialised"
        except Exception:  # pragma: no cover - defensive
            return "Camera not initialised"

    def _plate_loaded(self) -> bool:
        """Is a plate assigned in the *reader's* resource tree?

        Deliberately asks the reader rather than the PlateStateStore: the
        store survives restarts, so it can claim a plate the freshly
        reconnected reader knows nothing about. Reads go through the reader,
        so the reader is the authority for whether one can succeed.
        """

        if self._reader is None:
            return False
        probe = getattr(self._reader, "has_plate", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - defensive
            return False

    def _base_details(
        self,
        *,
        drawer_override: str | None = None,
    ) -> dict[str, Any]:
        plate = self.plate_state.get()
        holder = self.claims.current()
        details: dict[str, Any] = {
            "drawer": drawer_override if drawer_override is not None else self._drawer,
            "backend": str(_config.get("instrument", "backend", "cytation5")),
            "imaging_enabled": self.imaging_enabled,
            "loaded_plate": plate.model_dump(mode="json") if plate else None,
            "claimed_by": holder.model_dump(mode="json") if holder else None,
            "claims_enforced": self.claims.enforce,
            # Whether the reader itself holds a plate, as distinct from what
            # the persisted store remembers — the two disagree after a
            # restart, and only the former permits a read.
            "plate_in_reader": self._plate_loaded(),
        }
        if self.imaging_enabled:
            camera_ready = self._camera_ready()
            imaging: dict[str, Any] = {
                "camera_ready": camera_ready,
                "camera_error": None if camera_ready else self._camera_error(),
            }
            # The instrument's own fit-out, read from its configuration.
            # Published so "that channel needs a cube you don't have" is
            # answerable from /status instead of by opening the box.
            inventory = getattr(self._reader, "optics_inventory", None)
            if inventory is not None:
                try:
                    optics = inventory()
                    imaging.update(
                        {
                            "installed_objectives": optics["objectives"],
                            "installed_filters": optics["filters"],
                            "objective_slots": optics["objective_slots"],
                            "filter_slots": optics["filter_slots"],
                        }
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
            # The driver refuses phase contrast on Cytation1 firmware, so
            # whether that channel exists is a property of this unit, not of
            # the request. `null` means the firmware version is unknown.
            phase = getattr(self._reader, "supports_phase_contrast", None)
            if phase is not None:
                try:
                    imaging["phase_contrast_available"] = phase()
                except Exception:  # pragma: no cover - defensive
                    pass
            details["imaging"] = imaging

        serial = getattr(self._reader, "serial_number", None) if self._reader else None
        if serial is not None:
            try:
                details["instrument_serial"] = serial()
            except Exception:  # pragma: no cover - defensive
                pass
        lo, hi = self._temperature_range()
        if lo is not None or hi is not None:
            details["temperature_range_c"] = {"min": lo, "max": hi}
        return details

    def _allowed_actions(self, state: str, activity: str = "idle") -> list[str]:
        """Return the skills this device will currently honor on /control/*.

        Mirrors the skill names declared in
        ``ac-organic-lab/skills/.../skill_catalog/plate_reader.py``
        (see docs/LABSKILLS.md). Always advertises
        claim verbs so an SDK can negotiate exclusive control before
        attempting any state-bound action.

        Gated on ``activity`` as well as on ``state`` (§2.3): while an
        operation is in flight, nothing that would start a second one is
        advertised. The two gates agree by construction today — ``busy`` is
        computed from the same ``_busy_state`` — but keying on activity is
        what the spec requires and what a future state can't quietly break.
        """
        always = ["claim", "heartbeat", "release"]
        if activity == "running":
            # §2.3 is explicit that abort/stop stay available while running.
            # Shaking made this urgent rather than academic: it outlives the
            # request that started it, so without `shake.stop` here the only
            # documented way to stop the plate moving would be `shutdown`.
            return [*always, *(["shake.stop"] if self._is_shaking() else [])]
        if state == "requires_init":
            return [*always, "startup"]
        if state == "dry_run" or state == "ready":
            # Two preconditions gate the optical actions, and both are
            # mirrored here because §6.2 requires that an action listed in
            # allowed_actions cannot be refused if invoked immediately:
            #
            #  - reads and captures need a plate assigned in the reader
            #    (PyLabRobot raises NoPlateError otherwise);
            #  - captures additionally need the camera initialised.
            plate_loaded = self._plate_loaded()
            can_image = self.imaging_enabled and self._camera_ready() and plate_loaded
            return [
                *always,
                "shutdown",
                "drawer.open",
                "drawer.close",
                "plate.load",
                "plate.unload",
                "well.update",
                "incubator.set_temperature",
                "incubator.stop",
                "shake.start",
                *(
                    [
                        "read.absorbance",
                        "read.fluorescence",
                        "read.luminescence",
                    ]
                    if plate_loaded
                    else []
                ),
                *(["imaging.capture"] if can_image else []),
            ]
        if state == "busy":
            return [*always]
        if state in ("error", "degraded"):
            return [*always, "shutdown"]
        return list(always)

    def _record_error(self, exc: Exception, code: str) -> None:
        self._last_error = ErrorInfo(
            code=code,
            message=describe(exc),
            severity="error",
            timestamp=datetime.now(timezone.utc),
        )
        logger.exception("Cytation error in %s", code)


def _safe_hostname() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        return None


__all__ = ["CytationService", "StubCytationReader"]
