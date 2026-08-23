"""Unit tests for the D2XX transport shim.

These verify the *mapping* — that every pylibftdi entry point PyLabRobot calls
lands on the right D2XX call with the right arguments, and that libftdi's 0/-1
return convention is preserved. They cannot verify the shim against a real
reader: D2XX only sees the chip while FTDI's vendor driver is bound, and this
PC normally runs libusbK. Bench verification is tracked in
``docs/GEN5_ABSORBANCE.md``.
"""

from __future__ import annotations

import pytest

from agilent_cytation_server import ftd2xx_shim as shim


class FakeHandle:
    """Records D2XX calls instead of touching a driver."""

    def __init__(self, queued: bytes = b"") -> None:
        self.calls: list[tuple] = []
        self.queued = queued
        self.written = b""
        self.timeouts: tuple | None = None

    def setTimeouts(self, r, w):  # noqa: N802 - mirrors ftd2xx's naming
        self.timeouts = (r, w)

    def setDataCharacteristics(self, wordlen, stopbits, parity):  # noqa: N802
        self.calls.append(("setDataCharacteristics", wordlen, stopbits, parity))

    def setFlowControl(self, flow):  # noqa: N802
        self.calls.append(("setFlowControl", flow))

    def setRts(self):  # noqa: N802
        self.calls.append(("setRts",))

    def clrRts(self):  # noqa: N802
        self.calls.append(("clrRts",))

    def setDtr(self):  # noqa: N802
        self.calls.append(("setDtr",))

    def clrDtr(self):  # noqa: N802
        self.calls.append(("clrDtr",))

    def resetDevice(self):  # noqa: N802
        self.calls.append(("resetDevice",))

    def setLatencyTimer(self, latency):  # noqa: N802
        self.calls.append(("setLatencyTimer", latency))

    def purge(self, mask):
        self.calls.append(("purge", mask))

    def getModemStatus(self):  # noqa: N802
        return 0x6001

    def getQueueStatus(self):  # noqa: N802
        return len(self.queued)

    def read(self, n):
        out, self.queued = self.queued[:n], self.queued[n:]
        return out

    def write(self, data):
        self.written += data
        return len(data)

    def setBaudRate(self, baud):  # noqa: N802
        self.calls.append(("setBaudRate", baud))

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def dev() -> shim.D2xxDevice:
    d = shim.D2xxDevice(lazy_open=True, device_id="23030927")
    d._handle = FakeHandle()
    return d


# ---------------------------------------------------------------------------
# Line / flow mapping
# ---------------------------------------------------------------------------


def test_line_property_maps_to_data_characteristics(dev) -> None:
    assert dev.ftdi_fn.ftdi_set_line_property(8, 0, 0) == 0
    assert dev._handle.calls == [("setDataCharacteristics", 8, 0, 0)]


def test_one_and_a_half_stop_bits_is_refused_not_silently_wrong(dev) -> None:
    """libftdi's STOP_BIT_15 has no D2XX equivalent.

    Passing it through would land on D2XX's STOP_BITS_2 and mis-frame every
    byte, so the shim reports libftdi's error code instead.
    """
    assert dev.ftdi_fn.ftdi_set_line_property(8, 1, 0) == -1
    assert dev._handle.calls == []


def test_flowctrl_constants_pass_through_unchanged(dev) -> None:
    """libftdi SIO_RTS_CTS_HS (0x100) == D2XX FLOW_RTS_CTS, so no remap."""
    import ftd2xx.defines as D

    assert dev.ftdi_fn.ftdi_setflowctrl(0x100) == 0
    assert dev._handle.calls == [("setFlowControl", D.FLOW_RTS_CTS)]


# ---------------------------------------------------------------------------
# Modem lines: one libftdi call, two D2XX calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level, expected", [(1, "setRts"), (0, "clrRts"), (True, "setRts"), (False, "clrRts")]
)
def test_rts_level_selects_set_or_clear(dev, level, expected) -> None:
    assert dev.ftdi_fn.ftdi_setrts(level) == 0
    assert dev._handle.calls == [(expected,)]


@pytest.mark.parametrize("level, expected", [(1, "setDtr"), (0, "clrDtr")])
def test_dtr_level_selects_set_or_clear(dev, level, expected) -> None:
    assert dev.ftdi_fn.ftdi_setdtr(level) == 0
    assert dev._handle.calls == [(expected,)]


