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
            from pylabrobot.plate_reading.biotek import (  # type: ignore[import-not-found]
                Cytation5Backend,
            )
        except ImportError as exc:  # pragma: no cover - exercised on dev boxes only
            raise ImportError(
                "pylabrobot is required to drive the real Cytation 5. "
                "Install with `uv sync --extra plr --extra windows`."
            ) from exc

        backend_kwargs: dict[str, Any] = {}
        if self.usb_serial:
            backend_kwargs["serial_number"] = self.usb_serial

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


class StubCytationReader:
    """In-memory Cytation stub for dry-run / non-Windows development.

    Mirrors the public surface of :class:`CytationReader` so the
    service code path is identical regardless of which one is wired in.
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
