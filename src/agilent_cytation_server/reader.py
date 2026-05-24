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
"""

from __future__ import annotations

import logging
from typing import Any

from . import config as _config

logger = logging.getLogger(__name__)


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

    def __init__(self, *, usb_serial: str | None = None) -> None:
        self.usb_serial = usb_serial or None
        self._reader: Any | None = None
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
                Cytation5Backend,
            )
        except ImportError as exc:  # pragma: no cover - exercised on dev boxes only
            raise ImportError(
                "pylabrobot is required to drive the real Cytation 5. "
                "Install with `uv sync --extra plr --extra windows`."
            ) from exc

        _patch_pylabrobot_ftdi_enumeration()

        backend_kwargs: dict[str, Any] = {}
        if self.usb_serial:
            backend_kwargs["device_id"] = self.usb_serial

        backend = Cytation5Backend(**backend_kwargs)
        # The PlateReader frontend wraps the backend in PLR's resource
        # tree. Phase 1 does not put a Plate inside it; sample tracking
        # ships in Phase 2.
        self._reader = PlateReader(
            name="cytation_5",
            size_x=500,
            size_y=400,
            size_z=300,
            backend=backend,
        )
        await self._reader.setup()
        self._connected = True
        logger.info("Cytation 5 backend connected (usb_serial=%r)", self.usb_serial)

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

    async def get_temperature(self) -> float | None:
        """Return the incubator temperature in degrees Celsius, or
        ``None`` if the backend cannot answer.

        Wrapped to never raise: the spec requires ``GET /status`` to
        always return HTTP 200, so per-getter failures must fold into
        ``degraded`` rather than crash.
        """
        if self._reader is None:
            return None
        getter = getattr(self._reader, "get_temperature", None)
        if getter is None:
            return None
        try:
            value = await getter()
        except Exception:  # pragma: no cover - hardware-specific
            logger.debug("Cytation get_temperature failed", exc_info=True)
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

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
    # Phase 3 read methods -- thin delegation to PyLabRobot.
    #
    # The exact kwarg names on PyLabRobot's PlateReader / Cytation5Backend
    # have drifted across versions; we delegate via getattr so a name
    # change surfaces as a clear RuntimeError instead of an AttributeError
    # deep in the call stack. Hardware verification of these against the
    # real Cytation is required before flipping protocol to "1.1" in the
    # dashboard's equipment.yaml -- see RUNBOOK.md §3-§4.
    # ------------------------------------------------------------------

    async def read_absorbance(
        self,
        *,
        wells: list[str],
        wavelength_nm: float,
    ) -> dict[str, float]:
        return await self._delegate_read(
            "read_absorbance",
            wells=wells,
            wavelength=wavelength_nm,
        )

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        gain: float = 50.0,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        return await self._delegate_read(
            "read_fluorescence",
            wells=wells,
            excitation_wavelength=excitation_nm,
            emission_wavelength=emission_nm,
            gain=gain,
            focal_height=focal_height_mm,
        )

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        integration_time_s: float = 1.0,
        gain: float = 50.0,
    ) -> dict[str, float]:
        return await self._delegate_read(
            "read_luminescence",
            wells=wells,
            integration_time=integration_time_s,
            gain=gain,
        )

    async def capture_image(
        self,
        *,
        well: str,
        channel: str,
        focal_height_mm: float = 5.0,
        exposure_ms: float = 10.0,
        gain: float = 1.0,
    ) -> dict[str, Any]:
        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")
        capture_fn = getattr(self._reader, "capture_image", None)
        if capture_fn is None:
            raise RuntimeError(
                "PyLabRobot PlateReader does not expose capture_image; the "
                "imaging surface graduates with a future PyLabRobot release."
            )
        return await capture_fn(
            well=well,
            channel=channel,
            focal_height=focal_height_mm,
            exposure_ms=exposure_ms,
            gain=gain,
        )

    async def _delegate_read(self, attr: str, **kwargs: Any) -> dict[str, float]:
        if self._reader is None:
            raise RuntimeError("Cytation reader is not connected")
        fn = getattr(self._reader, attr, None)
        if fn is None:
            raise RuntimeError(
                f"PyLabRobot PlateReader does not expose {attr}; verify the "
                "installed pylabrobot version matches what the cytation "
                "server expects."
            )
        result = await fn(**kwargs)
        # PyLabRobot returns either a dict[well, value] or a list keyed
        # by well index; normalise to dict[well, value] using the
        # well-list the caller supplied.
        if isinstance(result, dict):
            return {str(k): float(v) for k, v in result.items()}
        wells = kwargs.get("wells", [])
        return {str(w): float(v) for w, v in zip(wells, result)}


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
        self._setpoint_c = 37.0
        self._actual_c = 22.0  # ambient until "warmed up"
        self._drawer = "in"

    async def setup(self) -> None:
        self._connected = True
        self._actual_c = self._setpoint_c

    async def stop(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

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
        return {w: _synth_optical(w, wavelength_nm, scale=2.0) for w in wells}

    async def read_fluorescence(
        self,
        *,
        wells: list[str],
        excitation_nm: float,
        emission_nm: float,
        gain: float = 50.0,
        focal_height_mm: float = 7.0,
    ) -> dict[str, float]:
        self._require_connected()
        # gain / focal_height nudge the deterministic value enough that the
        # tests can distinguish "same wells, different args".
        bias = (gain / 50.0) * (focal_height_mm / 7.0)
        return {
            w: _synth_optical(w, excitation_nm + emission_nm, scale=10000.0) * bias
            for w in wells
        }

    async def read_luminescence(
        self,
        *,
        wells: list[str],
        integration_time_s: float = 1.0,
        gain: float = 50.0,
    ) -> dict[str, float]:
        self._require_connected()
        bias = integration_time_s * (gain / 50.0)
        return {w: _synth_optical(w, 0.0, scale=5000.0) * bias for w in wells}

    async def capture_image(
        self,
        *,
        well: str,
        channel: str,
        focal_height_mm: float = 5.0,
        exposure_ms: float = 10.0,
        gain: float = 1.0,
    ) -> dict[str, Any]:
        self._require_connected()
        return {
            "well": well,
            "channel": channel,
            "focal_height_mm": focal_height_mm,
            "exposure_ms": exposure_ms,
            "gain": gain,
            "synthetic": True,
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