# ---------------------------------------------------------------------------
# Buffers / device
# ---------------------------------------------------------------------------


def test_purge_masks_are_distinct(dev) -> None:
    import ftd2xx.defines as D

    dev.ftdi_fn.ftdi_usb_purge_rx_buffer()
    dev.ftdi_fn.ftdi_usb_purge_tx_buffer()
    assert dev._handle.calls == [("purge", D.PURGE_RX), ("purge", D.PURGE_TX)]


def test_reset_and_latency(dev) -> None:
    assert dev.ftdi_fn.ftdi_usb_reset() == 0
    assert dev.ftdi_fn.ftdi_set_latency_timer(16) == 0
    assert dev._handle.calls == [("resetDevice",), ("setLatencyTimer", 16)]


def test_failures_return_libftdi_error_code_not_an_exception(dev) -> None:
    """FTDI checks return codes; a raising shim would escape as a crash."""

    def boom():
        raise OSError("device unplugged")

    dev._handle.resetDevice = boom
    assert dev.ftdi_fn.ftdi_usb_reset() == -1


# ---------------------------------------------------------------------------
# IO semantics
# ---------------------------------------------------------------------------


def test_read_returns_only_buffered_bytes(dev) -> None:
    """D2XX's read blocks until satisfied; libftdi's returns what is there.

    The BioTek backend runs its own `_read_until` timeout loop and depends on
    the non-blocking behaviour, so the shim asks for min(requested, queued).
    """
    dev._handle.queued = b"AB"
    assert dev.read(8) == b"AB"
    assert dev.read(8) == b""  # empty queue must not block or raise


def test_write_reports_byte_count(dev) -> None:
    assert dev.write(b"hello") == 5
    assert dev._handle.written == b"hello"


def test_write_counts_bytes_when_driver_returns_none(dev) -> None:
    dev._handle.write = lambda data: None
    assert dev.write(b"1234") == 4


def test_baudrate_round_trips(dev) -> None:
    dev.baudrate = 38400
    assert dev.baudrate == 38400
    assert dev._handle.calls == [("setBaudRate", 38400)]


def test_close_is_idempotent_and_flips_closed(dev) -> None:
    assert dev.closed is False
    dev.close()
    assert dev.closed is True
    dev.close()  # must not raise


def test_handle_access_after_close_is_a_clear_error(dev) -> None:
    dev.close()
    with pytest.raises(RuntimeError, match="not open"):
        _ = dev.handle


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def test_resolve_matches_on_serial(monkeypatch) -> None:
    monkeypatch.setattr(
        shim,
        "list_devices",
        lambda: [
            {"index": 0, "serial": "OTHER123", "description": "some cable"},
            {"index": 1, "serial": "23030927", "description": "Cytation5"},
        ],
    )
    assert shim.resolve_device_serial("23030927") == "23030927"


def test_resolve_lists_what_it_saw_when_the_serial_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        shim,
        "list_devices",
        lambda: [{"index": 0, "serial": "OTHER123", "description": "some cable"}],
    )
    with pytest.raises(shim.D2xxUnavailable) as exc:
        shim.resolve_device_serial("23030927")
    assert "OTHER123" in str(exc.value)  # the message must be actionable


def test_empty_enumeration_explains_the_libusbk_case(monkeypatch) -> None:
    """The likeliest cause of "no devices" is the wrong driver being bound."""
    monkeypatch.setattr(shim, "list_devices", lambda: [])
    with pytest.raises(shim.D2xxUnavailable, match="libusbK"):
        shim.resolve_device_serial()


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def test_install_patches_and_is_idempotent() -> None:
    plr_ftdi = pytest.importorskip("pylabrobot.io.ftdi")
    original_device = plr_ftdi.Device
    original_resolve = plr_ftdi.FTDI._resolve_device_serial
    try:
        shim.install()
        assert plr_ftdi.Device is shim.D2xxDevice
        assert plr_ftdi.FTDI._resolve_device_serial is not original_resolve
        shim.install()  # second call must be a no-op, not a re-patch
        assert plr_ftdi.Device is shim.D2xxDevice
    finally:
        plr_ftdi.Device = original_device
        plr_ftdi.FTDI._resolve_device_serial = original_resolve
        plr_ftdi._d2xx_installed = False
