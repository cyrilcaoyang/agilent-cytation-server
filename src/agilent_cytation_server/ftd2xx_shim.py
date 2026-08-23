r"""Drive the Cytation's FTDI link through FTDI's D2XX driver instead of libusb.

Why this exists
---------------
PyLabRobot reaches the reader with ``pylabrobot.io.ftdi.FTDI``, which wraps
``pylibftdi.Device`` — and pylibftdi goes through **libusb**. On Windows libusb
can only see the FTDI chip when libusbK/WinUSB is bound to it, which means the
chip cannot simultaneously be visible to **Gen5**, which needs FTDI's own
vendor driver. That is the entire reason for the driver swap in ``RUNBOOK.md``
§4/§5, and it is not symmetric: going back to libusbK cannot be scripted,
because ``ftdibus.inf`` outranks the self-signed libwdi package and no CLI can
override driver ranking. Zadig exists to do exactly that, by hand, at the
machine.

**D2XX talks through the vendor driver rather than around it.** With this
transport installed the reader stays on FTDI permanently, Gen5 and this service
coexist at the driver level, and switching between them is `nssm stop cytation`
— no Zadig, no `pnputil`, no GUI, and therefore remotely operable.

What it replaces
----------------
``FTDI`` touches ``pylibftdi.Device`` through a small, fixed surface, so this
module substitutes that one object rather than reimplementing the transport:

===========================  ==================================
pylibftdi                    D2XX
===========================  ==================================
``open`` / ``close``         ``FT_OpenEx`` / ``FT_Close``
``read`` / ``write``         ``FT_Read`` / ``FT_Write``
``baudrate =``               ``FT_SetBaudRate``
``ftdi_set_line_property``   ``FT_SetDataCharacteristics``
``ftdi_setflowctrl``         ``FT_SetFlowControl``
``ftdi_setrts`` / ``setdtr`` ``FT_SetRts``/``ClrRts``, ``SetDtr``/``ClrDtr``
``ftdi_usb_reset``           ``FT_ResetDevice``
``ftdi_set_latency_timer``   ``FT_SetLatencyTimer``
``ftdi_usb_purge_*_buffer``  ``FT_Purge``
``ftdi_poll_modem_status``   ``FT_GetModemStatus``
===========================  ==================================

It also replaces ``FTDI._resolve_device_serial``. That is half the payoff: the
stock implementation enumerates with pyusb and cannot open a vendor-driver-bound
device, failing with ``NotImplementedError: Operation not supported`` — the
error a swapped-to-Gen5 reader produces today. D2XX enumerates through
``FT_CreateDeviceInfoList`` precisely *because* the vendor driver is bound.

Status: **not yet verified against hardware.** D2XX cannot see the reader while
it is bound to libusbK/WinUSB, so this needs a bench run in FTDI mode. See
``docs/GEN5_ABSORBANCE.md``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# libftdi returns 0 on success and a negative errno on failure; FTDI's callers
# check that convention, while ftd2xx raises. Every wrapper below translates.
_OK = 0
_ERR = -1

# libftdi's stop-bit enum has a 1.5 value that D2XX does not implement.
_STOP_BITS = {0: 0, 2: 2}  # libftdi STOP_BIT_1 / STOP_BIT_2 -> D2XX


class D2xxUnavailable(RuntimeError):
    """The ftd2xx package or FTDI's D2XX DLL could not be loaded."""


def _ftd2xx() -> Any:
    try:
        import ftd2xx  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on host install
        raise D2xxUnavailable(
            "ftd2xx is required for the D2XX transport. Install with "
            "`uv pip install ftd2xx`; the DLL itself ships with FTDI's CDM driver."
        ) from exc
    return ftd2xx


def list_devices() -> list[dict[str, Any]]:
    """Every FTDI device D2XX can see, as ``{index, serial, description}``.

    Returns an empty list when the chip is bound to libusbK/WinUSB — that is
    not an error, it is what "the other driver owns it" looks like from here.
    """
    ftd2xx = _ftd2xx()
    out: list[dict[str, Any]] = []
    for i in range(ftd2xx.createDeviceInfoList()):
        try:
            d = ftd2xx.getDeviceInfoDetail(i)
        except Exception:  # pragma: no cover - hardware-specific
            logger.debug("D2XX device %d could not be described", i, exc_info=True)
            continue
        out.append(
            {
                "index": i,
                "serial": _text(d.get("serial") if isinstance(d, dict) else d.serial),
                "description": _text(
                    d.get("description") if isinstance(d, dict) else d.description
                ),
            }
        )
    return out


