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
from datetime import datetime, timezone
from typing import Any, Callable

from . import config as _config
from .claims import ClaimManager
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
# `ready` / `degraded` (matches the convention used by agilent_plateloc).
_RECENT_ERROR_WINDOW_S = 60.0


class CytationService:
    """Wraps a :class:`CytationReader` (or :class:`StubCytationReader`)
    and produces spec-compliant :class:`EquipmentStatus` snapshots.

    Concurrency: all driver I/O happens inside ``self._lock``. Status
    reads share the same lock so a poll cannot interleave with a write.
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
        self._last_read_at: float | None = None  # time.monotonic()
        self._read_count: int = 0

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
            except Exception as exc:
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

    # ------------------------------------------------------------------
    # Status (side-effect-free)
    # ------------------------------------------------------------------

    async def get_status(self) -> EquipmentStatus:
        """Produce a fresh status snapshot. MUST NOT mutate hardware state.

        The spec requires this endpoint to be safe to call every 2-3
        seconds and to always return HTTP 200 unless the process itself
        is broken. Per-getter failures fold into ``degraded`` rather
        than raise.
        """

        async with self._lock:
            return await self._build_status_locked()

    async def _build_status_locked(self) -> EquipmentStatus:
        now = datetime.now(timezone.utc)
        uptime = time.monotonic() - self._started_at
        host = _safe_hostname()

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
                message=(
                    "Driver not connected. POST /control/startup to "
                    "initialise the reader."
                ),
                required_actions=["startup"],
                allowed_actions=self._allowed_actions("requires_init"),
                device_time=now,
                uptime_seconds=uptime,
                components=self._disconnected_components(),
                # Stage state is unknown when disconnected; keep details
                # in sync with components.plate_stage.state.
                details=self._base_details(drawer_override="unknown"),
                last_error=self._last_error,
            )

        # ---- read what we can; never let a single getter fail status -----
        metrics: dict[str, MetricValue] = {}
        details: dict[str, Any] = self._base_details()
        readback_errors: list[str] = []

        actual_temp = await self._safe_read(
            "actual_temperature", self._reader.get_temperature, readback_errors
        )
        if actual_temp is not None:
            metrics["actual_temperature"] = MetricValue(
                value=float(actual_temp), unit="C", timestamp=now
            )

        if self._last_read_at is not None:
            metrics["last_read_seconds_ago"] = MetricValue(
                value=round(time.monotonic() - self._last_read_at, 3),
                unit="s",
            )
        metrics["read_count"] = MetricValue(value=int(self._read_count), unit="count")

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
            message=message,
            allowed_actions=self._allowed_actions(state),
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
            return self.plate_state.load_plate(
                plate_id=plate_id, model=chosen, wells=wells
            )

    async def unload_plate(self) -> LoadedPlate | None:
        """Clear the currently-loaded plate. Returns the prior plate (if any)."""
        async with self._lock:
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
            self._busy_state = True
            try:
                await self._reader.open_drawer()
                self._drawer = "out"
            except Exception as exc:
                self._record_error(exc, "drawer.open")
                raise
            finally:
                self._busy_state = False

    async def close_drawer(self) -> None:
        async with self._lock:
            self._require_connected()
            self._busy_state = True
            try:
                await self._reader.close_drawer()
                self._drawer = "in"
            except Exception as exc:
                self._record_error(exc, "drawer.close")
                raise
            finally:
                self._busy_state = False

    async def read_absorbance(
        self,
        *,
        wells: list[str],
        wavelength_nm: float,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            self._busy_state = True
            try:
                result = await self._reader.read_absorbance(
                    wells=wells, wavelength_nm=wavelength_nm
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result
            except Exception as exc:
                self._record_error(exc, "read.absorbance")
                raise
            finally:
                self._busy_state = False

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        gain: float = 50.0,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            self._busy_state = True
            try:
                result = await self._reader.read_fluorescence(
                    wells=wells,
                    excitation_nm=excitation_nm,
                    emission_nm=emission_nm,
                    gain=gain,
                    focal_height_mm=focal_height_mm,
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result
            except Exception as exc:
                self._record_error(exc, "read.fluorescence")
                raise
            finally:
                self._busy_state = False

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        integration_time_s: float = 1.0,
        gain: float = 50.0,
    ) -> dict[str, float]:
        async with self._lock:
            self._require_connected()
            self._busy_state = True
            try:
                result = await self._reader.read_luminescence(
                    wells=wells,
                    integration_time_s=integration_time_s,
                    gain=gain,
                )
                self._read_count += 1
                self._last_read_at = time.monotonic()
                return result
            except Exception as exc:
                self._record_error(exc, "read.luminescence")
                raise
            finally:
                self._busy_state = False

    async def capture_image(
        self,
        *,
        well: str,
        channel: str,
        focal_height_mm: float = 5.0,
        exposure_ms: float = 10.0,
        gain: float = 1.0,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_connected()
            if not self.imaging_enabled:
                raise RuntimeError(
                    "Imaging is disabled in config.toml ([imaging].enabled=false)"
                )
            self._busy_state = True
            try:
                return await self._reader.capture_image(
                    well=well,
                    channel=channel,
                    focal_height_mm=focal_height_mm,
                    exposure_ms=exposure_ms,
                    gain=gain,
                )
            except Exception as exc:
                self._record_error(exc, "imaging.capture")
                raise
            finally:
                self._busy_state = False

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
        }
        if self.imaging_enabled:
            comps["imaging"] = ComponentStatus(connected=False, state="disconnected")
        return comps

    def _connected_components(
        self, actual_temp: float | None
    ) -> dict[str, ComponentStatus]:
        optics_state = "busy" if self._busy_state else "idle"
        if actual_temp is None:
            incubator_state = "unknown"
        elif actual_temp >= 30.0:  # Cytation incubator default range; arbitrary
            incubator_state = "at_setpoint"
        else:
            incubator_state = "off"
        comps: dict[str, ComponentStatus] = {
            "optics": ComponentStatus(connected=True, state=optics_state),
            "incubator": ComponentStatus(connected=True, state=incubator_state),
            "plate_stage": ComponentStatus(connected=True, state=self._drawer),
        }
        if self.imaging_enabled:
            comps["imaging"] = ComponentStatus(
                connected=True,
                state="busy" if self._busy_state else "idle",
            )
        return comps

    def _base_details(
        self,
        *,
        drawer_override: str | None = None,
    ) -> dict[str, Any]:
        plate = self.plate_state.get()
        holder = self.claims.current()
        return {
            "drawer": drawer_override if drawer_override is not None else self._drawer,
            "backend": str(_config.get("instrument", "backend", "cytation5")),
            "imaging_enabled": self.imaging_enabled,
            "loaded_plate": plate.model_dump(mode="json") if plate else None,
            "claimed_by": holder.model_dump(mode="json") if holder else None,
            "claims_enforced": self.claims.enforce,
        }

    def _allowed_actions(self, state: str) -> list[str]:
        """Return the skills this device will currently honor on /control/*.

        Mirrors the skill names declared in
        ``ac-organic-lab/skills/.../skill_catalog/plate_reader.py``
        (Phase 4 -- see docs/phase4_handoff.md). Always advertises
        claim verbs so an SDK can negotiate exclusive control before
        attempting any state-bound action.
        """
        always = ["claim", "heartbeat", "release"]
        if state == "requires_init":
            return [*always, "startup"]
        if state == "dry_run" or state == "ready":
            return [
                *always,
                "shutdown",
                "drawer.open",
                "drawer.close",
                "plate.load",
                "plate.unload",
                "well.update",
                "read.absorbance",
                "read.fluorescence",
                "read.luminescence",
                *(["imaging.capture"] if self.imaging_enabled else []),
            ]
        if state == "busy":
            return [*always]
        if state in ("error", "degraded"):
            return [*always, "shutdown"]
        return list(always)

    def _record_error(self, exc: Exception, code: str) -> None:
        self._last_error = ErrorInfo(
            code=code,
            message=str(exc),
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
