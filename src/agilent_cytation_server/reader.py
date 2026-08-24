"""Thin wrapper around PyLabRobot's ``PlateReader`` + Cytation backend.

This module is the *only* place that imports PyLabRobot. Everything is
import-on-call so the test suite and the dry-run service can run on
macOS / Linux without ``pylabrobot`` installed at all.

The wrapper exposes the small surface the Phase 1 service actually
needs:

* :meth:`setup` / :meth:`stop` — connect / disconnect.
* :meth:`is_connected`        — cheap, side-effect-free.
* :meth:`get_temperature`     — read incubator temperature (best-effort).
* :meth:`open_drawer` / :meth:`close_drawer` — Phase 3+ reuse this.
* :meth:`read_absorbance` / :meth:`read_fluorescence` /
  :meth:`read_luminescence` — Phase 3+ reuse these.

Phase 1 only calls :meth:`setup`, :meth:`stop`, :meth:`is_connected`
and :meth:`get_temperature`. The read methods raise ``NotImplementedError``
on the stub and delegate to PyLabRobot on the real backend.

Two facts about PyLabRobot 0.2.1 shape everything below, and both were
found by reading its source rather than its docs:

1. **Reads need a ``Plate`` inside the ``PlateReader``.** The frontend's
   ``read_*`` methods call ``self.get_plate()``, which raises
   ``NoPlateError`` when no ``Plate`` child is assigned, and they want
   ``wells`` as ``Well`` *objects* from that plate — not well-name
   strings. They also return a row-major ``list[list[float]]`` grid, not
   a per-well mapping. :meth:`load_plate` and :meth:`_grid_to_wells`
   bridge all three.
2. **Imaging is not on the ``PlateReader`` frontend at all.** It lives on
   a separate ``Imager`` frontend, and ``Imager.capture()`` consults a
   hardcoded magnification -> image-size table covering only 4x/20x/40x —
   so it raises ``ValueError`` before touching the camera for any other
   objective. :meth:`capture_image` therefore drives the backend
   primitives directly, which is the path ``scripts/capture_a1.py`` proved
   on this hardware and what verified live on 2026-08-12.

   The three objectives currently fitted here (4x/20x/40x Phase) happen to
   fall inside that table, so ``Imager.capture()`` would work today. The
   primitive path is kept anyway: it survives refitting a 1.25x or 60x
   objective, and it is the only way to control LED intensity per capture.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as _config
from .errors import CameraNotReady, PlateNotLoaded
from .models import TEMPERATURE_MAX_C, TEMPERATURE_MIN_C

logger = logging.getLogger(__name__)


# Channel id (as accepted by POST /control/imaging/capture) -> the name of
# the PyLabRobot ``ImagingMode`` enum member. Aliases exist because the
# vendor, the spec note in docs/notes/reads_and_imaging.md, and PLR all
# spell these differently. Resolved lazily so this module stays
# importable without pylabrobot.
_CHANNEL_ALIASES: dict[str, str] = {
    "brightfield": "BRIGHTFIELD",
    "bright_field": "BRIGHTFIELD",
    "bf": "BRIGHTFIELD",
    "color_brightfield": "COLOR_BRIGHTFIELD",
    "phase_contrast": "PHASE_CONTRAST",
    "phase": "PHASE_CONTRAST",
    "dapi": "DAPI",
    "uv": "DAPI",
    "gfp": "GFP",
    "fitc": "FITC",
    "rfp": "RFP",
    "texas_red": "TEXAS_RED",
    "cy5": "CY5",
    "cfp": "CFP",
    "yfp": "YFP",
}


_FTDI_ENUMERATION_PATCHED = False


def _patch_pylabrobot_ftdi_enumeration() -> None:
    """Make PyLabRobot's FTDI device enumeration skip devices libusb can't open.

    PyLabRobot's ``FTDI._resolve_device_serial`` walks every FTDI-VID/PID
    device on the bus and calls ``usb.util.get_string`` on each to read
    its serial number. ``get_string`` requires opening the device, which
    fails on Windows with ``NotImplementedError("Operation not supported
    or unimplemented on this platform")`` for any FTDI device bound to
    the FTDI vendor driver (FTDIBUS/FTSER2K) rather than libusbK. The
    upstream code catches ``ValueError`` in its diagnostic-listing loop
    but not in the candidate loop, so a single co-resident FTDI VCP on
    the PC blocks the Cytation from ever being matched.

    This patch wraps the serial read in a broader ``try/except`` so
    unopenable devices are skipped instead of aborting enumeration.
    """
    global _FTDI_ENUMERATION_PATCHED
    if _FTDI_ENUMERATION_PATCHED:
        return

    from typing import cast
    import pylibftdi.driver
    import usb.core
    import usb.util
    from pylabrobot.io.ftdi import FTDI

    def _resolve_device_serial(self) -> str:
        search_kwargs: dict[str, Any] = {}
        if self._vid is not None:
            search_kwargs["idVendor"] = self._vid
        if self._pid is not None:
            search_kwargs["idProduct"] = self._pid

        candidates = []
        skipped = []
        for device in usb.core.find(find_all=True, **search_kwargs):
            if self._vid is None and device.idVendor not in pylibftdi.driver.USB_VID_LIST:
                continue
            if self._vid is not None and device.idVendor != self._vid:
                continue
            if self._pid is None and device.idProduct not in pylibftdi.driver.USB_PID_LIST:
                continue
            if self._pid is not None and device.idProduct != self._pid:
                continue

            try:
                serial = usb.util.get_string(device, device.iSerialNumber)
            except (NotImplementedError, usb.core.USBError, ValueError) as exc:
                skipped.append(f"{device.idVendor:04x}:{device.idProduct:04x} ({type(exc).__name__}: {exc})")
                continue

            if self._device_id is not None and serial != self._device_id:
                continue
            candidates.append((device, serial))

        if skipped:
            logger.info("Skipped %d FTDI device(s) libusb could not open: %s", len(skipped), skipped)

        if len(candidates) == 0:
            raise RuntimeError(
                f"No FTDI devices matched device_id={self._device_id!r}. "
                f"Skipped {len(skipped)} unopenable device(s): {skipped}."
            )
        if len(candidates) > 1:
            raise RuntimeError(
                f"Multiple FTDI devices matched device_id={self._device_id!r}; "
                "pin device_id explicitly in config.toml [instrument].usb_serial."
            )
        return cast(str, candidates[0][1])

    FTDI._resolve_device_serial = _resolve_device_serial
    _FTDI_ENUMERATION_PATCHED = True
    logger.info("Patched pylabrobot.io.ftdi.FTDI._resolve_device_serial to skip unopenable devices")


class CytationReader:
    """Real PyLabRobot-backed Cytation 5 reader.

    Constructed lazily — :meth:`setup` is what actually instantiates the
    PyLabRobot ``PlateReader`` + ``Cytation5Backend``. Lifespan code in
    ``api.py`` invokes :meth:`setup` with a timeout; failures are swallowed
    by the service (the device falls into ``requires_init`` and the
    operator retries via a future ``POST /control/startup``).
    """

    def __init__(
        self,
        *,
        usb_serial: str | None = None,
        imaging_enabled: bool | None = None,
        captures_dir: str | Path | None = None,
    ) -> None:
        self.usb_serial = usb_serial or None
        if imaging_enabled is None:
            imaging_enabled = bool(_config.get("imaging", "enabled", True))
        self.imaging_enabled = imaging_enabled
        self._captures_dir = Path(
            captures_dir
            or _config.get("imaging", "captures_dir", None)
            or Path(__file__).resolve().parents[2] / "captures"
        )
        self._reader: Any | None = None
        self._backend: Any | None = None
        self._plate: Any | None = None
        self._plate_model: str | None = None
        self._camera_ready = False
        self._camera_error: str | None = None
        self._firmware_version: str | None = None
        self._serial_number: str | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Connect to the Cytation over USB.

        Imports PyLabRobot lazily so this module is import-safe on
        platforms without it.
        """
        try:
            from pylabrobot.plate_reading import PlateReader  # type: ignore[import-not-found]
            from pylabrobot.plate_reading.agilent.biotek_cytation_backend import (  # type: ignore[import-not-found]
                CytationBackend,
            )
        except ImportError as exc:  # pragma: no cover - exercised on dev boxes only
            raise ImportError(
                "pylabrobot is required to drive the real Cytation 5. "
                "Install with `uv sync --extra plr --extra windows`."
            ) from exc

        # `[instrument].ftdi_transport = "d2xx"` drives the reader through
        # FTDI's vendor driver instead of libusb, which removes the Zadig swap
        # in RUNBOOK §4/§5 entirely — Gen5 and this service then coexist and
        # switching is a service stop. Defaults to "libusb" (the shipped,
        # bench-verified path); see docs/GEN5_ABSORBANCE.md.
        transport = str(_config.get("instrument", "ftdi_transport", "libusb")).lower()
        if transport == "d2xx":
            from .ftd2xx_shim import install as _install_d2xx

            trace = bool(_config.get("instrument", "ftdi_trace", False))
            _install_d2xx(trace=trace)
            logger.info(
                "FTDI transport: D2XX (vendor driver; no libusbK bind needed)%s",
                " [TRACE ON]" if trace else "",
            )
        else:
            _patch_pylabrobot_ftdi_enumeration()

        backend_kwargs: dict[str, Any] = {}
        if self.usb_serial:
            backend_kwargs["device_id"] = self.usb_serial

        # `CytationBackend`, not the `Cytation5Backend` alias: the latter is
        # deprecated upstream and only exists to emit a FutureWarning.
        #
        # The subclass exists solely to correct `temperature_range`. Upstream
        # hardcodes (4.0, 45.0) for every Cytation — `supports_cooling` returns
        # True unconditionally and 45.0 is commented "default BioTek max" — and
        # `CytationBackend.set_temperature` validates against that property. So
        # without this, the driver refuses setpoints this instrument accepts.
        # The real limits come from the unit itself; see models.py.
        class _RangeCorrectedBackend(CytationBackend):  # type: ignore[misc,valid-type]
            @property
            def temperature_range(self) -> tuple[float | None, float | None]:
                return (TEMPERATURE_MIN_C, TEMPERATURE_MAX_C)

        backend = _RangeCorrectedBackend(**backend_kwargs)
        self._backend = backend
        self._reader = PlateReader(
            name="cytation_5",
            size_x=500,
            size_y=400,
            size_z=300,
            backend=backend,
        )

        # The camera is brought up inside the same setup() call, because
        # that is the only place PyLabRobot will do it (`use_cam` is a
        # backend-setup kwarg, forwarded through Machine.setup).
        #
        # A camera failure must NOT cost us the reader: absorbance and
        # fluorescence are the more important half of this device, and
        # PySpin being absent or the Blackfly being claimed by SpinView is
        # a routine condition. On failure the backend closes the FTDI
        # handle and re-raises (so the retry below can reopen it), which is
        # why this is a full second setup() rather than a resumption.
        if self.imaging_enabled:
            try:
                await self._reader.setup(use_cam=True)
                self._camera_ready = True
                self._camera_error = None
            except Exception as exc:
                self._camera_ready = False
                self._camera_error = str(exc)
                logger.warning(
                    "Cytation camera setup failed; continuing reader-only. "
                    "Imaging actions will be withheld from allowed_actions. (%s)",
                    exc,
                )
                await self._reader.setup()
        else:
            await self._reader.setup()

        self._connected = True

        # Installed objectives / filter cubes are read from the instrument's
        # own configuration (whatever was registered in Gen5's Instrument
        # Configuration -> Imager). PLR only loads them at the tail of its
        # camera setup, so on the reader-only path we ask for them
        # explicitly — they come over the serial link, not from the camera,
        # so this works even with PySpin missing. Purely informational, so
        # never fatal.
        await self._load_optics_inventory()
        await self._load_identity()

        logger.info(
            "Cytation connected (usb_serial=%r, firmware=%r, camera_ready=%s)",
            self.usb_serial,
            self._firmware_version,
            self._camera_ready,
        )

    async def _load_identity(self) -> None:
        """Read firmware version and serial number once, at connect.

        Cached rather than polled: they never change while connected, and
        `/status` must not issue instrument I/O on the request path. The
        firmware version is load-bearing beyond bookkeeping — the driver
        refuses phase-contrast imaging when it starts with "1" (its Cytation1
        discriminator), so this is what tells an operator whether that channel
        is available at all.
        """

        backend = self._backend
        if backend is None:
            return
        for attr, target in (
            ("get_firmware_version", "_firmware_version"),
            ("get_serial_number", "_serial_number"),
        ):
            fn = getattr(backend, attr, None)
            if fn is None:
                continue
            try:
                value = await fn()
            except Exception as exc:  # pragma: no cover - hardware-specific
                logger.debug("Cytation %s failed: %s", attr, exc)
                continue
            if value:
                setattr(self, target, str(value).strip())

    def supports_phase_contrast(self) -> bool | None:
        """Whether the driver will accept PHASE_CONTRAST on this unit.

        ``None`` when the firmware version is unknown. PyLabRobot raises
        ``NotImplementedError`` for phase contrast on Cytation1, which it
        identifies by the firmware version string starting with "1".
        """

        if self._firmware_version is None:
            return None
        return not self._firmware_version.startswith("1")

    async def _load_optics_inventory(self) -> None:
        """Best-effort population of the backend's objective / filter lists.

        Answers "what imaging hardware is physically installed" on
        ``/status``, so an operator can tell a missing filter cube from a
        software problem without opening the instrument.
        """

        backend = self._backend
        if backend is None:
            return
        for attr, loader in (("_filters", "_load_filters"), ("_objectives", "_load_objectives")):
            if getattr(backend, attr, "unset") is not None:
                continue
            fn = getattr(backend, loader, None)
            if fn is None:
                continue
            try:
                await fn()
            except Exception as exc:  # pragma: no cover - hardware-specific
                logger.debug("Cytation %s failed: %s", loader, exc)

    async def stop(self) -> None:
        if self._reader is None:
            self._connected = False
            return
        try:
            await self._reader.stop()
        finally:
            self._reader = None
            self._connected = False

    # ------------------------------------------------------------------
    # Cheap, side-effect-free queries (used by GET /status)
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        return self._connected and self._reader is not None

    def camera_ready(self) -> bool:
        """Is the imaging path actually usable *right now*?

        This is what gates ``imaging.capture`` in ``allowed_actions``. It is
        deliberately not ``config[imaging].enabled``: that flag says imaging
        is wanted, not that a camera was found, and advertising an action the
        device would 500 on violates STATUS_SPEC §6.2.
        """

        return self._connected and self._camera_ready

    def camera_error(self) -> str | None:
        """Why the camera is unavailable, or ``None`` if it is fine."""

        return self._camera_error

    def installed_objectives(self) -> list[str]:
        return self._enum_names("objectives")

    def installed_filters(self) -> list[str]:
        return self._enum_names("filters")

    def optics_inventory(self) -> dict[str, Any]:
        """What is fitted, and whether we actually managed to ask.

        An empty list and a failed query are *not* the same claim — the first
        says "no filter cubes are installed", the second says "we don't
        know" — and conflating them would send someone hunting for hardware
        that is present, or vice versa. Same discipline §2.1 imposes on
        ``unknown`` versus a real answer, applied one level down.

        ``*_slots`` is the number of physical positions the instrument
        reported (``None`` when the query never succeeded), so a reader can
        see 4 empty turret positions as distinct from silence.
        """

        return {
            "objectives": self._enum_names("objectives"),
            "filters": self._enum_names("filters"),
            "objective_slots": self._slot_count("objectives"),
            "filter_slots": self._slot_count("filters"),
        }

    def _raw_optics(self, attr: str) -> list[Any] | None:
        """The backend's fixed-length slot list, or ``None`` if never loaded.

        Both properties raise rather than return ``None`` when the
        instrument was never queried, which is what makes the distinction
        recoverable here.
        """

        backend = self._backend
        if backend is None:
            return None
        try:
            return list(getattr(backend, attr) or [])
        except Exception:
            return None

    def _enum_names(self, attr: str) -> list[str]:
        entries = self._raw_optics(attr)
        if entries is None:
            return []
        return [e.name for e in entries if e is not None]

    def _slot_count(self, attr: str) -> int | None:
        entries = self._raw_optics(attr)
        return None if entries is None else len(entries)

    # ------------------------------------------------------------------
    # Plate residency
    #
    # PyLabRobot models the plate as a child resource of the PlateReader,
    # and every read goes through `get_plate()`. Keeping the resource tree
    # in step with the service's own PlateStateStore is what makes reads
    # possible at all.
    # ------------------------------------------------------------------

    def has_plate(self) -> bool:
        return self._plate is not None

    def load_plate(self, *, plate_id: str, model: str | None = None) -> str:
        """Assign a ``Plate`` resource, returning the model actually used."""

        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")

        from .plates import PLATE_FACTORIES

        model = model or str(_config.get("plates", "default_model", "custom_96"))
        factory = PLATE_FACTORIES.get(model)
        if factory is None:
            raise ValueError(
                f"Unknown plate model {model!r}. Known: {sorted(PLATE_FACTORIES)}"
            )

        self.unload_plate()
        # PLR resource names must be unique within a tree; the plate_id is
        # operator-supplied and may repeat across loads, so it is only a
        # suffix here.
        plate = factory(name=f"plate_{plate_id}")
        self._reader.assign_child_resource(plate)
        self._plate = plate
        self._plate_model = model
        return model

    def unload_plate(self) -> None:
        if self._reader is None or self._plate is None:
            self._plate = None
            self._plate_model = None
            return
        try:
            self._reader.unassign_child_resource(self._plate)
        except Exception as exc:  # pragma: no cover - resource-tree specific
            logger.debug("Cytation unassign plate failed: %s", exc)
        finally:
            self._plate = None
            self._plate_model = None

    def _require_plate(self) -> Any:
        if self._plate is None:
            raise PlateNotLoaded()
        return self._plate

    def _wells_for(self, names: list[str]) -> list[Any]:
        """Resolve well-name strings to the plate's own ``Well`` objects.

        PyLabRobot compares ``well.parent`` against the plate it was handed,
        so these must be the *same* objects, not equivalents.
        """

        plate = self._require_plate()
        wells = []
        for name in names:
            try:
                wells.append(plate.get_item(name))
            except (IndexError, KeyError) as exc:
                raise ValueError(f"Well {name!r} is not on this plate") from exc
        return wells

    @staticmethod
    def _grid_to_wells(grid: Any, wells: list[Any], names: list[str]) -> dict[str, float]:
        """Pick the requested wells out of PyLabRobot's row-major grid.

        PLR returns a full plate-shaped ``list[list[float]]`` with ``None``
        in the positions it did not read, so the caller's well list is what
        selects the meaningful cells.
        """

        out: dict[str, float] = {}
        for name, well in zip(names, wells):
            row, col = well.get_row(), well.get_column()
            try:
                value = grid[row][col]
            except (IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"Reader returned no data for well {name} (row={row}, col={col})"
                ) from exc
            if value is None:
                raise RuntimeError(f"Reader returned no data for well {name}")
            out[name] = float(value)
        return out

    async def get_temperature(self) -> float | None:
        """Return the incubator temperature in degrees Celsius, or
        ``None`` if the backend cannot answer.

        Wrapped to never raise: the spec requires ``GET /status`` to
        always return HTTP 200, so per-getter failures must fold into
        ``degraded`` rather than crash.
        """
        # `get_current_temperature` lives on the BACKEND. The frontend
        # `PlateReader` has no temperature method at all, so looking it up
        # there silently returned None on every poll — which is why
        # `components.incubator` read `unknown` and no `actual_temperature`
        # metric was ever published.
        if self._backend is None:
            return None
        getter = getattr(self._backend, "get_current_temperature", None)
        if getter is None:
            return None
        # Never issue instrument I/O while the shake task owns the link:
        # `send_command` has no internal lock, so two concurrent callers
        # interleave writes and steal each other's replies. A stale reading
        # (with `readback_age_s` saying so) beats a corrupted exchange.
        if self.is_shaking():
            return None
        try:
            value = await getter()
        except Exception:  # pragma: no cover - hardware-specific
            logger.debug("Cytation get_current_temperature failed", exc_info=True)
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    # ------------------------------------------------------------------
    # Incubator
    # ------------------------------------------------------------------

    def temperature_range(self) -> tuple[float | None, float | None]:
        """``(min_c, max_c)`` the driver will accept, or ``(None, None)``.

        Note the driver hardcodes ``supports_cooling = True`` for every
        Cytation, which is what produces the 4 °C floor. Active cooling below
        ambient is not fitted on every unit, so a low setpoint may be accepted
        here and quietly ignored by the hardware — verify on the bench before
        an assay depends on it.
        """

        if self._backend is None:
            return (None, None)
        try:
            lo, hi = self._backend.temperature_range
            return (
                float(lo) if lo is not None else None,
                float(hi) if hi is not None else None,
            )
        except Exception:
            return (None, None)

    async def set_temperature(self, celsius: float) -> None:
        if self._backend is None:
            raise RuntimeError("Cytation reader is not connected")
        await self._backend.set_temperature(celsius)

    async def stop_temperature_control(self) -> None:
        if self._backend is None:
            raise RuntimeError("Cytation reader is not connected")
        await self._backend.stop_heating_or_cooling()

    # ------------------------------------------------------------------
    # Shaker
    # ------------------------------------------------------------------

    def is_shaking(self) -> bool:
        """Cheap, side-effect-free — safe to call from the status path.

        Reads the backend's own flag rather than tracking our own, so a shake
        that the driver ended (or never started) cannot leave us asserting
        motion that isn't happening.
        """

        backend = self._backend
        if backend is None:
            return False
        return bool(getattr(backend, "_shaking", False))

    async def shake(self, *, pattern: str = "orbital", displacement_mm: int = 3) -> None:
        """Start shaking and return once motion has begun.

        ``displacement_mm`` is PyLabRobot's ``frequency`` argument, which is
        not a frequency: it is the orbit displacement in mm (1-6), inversely
        related to speed — 6 mm is ~360 CPM and 1 mm is ~1096 CPM.

        The driver cannot shake indefinitely. Duration is set on the
        instrument with a 16-minute ceiling, so PyLabRobot keeps a background
        task alive that re-issues the command every 16 minutes; its own
        docstring warns the door may briefly open at each boundary. Fine for
        a mix before a read, not something to leave running unattended.
        """

        if self._backend is None:
            raise RuntimeError("Cytation reader is not connected")
        from pylabrobot.plate_reading.agilent.biotek_backend import (  # type: ignore[import-not-found]
            BioTekPlateReaderBackend,
        )

        try:
            shake_type = BioTekPlateReaderBackend.ShakeType[pattern.strip().upper()]
        except KeyError as exc:
            raise ValueError(
                f"Unknown shake pattern {pattern!r}. Known: linear, orbital"
            ) from exc
        if not 1 <= displacement_mm <= 6:
            raise ValueError("displacement_mm must be between 1 and 6")
        await self._backend.shake(shake_type, displacement_mm)

    async def stop_shaking(self) -> None:
        if self._backend is None:
            raise RuntimeError("Cytation reader is not connected")
        await self._backend.stop_shaking()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def firmware_version(self) -> str | None:
        return self._firmware_version

    def serial_number(self) -> str | None:
        return self._serial_number

    # ------------------------------------------------------------------
    # Drawer + measurements (Phase 3+ surface — declared here so Phase 1
    # status_builder can reference the same symbols).
    # ------------------------------------------------------------------

    async def open_drawer(self) -> None:
        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")
        await self._reader.open()

    async def close_drawer(self) -> None:
        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")
        await self._reader.close()

    # ------------------------------------------------------------------
    # Reads.
    #
    # Every read follows the same three steps, because PyLabRobot's
    # frontend demands them: resolve names to this plate's Well objects,
    # ask for the structured return type, then project the plate-shaped
    # grid back down to the wells the caller asked for.
    #
    # `use_new_return_type=True` is not optional politeness — the legacy
    # path returns `result[0]["data"]` and logs a deprecation warning on
    # every read, and the structured form is what carries the temperature
    # and timestamp we surface for traceability.
    # ------------------------------------------------------------------

    async def read_absorbance(
        self,
        *,
        wells: list[str],
        wavelength_nm: float,
    ) -> dict[str, float]:
        well_objs = self._wells_for(wells)
        result = await self._call_frontend(
            "read_absorbance",
            wavelength=int(round(wavelength_nm)),
            wells=well_objs,
            use_new_return_type=True,
        )
        return self._grid_to_wells(result[0]["data"], well_objs, wells)

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        well_objs = self._wells_for(wells)
        result = await self._call_frontend(
            "read_fluorescence",
            excitation_wavelength=int(round(excitation_nm)),
            emission_wavelength=int(round(emission_nm)),
            focal_height=focal_height_mm,
            wells=well_objs,
            use_new_return_type=True,
        )
        return self._grid_to_wells(result[0]["data"], well_objs, wells)

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        focal_height_mm: float = 7.0,
        integration_time_s: float = 1.0,
    ) -> dict[str, float]:
        well_objs = self._wells_for(wells)
        result = await self._call_frontend(
            "read_luminescence",
            focal_height=focal_height_mm,
            integration_time=integration_time_s,
            wells=well_objs,
            use_new_return_type=True,
        )
        return self._grid_to_wells(result[0]["data"], well_objs, wells)

    async def _call_frontend(self, attr: str, **kwargs: Any) -> Any:
        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")
        fn = getattr(self._reader, attr, None)
        if fn is None:
            raise RuntimeError(
                f"PyLabRobot PlateReader does not expose {attr}; verify the "
                "installed pylabrobot version matches what the cytation "
                "server expects."
            )
        return await fn(**kwargs)

    # ------------------------------------------------------------------
    # Imaging
    # ------------------------------------------------------------------

    # Search budget for the auto-* helpers. Each round is a real exposure on
    # the sample, so this is a photobleaching / phototoxicity budget as much
    # as a time one — keep it small.
    _AUTO_ROUNDS = 8
    _FOCUS_TOLERANCE_MM = 0.005
    _DEFAULT_EXPOSURE_MS = 10.0

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
        autofocus_range: tuple[float, float] = (4.5, 13.88),
        auto_exposure_range: tuple[float, float] = (0.1, 500.0),
        auto_exposure_target: float = 0.8,
    ) -> dict[str, Any]:
        """Capture one image of one well and write it to the captures dir.

        This drives the backend primitives rather than PyLabRobot's
        ``Imager.capture()``. That is not a shortcut: ``Imager.capture()``
        consults a hardcoded magnification -> image-size table that knows
        only 4x/20x/40x and raises ``ValueError`` before touching the
        camera for any other objective. The 4x/20x/40x Phase objectives
        currently fitted fall inside that table, but the primitive path is
        kept regardless: it survives refitting glass the table doesn't
        know (e.g. 1.25x or 60x), and it is the only way to set LED
        intensity per capture. ``scripts/capture_a1.py`` established this
        sequence on the real unit; keep the two in step.
        """

        if self._reader is None or self._backend is None:
            raise RuntimeError("Cytation reader is not connected")
        if not self.imaging_enabled:
            raise RuntimeError(
                "Imaging is disabled in config.toml ([imaging].enabled=false)"
            )
        if not self._camera_ready:
            raise CameraNotReady(
                "Imaging camera is not initialised. Check the Spinnaker SDK / "
                "PySpin install, then POST /control/shutdown followed by "
                "/control/startup.",
                camera_error=self._camera_error,
            )

        backend = self._backend
        mode = self._resolve_channel(channel)
        obj = self._resolve_objective(objective)
        well_obj = self._wells_for([well])[0]
        plate = self._require_plate()

        await backend.set_plate(plate)
        await backend.set_objective(obj)
        await backend.set_imaging_mode(mode, led_intensity=led_intensity)
        # `select` is 1-based here: Well.get_row()/get_column() are 0-based,
        # and the +1 is what capture_a1.py verified against the hardware.
        # (PLR's own Imager.capture passes 0-based values straight through,
        # which is an upstream off-by-one we deliberately do not copy.)
        await backend.select(row=well_obj.get_row() + 1, column=well_obj.get_column() + 1)
        await backend.set_gain(gain)
        # Centre of the selected well.
        await backend.set_position(0.0, 0.0)

        backend.start_acquisition()
        tuning: dict[str, Any] = {}
        try:
            if auto_exposure:
                exposure_ms = await self._auto_exposure(
                    focal_height_mm=focal_height_mm,
                    low_ms=auto_exposure_range[0],
                    high_ms=auto_exposure_range[1],
                    target_fraction=auto_exposure_target,
                    tuning=tuning,
                )
            if autofocus:
                focal_height_mm = await self._auto_focus(
                    exposure_ms=exposure_ms,
                    low_mm=autofocus_range[0],
                    high_mm=autofocus_range[1],
                    tuning=tuning,
                )
            image = await self._acquire_at(focal_height_mm, exposure_ms)
        finally:
            backend.stop_acquisition()
            try:
                await backend.led_off()
            except Exception as exc:  # pragma: no cover - hardware-specific
                # Leaving an LED on is a real (if minor) hazard for live
                # samples, so it is worth a warning rather than silence.
                logger.warning("Cytation led_off failed after capture: %s", exc)

        payload = self._save_capture(
            image,
            well=well,
            channel=mode.name,
            objective=obj.name,
            focal_height_mm=focal_height_mm,
            exposure_ms=exposure_ms,
            gain=gain,
        )
        if tuning:
            payload["tuning"] = tuning
        return payload

    async def _acquire_at(self, focal_height_mm: float, exposure_ms: float) -> Any:
        """Set focus + exposure and grab one frame. Acquisition must be live."""

        backend = self._backend
        await backend.set_exposure(exposure_ms)
        await backend.set_focus(focal_height_mm)
        return await backend._acquire_image()  # noqa: SLF001 - see capture_image

    async def _auto_exposure(
        self,
        *,
        focal_height_mm: float,
        low_ms: float,
        high_ms: float,
        target_fraction: float,
        tuning: dict[str, Any],
    ) -> float:
        """Binary-search exposure until the peak pixel sits near target.

        Uses PyLabRobot's ``max_pixel_at_fraction`` evaluator — deliberately
        *not* its ``fraction_overexposed``, which counts pixels strictly
        greater than ``max_pixel_value`` (255 by default) in a uint8 array.
        Nothing can exceed 255, so that evaluator always sees a fraction of
        zero and drives exposure upward until the frame clips — the opposite
        of what it promises.
        """

        from pylabrobot.plate_reading.imager import (  # type: ignore[import-not-found]
            max_pixel_at_fraction,
        )

        evaluate = max_pixel_at_fraction(fraction=target_fraction, margin=0.05)
        lo, hi = low_ms, high_ms
        chosen = min(max(self._DEFAULT_EXPOSURE_MS, lo), hi)
        for _ in range(self._AUTO_ROUNDS):
            image = await self._acquire_at(focal_height_mm, chosen)
            verdict = await evaluate(image)
            if verdict == "good":
                break
            if verdict == "lower":
                hi = chosen
            else:
                lo = chosen
            nxt = (lo + hi) / 2.0
            if abs(nxt - chosen) < 0.01:
                break
            chosen = nxt
        tuning["auto_exposure"] = {
            "exposure_ms": round(chosen, 3),
            "target_peak_fraction": target_fraction,
        }
        return chosen

    async def _auto_focus(
        self,
        *,
        exposure_ms: float,
        low_mm: float,
        high_mm: float,
        tuning: dict[str, Any],
    ) -> float:
        """Golden-section search on focal height, maximising sharpness.

        Sharpness is PyLabRobot's ``evaluate_focus_nvmg_sobel`` (normalised
        variance of Sobel gradient magnitude over the centre of the frame).
        Golden section costs one acquisition per iteration instead of two,
        which matters when each frame is a real exposure on live cells.
        """

        from pylabrobot.plate_reading.imager import (  # type: ignore[import-not-found]
            evaluate_focus_nvmg_sobel,
        )

        invphi = (5**0.5 - 1) / 2
        a, b = low_mm, high_mm
        c, d = b - invphi * (b - a), a + invphi * (b - a)
        fc = evaluate_focus_nvmg_sobel(await self._acquire_at(c, exposure_ms))
        fd = evaluate_focus_nvmg_sobel(await self._acquire_at(d, exposure_ms))
        for _ in range(self._AUTO_ROUNDS):
            if abs(b - a) < self._FOCUS_TOLERANCE_MM:
                break
            if fc > fd:
                b, d, fd = d, c, fc
                c = b - invphi * (b - a)
                fc = evaluate_focus_nvmg_sobel(await self._acquire_at(c, exposure_ms))
            else:
                a, c, fc = c, d, fd
                d = a + invphi * (b - a)
                fd = evaluate_focus_nvmg_sobel(await self._acquire_at(d, exposure_ms))
        best = c if fc > fd else d
        tuning["autofocus"] = {
            "focal_height_mm": round(best, 4),
            "sharpness": round(float(max(fc, fd)), 6),
        }
        return best

    def _resolve_channel(self, channel: str) -> Any:
        from pylabrobot.plate_reading.standard import ImagingMode  # type: ignore[import-not-found]

        key = channel.strip().lower().replace("-", "_").replace(" ", "_")
        member = _CHANNEL_ALIASES.get(key, key.upper())
        try:
            mode = ImagingMode[member]
        except KeyError as exc:
            raise ValueError(
                f"Unknown imaging channel {channel!r}. "
                f"Known: {sorted(_CHANNEL_ALIASES)}"
            ) from exc

        # A fluorescence channel needs its filter cube physically fitted.
        # Brightfield/phase contrast use the white LED through the condenser
        # and no cube, so they work on any imaging-equipped unit.
        #
        # The gate keys on `filter_slots`, not on the truthiness of the list:
        # an *empty* list means "queried, nothing fitted" and must refuse
        # every fluorescence channel, whereas a failed query (slots None)
        # must not refuse anything — we would be guessing. Testing `if
        # installed` conflated the two and let a DAPI request through to the
        # backend, which failed with `<ImagingMode.DAPI: 16> is not in list`
        # from an internal `.index()` (observed live 2026-08-12).
        transmitted = {"BRIGHTFIELD", "COLOR_BRIGHTFIELD", "PHASE_CONTRAST"}
        if mode.name not in transmitted:
            slots = self._slot_count("filters")
            installed = self.installed_filters()
            if slots is not None and mode.name not in installed:
                raise ValueError(
                    f"Channel {mode.name} needs a filter cube that is not "
                    f"installed. This instrument reports {slots} cube slot(s), "
                    f"fitted with: {installed or 'none'}. Fluorescence imaging "
                    "requires the matching cube in the filter wheel."
                )
        return mode

    def _resolve_objective(self, requested: str | None) -> Any:
        from pylabrobot.plate_reading.standard import Objective  # type: ignore[import-not-found]

        installed = self.installed_objectives()
        if requested:
            # Objective member names are mixed-case (`O_4X_PL_FL_Phase`), so
            # upper-casing the request rejected the instrument's own reported
            # names — it listed the objective as installed and refused it in
            # the same breath (observed live 2026-08-12). Match exactly first,
            # then case-insensitively.
            name = requested.strip()
            obj = getattr(Objective, name, None)
            if obj is None:
                obj = next(
                    (m for m in Objective if m.name.lower() == name.lower()), None
                )
            if obj is None:
                raise ValueError(
                    f"Unknown objective {requested!r}. Installed: {installed}"
                )
            if installed and obj.name not in installed:
                raise ValueError(
                    f"Objective {obj.name} is not installed. Installed: {installed}"
                )
            return obj

        if not installed:
            raise RuntimeError(
                "The Cytation reported no objectives. Register them in Gen5 "
                "(Instrument Configuration -> Imager) and reconnect."
            )
        # Default to the widest field of view, which is the safest choice for
        # an unattended whole-well capture.
        return min((Objective[n] for n in installed), key=lambda o: o.magnification)

    def _save_capture(
        self,
        image: Any,
        *,
        well: str,
        channel: str,
        objective: str,
        focal_height_mm: float,
        exposure_ms: float,
        gain: float,
    ) -> dict[str, Any]:
        try:
            from PIL import Image as PILImage  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dev boxes
            raise RuntimeError(
                "pillow is required to save captures. "
                "Install with `uv sync --extra imaging`."
            ) from exc

        self._captures_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        filename = (
            f"{well}_{channel.lower()}_{stamp}"
            f"_f{focal_height_mm:.2f}_e{exposure_ms:.1f}.png"
        )
        path = self._captures_dir / filename
        PILImage.fromarray(image).save(path)

        # Pixel stats travel with the result because focus and exposure are
        # tuned from them, and a caller that only gets a path has to reopen
        # the file to learn whether the frame was black.
        stats: dict[str, Any] = {}
        try:
            stats = {
                "min": int(image.min()),
                "max": int(image.max()),
                "mean": round(float(image.mean()), 2),
            }
        except Exception:  # pragma: no cover - non-array image
            pass

        return {
            "well": well,
            "channel": channel,
            "objective": objective,
            "focal_height_mm": focal_height_mm,
            "exposure_ms": exposure_ms,
            "gain": gain,
            "image_path": str(path),
            "width": int(image.shape[1]) if hasattr(image, "shape") else None,
            "height": int(image.shape[0]) if hasattr(image, "shape") else None,
            "pixel_stats": stats,
        }


