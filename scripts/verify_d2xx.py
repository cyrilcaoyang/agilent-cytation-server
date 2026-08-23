r"""Bench-verify the D2XX transport shim, without touching config or the service.

Run this in **Gen5/FTDI mode** (RUNBOOK §5) with the `cytation` service stopped.
It exercises the shim directly — enumerate, open, configure, read, close — so a
failure is attributable to the transport rather than to the whole service stack.

    C:\SDL_Tools\nssm.exe stop cytation
    cd C:\Users\sdl2\Projects\agilent-cytation-server
    .venv\Scripts\python.exe scripts\verify_d2xx.py

Every step prints PASS/FAIL and the script exits non-zero on the first failure.
Nothing here moves hardware: no plate motion, no optics, no temperature.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agilent_cytation_server.ftd2xx_shim import (  # noqa: E402
    D2xxDevice,
    D2xxUnavailable,
    list_devices,
    resolve_device_serial,
)

EXPECTED_SERIAL = "23030927"   # this unit; see docs/GEN5_ABSORBANCE.md §4

_failed = False


def check(label: str, fn) -> object:
    global _failed
    try:
        value = fn()
    except Exception as exc:
        print(f"FAIL  {label}\n        {type(exc).__name__}: {exc}")
        _failed = True
        raise SystemExit(1)
    print(f"PASS  {label}" + (f" -> {value!r}" if value is not None else ""))
    return value


def main() -> int:
    print("=== D2XX transport verification ===\n")

    check("ftd2xx imports", lambda: __import__("ftd2xx") and None)

    devices = check("D2XX enumerates FTDI devices", list_devices)
    if not devices:
        print(
            "\nFAIL  no devices visible.\n"
            "      This is the expected result while the chip is bound to\n"
            "      libusbK/WinUSB — D2XX can only see it through FTDI's vendor\n"
            "      driver. Swap per RUNBOOK §5 first, then re-run."
        )
        return 1
    for d in devices:
        print(f"        [{d['index']}] serial={d['serial']!r} desc={d['description']!r}")

    serial = check(
        f"resolve_device_serial({EXPECTED_SERIAL!r})",
        lambda: resolve_device_serial(EXPECTED_SERIAL),
    )
    if serial != EXPECTED_SERIAL:
        print(f"FAIL  resolved {serial!r}, expected {EXPECTED_SERIAL!r}")
        return 1

    dev = check("open device", lambda: D2xxDevice(device_id=EXPECTED_SERIAL) and None)
    dev = D2xxDevice(device_id=EXPECTED_SERIAL)
    try:
        # The exact configuration sequence the BioTek backend issues at setup.
        check("set baudrate 38400", lambda: setattr(dev, "baudrate", 38400))
        check("set_line_property(8, 0, 0)",
              lambda: _expect_ok(dev.ftdi_fn.ftdi_set_line_property(8, 0, 0)))
        check("set_flowctrl(0)", lambda: _expect_ok(dev.ftdi_fn.ftdi_setflowctrl(0)))
        check("set_latency_timer(16)",
              lambda: _expect_ok(dev.ftdi_fn.ftdi_set_latency_timer(16)))
        check("purge rx", lambda: _expect_ok(dev.ftdi_fn.ftdi_usb_purge_rx_buffer()))
        check("purge tx", lambda: _expect_ok(dev.ftdi_fn.ftdi_usb_purge_tx_buffer()))
        check("set_rts(True)", lambda: _expect_ok(dev.ftdi_fn.ftdi_setrts(True)))
        # A non-blocking read on an idle link should return b"" rather than hang.
        check("read is non-blocking on an idle link", lambda: dev.read(64))
    finally:
        dev.close()
        print("PASS  close")

    print(
        "\nAll transport checks passed.\n\n"
        "Next, end to end:\n"
        '  1. set [instrument].ftdi_transport = "d2xx" in config.toml\n'
        "  2. nssm start cytation   (elevated)\n"
        "  3. curl http://127.0.0.1:8040/status  -> expect equipment_status: ready\n"
        "  4. drive one read and one imaging.capture; compare against\n"
        "     captures/20260823T1804_fullplate_sweep_gen5/\n"
        "  5. with the service RUNNING, confirm Gen5 still connects — that is\n"
        "     the whole point, and the one thing no unit test can establish.\n"
    )
    return 0


def _expect_ok(rc: int) -> int:
    if rc != 0:
        raise RuntimeError(f"libftdi-convention error code {rc}")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except D2xxUnavailable as exc:
        print(f"FAIL  {exc}")
        raise SystemExit(1) from exc