def _text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("ascii", "replace")
    return "" if v is None else str(v)


class _FtdiFn:
    """The ``Device.ftdi_fn`` namespace, backed by D2XX.

    pylibftdi exposes raw libftdi entry points through this proxy, and
    ``pylabrobot.io.ftdi.FTDI`` calls nine of them. Each returns libftdi's 0/-1
    rather than raising, because that is what the caller checks.
    """

    def __init__(self, dev: D2xxDevice) -> None:
        self._dev = dev

    def _call(self, what: str, fn: Any, *args: Any) -> int:
        try:
            fn(*args)
            return _OK
        except Exception:  # pragma: no cover - hardware-specific
            logger.exception("D2XX %s failed", what)
            return _ERR

    # ---- line / flow -------------------------------------------------
    def ftdi_set_line_property(self, bits: int, stopbits: int, parity: int) -> int:
        if stopbits not in _STOP_BITS:
            logger.error(
                "libftdi stopbits=%s (1.5 stop bits) has no D2XX equivalent", stopbits
            )
            return _ERR
        return self._call(
            "setDataCharacteristics",
            self._dev.handle.setDataCharacteristics,
            bits,
            _STOP_BITS[stopbits],
            parity,
        )

    def ftdi_setflowctrl(self, flowctrl: int) -> int:
        # libftdi's SIO_*_HS constants are 0x0/0x100/0x200/0x400, identical to
        # D2XX's FLOW_* values, so this passes through unchanged.
        return self._call("setFlowControl", self._dev.handle.setFlowControl, flowctrl)

    # ---- modem lines -------------------------------------------------
    def ftdi_setrts(self, level: int) -> int:
        h = self._dev.handle
        return self._call("setRts" if level else "clrRts", h.setRts if level else h.clrRts)

    def ftdi_setdtr(self, level: int) -> int:
        h = self._dev.handle
        return self._call("setDtr" if level else "clrDtr", h.setDtr if level else h.clrDtr)

    def ftdi_poll_modem_status(self, status_ptr: Any = None) -> int:
        try:
            status = self._dev.handle.getModemStatus()
        except Exception:  # pragma: no cover - hardware-specific
            logger.exception("D2XX getModemStatus failed")
            return _ERR
        # libftdi writes the status through a pointer; mirror that when given
        # one, and otherwise just report success.
        if status_ptr is not None and hasattr(status_ptr, "value"):
            status_ptr.value = status
        return _OK

    # ---- device / buffers --------------------------------------------
    def ftdi_usb_reset(self) -> int:
        return self._call("resetDevice", self._dev.handle.resetDevice)

    def ftdi_set_latency_timer(self, latency: int) -> int:
        return self._call("setLatencyTimer", self._dev.handle.setLatencyTimer, latency)

    def ftdi_usb_purge_rx_buffer(self) -> int:
        import ftd2xx.defines as D  # type: ignore[import-not-found]

        return self._call("purge(RX)", self._dev.handle.purge, D.PURGE_RX)

    def ftdi_usb_purge_tx_buffer(self) -> int:
        import ftd2xx.defines as D  # type: ignore[import-not-found]

        return self._call("purge(TX)", self._dev.handle.purge, D.PURGE_TX)