class StubCytationReader:
    """In-memory Cytation stub for dry-run / non-Windows development.

    Mirrors the public surface of :class:`CytationReader` so the
    service code path is identical regardless of which one is wired in.

    Phase 3 read methods return deterministic synthetic values (well-id
    hash modulated) so workflow code can be exercised end-to-end in
    ``dry_run`` without ever opening the USB endpoint.
    """

    def __init__(self) -> None:
        self._connected = False
        self._setpoint_c: float | None = None
        self._actual_c = 22.0  # ambient until a setpoint is applied
        self._drawer = "in"
        self._shaking = False
        self.imaging_enabled = bool(_config.get("imaging", "enabled", True))
        self._plate: str | None = None
        self._plate_model: str | None = None

    async def setup(self) -> None:
        self._connected = True
        # Sits at ambient until a setpoint is applied, matching a real
        # incubator that is not heating on connect.
        self._actual_c = 22.0

    async def stop(self) -> None:
        self._connected = False
        self._shaking = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Introspection — the stub claims a plausible imaging fit-out so the
    # dry-run service exercises the same allowed_actions branches as the
    # real one.
    # ------------------------------------------------------------------

    def camera_ready(self) -> bool:
        return self._connected and self.imaging_enabled

    def camera_error(self) -> str | None:
        return None

    def installed_objectives(self) -> list[str]:
        return ["O_4X_PL_FL", "O_20X_PL_FL"]

    def installed_filters(self) -> list[str]:
        return ["DAPI", "GFP", "RFP"]

    def optics_inventory(self) -> dict[str, Any]:
        return {
            "objectives": self.installed_objectives(),
            "filters": self.installed_filters(),
            "objective_slots": 6,
            "filter_slots": 4,
        }

    def firmware_version(self) -> str | None:
        return "3.10-stub"

    def serial_number(self) -> str | None:
        return "STUB0000"

    def supports_phase_contrast(self) -> bool | None:
        return True

    # ---- incubator ---------------------------------------------------

    def temperature_range(self) -> tuple[float | None, float | None]:
        # Same range the real reader reports, so dry-run refuses exactly what
        # production refuses — the whole point of the stub.
        return (TEMPERATURE_MIN_C, TEMPERATURE_MAX_C)

    async def set_temperature(self, celsius: float) -> None:
        self._require_connected()
        lo, hi = self.temperature_range()
        if not (lo <= celsius <= hi):
            raise ValueError(
                f"Requested temperature {celsius}°C is outside {lo}-{hi}°C"
            )
        self._setpoint_c = celsius
        # The stub reaches setpoint instantly; a real incubator ramps.
        self._actual_c = celsius

    async def stop_temperature_control(self) -> None:
        self._require_connected()
        self._setpoint_c = None
        self._actual_c = 22.0

    # ---- shaker ------------------------------------------------------

    def is_shaking(self) -> bool:
        return self._shaking

    async def shake(self, *, pattern: str = "orbital", displacement_mm: int = 3) -> None:
        self._require_connected()
        if pattern.strip().lower() not in {"linear", "orbital"}:
            raise ValueError(
                f"Unknown shake pattern {pattern!r}. Known: linear, orbital"
            )
        if not 1 <= displacement_mm <= 6:
            raise ValueError("displacement_mm must be between 1 and 6")
        self._shaking = True

    async def stop_shaking(self) -> None:
        self._require_connected()
        self._shaking = False

    def has_plate(self) -> bool:
        return self._plate is not None

    def load_plate(self, *, plate_id: str, model: str | None = None) -> str:
        self._require_connected()
        from .plates import PLATE_FACTORIES

        model = model or str(_config.get("plates", "default_model", "custom_96"))
        if model not in PLATE_FACTORIES:
            raise ValueError(
                f"Unknown plate model {model!r}. Known: {sorted(PLATE_FACTORIES)}"
            )
        self._plate = plate_id
        self._plate_model = model
        return model

    def unload_plate(self) -> None:
        self._plate = None
        self._plate_model = None

    def _require_plate(self) -> None:
        if self._plate is None:
            raise PlateNotLoaded()

    @staticmethod
    def _check_wells(names: list[str]) -> None:
        """Reject wells that are not on a 96-well plate.

        The real reader gets this for free by resolving names against the
        ``Plate`` resource. Repeating it here (with plain string logic, so the
        stub keeps working on a box without pylabrobot) is what stops dry-run
        from accepting a request production would refuse — the whole point of
        the stub is that the service path behaves the same either way.
        """

        for name in names:
            row, col = name[:1].upper(), name[1:]
            if row not in "ABCDEFGH" or not col.isdigit() or not 1 <= int(col) <= 12:
                raise ValueError(f"Well {name!r} is not on this plate")

    @staticmethod
    def _check_channel(channel: str) -> None:
        key = channel.strip().lower().replace("-", "_").replace(" ", "_")
        if key not in _CHANNEL_ALIASES:
            raise ValueError(
                f"Unknown imaging channel {channel!r}. "
                f"Known: {sorted(_CHANNEL_ALIASES)}"
            )

    async def get_temperature(self) -> float | None:
        if not self._connected:
            return None
        return float(self._actual_c)

    async def open_drawer(self) -> None:
        if not self._connected:
            raise RuntimeError("stub reader is not connected")
        self._drawer = "out"

    async def close_drawer(self) -> None:
        if not self._connected:
            raise RuntimeError("stub reader is not connected")
        self._drawer = "in"

    # ------------------------------------------------------------------
    # Phase 3 read methods -- deterministic synthetic outputs
    # ------------------------------------------------------------------

    async def read_absorbance(
        self,
        *,
        wells: list[str],
        wavelength_nm: float,
    ) -> dict[str, float]:
        self._require_connected()
        self._require_plate()
        self._check_wells(wells)
        return {w: _synth_optical(w, wavelength_nm, scale=2.0) for w in wells}

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        self._require_connected()
        self._require_plate()
        self._check_wells(wells)
        # focal_height nudges the deterministic value enough that the tests
        # can distinguish "same wells, different args".
        bias = focal_height_mm / 7.0
        return {
            w: _synth_optical(w, excitation_nm + emission_nm, scale=10000.0) * bias
            for w in wells
        }

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        focal_height_mm: float = 7.0,
        integration_time_s: float = 1.0,
    ) -> dict[str, float]:
        self._require_connected()
        self._require_plate()
        self._check_wells(wells)
        bias = integration_time_s * (focal_height_mm / 7.0)
        return {w: _synth_optical(w, 0.0, scale=5000.0) * bias for w in wells}

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
        **_ignored: Any,
    ) -> dict[str, Any]:
        self._require_connected()
        self._require_plate()
        self._check_wells([well])
        self._check_channel(channel)
        tuning: dict[str, Any] = {}
        if auto_exposure:
            # Deterministic stand-in for the search, so a caller can see that
            # the resolved value differs from what it asked for.
            exposure_ms = 12.5
            tuning["auto_exposure"] = {
                "exposure_ms": exposure_ms,
                "target_peak_fraction": 0.8,
            }
        if autofocus:
            focal_height_mm = 8.25
            tuning["autofocus"] = {
                "focal_height_mm": focal_height_mm,
                "sharpness": 1.0,
            }
        return {
            "well": well,
            "channel": channel,
            "objective": objective or "O_4X_PL_FL",
            "focal_height_mm": focal_height_mm,
            "exposure_ms": exposure_ms,
            "gain": gain,
            "synthetic": True,
            **({"tuning": tuning} if tuning else {}),
            # Stub never writes a real image; the orchestrator gets a
            # data: URI placeholder instead so end-to-end tests can run
            # without touching the filesystem.
            "image_data_uri": "data:image/png;base64,iVBORw0KGgo=",
        }

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("stub reader is not connected")


def _synth_optical(well: str, wavelength_nm: float, *, scale: float) -> float:
    """Deterministic synthetic optical reading.

    Mixes the well-id and wavelength so the same (well, wavelength)
    pair always returns the same value but different wells or
    wavelengths produce different values. Range: roughly 0..scale.
    """
    h = abs(hash((well, round(wavelength_nm, 3))))
    return round((h % 10_000) / 10_000.0 * scale, 4)


def make_reader(*, dry_run: bool, usb_serial: str | None = None) -> Any:
    """Factory used by the service. Honors ``dry_run`` and the
    ``[instrument].backend`` config switch (``"dry_run"`` forces stub
    even when called with ``dry_run=False``).
    """

    backend_id = str(_config.get("instrument", "backend", "cytation5")).lower()
    if dry_run or backend_id == "dry_run":
        return StubCytationReader()
    return CytationReader(usb_serial=usb_serial or None)


__all__ = ["CytationReader", "StubCytationReader", "make_reader"]