class D2xxDevice:
    """A ``pylibftdi.Device`` work-alike backed by D2XX.

    Only the surface ``pylabrobot.io.ftdi.FTDI`` actually uses is implemented;
    see the module docstring for the mapping. The constructor signature matches
    the ``Device(...)`` call in ``FTDI.setup()``.
    """

    #: Read timeout. FTDI's own reads block until satisfied, whereas libftdi
    #: returns what is buffered; the BioTek backend's `_read_until` runs its own
    #: timeout loop and relies on the latter, so reads here are non-blocking.
    READ_TIMEOUT_MS = 50
    WRITE_TIMEOUT_MS = 2000

    def __init__(
        self,
        *,
        lazy_open: bool = False,
        device_id: str | None = None,
        pid: int | None = None,
        vid: int | None = None,
        interface_select: int | None = None,
    ) -> None:
        self._device_id = device_id
        self._pid = pid
        self._vid = vid
        self._interface_select = interface_select
        self._handle: Any = None
        self._baudrate: int | None = None
        self.ftdi_fn = _FtdiFn(self)
        if not lazy_open:
            self.open()

    # ---- lifecycle ---------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._handle is None

    @property
    def handle(self) -> Any:
        if self._handle is None:
            raise RuntimeError("D2XX device is not open")
        return self._handle

    def open(self) -> None:
        ftd2xx = _ftd2xx()
        if self._device_id:
            serial = self._device_id.encode() if isinstance(self._device_id, str) else self._device_id
            self._handle = ftd2xx.openEx(serial)
        else:
            devices = list_devices()
            if not devices:
                raise D2xxUnavailable(
                    "D2XX sees no FTDI devices. If the reader is bound to "
                    "libusbK/WinUSB, D2XX cannot see it — that binding is what "
                    "this transport exists to make unnecessary (RUNBOOK §4)."
                )
            self._handle = ftd2xx.open(devices[0]["index"])
        self._handle.setTimeouts(self.READ_TIMEOUT_MS, self.WRITE_TIMEOUT_MS)
        logger.info("D2XX opened FTDI device %s", self._device_id or "(first available)")

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    # ---- io ----------------------------------------------------------
    def read(self, num_bytes: int = 1) -> bytes:
        """Return up to ``num_bytes`` *buffered* bytes, never blocking.

        D2XX's own read blocks until the count is satisfied or the timeout
        expires; asking only for what `FT_GetQueueStatus` reports keeps the
        libftdi semantics the caller's read loops were written against.
        """
        h = self.handle
        available = h.getQueueStatus()
        if not available:
            return b""
        return h.read(min(num_bytes, available))

    def write(self, data: bytes) -> int:
        written = self.handle.write(data)
        # FT_Write reports the count; older wrappers return None on success.
        return len(data) if written is None else int(written)

    # ---- baudrate ----------------------------------------------------
    @property
    def baudrate(self) -> int | None:
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        self.handle.setBaudRate(value)
        self._baudrate = value


def resolve_device_serial(device_id: str | None = None) -> str:
    """Pick which FTDI serial to open, D2XX-side.

    Replaces ``FTDI._resolve_device_serial``, whose pyusb walk cannot open a
    vendor-driver-bound device and aborts enumeration entirely.
    """
    devices = list_devices()
    if not devices:
        raise D2xxUnavailable(
            "D2XX sees no FTDI devices (is the chip still bound to libusbK/WinUSB?)"
        )
    if device_id:
        for d in devices:
            if d["serial"] == device_id:
                return str(d["serial"])
        raise D2xxUnavailable(
            f"No D2XX device with serial {device_id!r}. Visible: "
            + ", ".join(f"{d['serial']!r} ({d['description']!r})" for d in devices)
        )
    if len(devices) > 1:
        logger.warning(
            "D2XX sees %d FTDI devices and no device_id was pinned; using %r",
            len(devices),
            devices[0]["serial"],
        )
    return str(devices[0]["serial"])


def install() -> None:
    """Point ``pylabrobot.io.ftdi`` at D2XX instead of libusb.

    Monkeypatch rather than a fork, matching the existing
    ``_patch_pylabrobot_ftdi_enumeration`` in ``reader.py``. Idempotent.
    """
    from pylabrobot.io import ftdi as plr_ftdi  # type: ignore[import-not-found]

    if getattr(plr_ftdi, "_d2xx_installed", False):
        return

    plr_ftdi.Device = D2xxDevice  # type: ignore[attr-defined]
    # pylibftdi/pyusb presence is asserted in FTDI.__init__ before any device
    # work; with D2XX driving, neither is needed.
    plr_ftdi.HAS_PYLIBFTDI = True  # type: ignore[attr-defined]
    plr_ftdi.HAS_PYUSB = True  # type: ignore[attr-defined]

    def _resolve(self: Any) -> str:
        return resolve_device_serial(getattr(self, "_device_id", None))

    plr_ftdi.FTDI._resolve_device_serial = _resolve  # type: ignore[attr-defined]
    plr_ftdi._d2xx_installed = True  # type: ignore[attr-defined]
    logger.info("pylabrobot FTDI transport switched to D2XX")
