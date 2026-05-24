"""Capture a single brightfield image of well A1 on an agilent_shallow_96 plate.

This script bypasses the high-level ``Cytation5Backend.capture()`` because
PyLabRobot 0.2.1 hardcodes an ``image_size`` lookup that only knows
magnifications 4x, 20x, and 40x. With a 1.25x PL APO objective, that
table raises ValueError before capture even runs. We call the underlying
primitives directly instead.

Prerequisites:
  1. Zadig swap done (FTDI -> libusbK). Verify with scripts/verify_reader.py first.
  2. Spinnaker SDK + PySpin installed in the venv.
  3. Plate physically loaded inside the Cytation; A1 in the back-left corner.
  4. Objectives configured in Gen5.exe (Instrument Configuration -> Imager).

Run from the repo root:

    C:\\SDL_Tools\\uv.exe run --project . python scripts\\capture_a1.py
    C:\\SDL_Tools\\uv.exe run --project . python scripts\\capture_a1.py --focus 8.5 --exposure 8
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage

from agilent_cytation_server.plates import agilent_shallow_96
from agilent_cytation_server.reader import _patch_pylabrobot_ftdi_enumeration

# Apply the same enumeration patch the live service uses, so a co-resident
# FTDIBUS-bound FTDI device (e.g. the COM4 USB-serial port) does not abort
# pylabrobot's FTDI device enumeration before the Cytation is matched.
_patch_pylabrobot_ftdi_enumeration()

from pylabrobot.plate_reading.agilent.biotek_cytation_backend import (  # noqa: E402
    Cytation5Backend,
)
from pylabrobot.plate_reading.standard import ImagingMode, Objective  # noqa: E402

from agilent_cytation_server import config as _config  # noqa: E402


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "captures"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("capture_a1")


def pick_objective(installed: list[Objective | None], want_low_mag: bool = True) -> Objective:
    candidates = [o for o in installed if o is not None]
    if not candidates:
        raise RuntimeError(
            "No objectives reported by the Cytation. Register them in Gen5.exe "
            "(Instrument Configuration -> Imager) and re-run."
        )
    log.info("Installed objectives: %s", [(o.name, o.magnification) for o in candidates])
    chosen = min(candidates, key=lambda o: o.magnification) if want_low_mag else candidates[0]
    log.info("Picked: %s (%.2fX)", chosen.name, chosen.magnification)
    return chosen


async def run(focal_height_mm: float, exposure_ms: float, gain: float) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    instrument_cfg = _config.get_section("instrument")
    usb_serial = instrument_cfg.get("usb_serial") if isinstance(instrument_cfg, dict) else None
    backend_kwargs = {"device_id": usb_serial} if usb_serial else {}
    log.info("Constructing Cytation5Backend(%s) ...", backend_kwargs)
    backend = Cytation5Backend(**backend_kwargs)

    log.info("Connecting (use_cam=True) ...")
    await backend.setup(use_cam=True)

    try:
        log.info("Firmware version: %s", backend.version)
        objective = pick_objective(backend.objectives)

        plate = agilent_shallow_96(name="a1_test_plate")

        log.info("Setting plate, objective, mode=BRIGHTFIELD ...")
        await backend.set_plate(plate)
        await backend.set_objective(objective)
        await backend.set_imaging_mode(ImagingMode.BRIGHTFIELD, led_intensity=10)

        log.info("Selecting well A1 (row=1, col=1) ...")
        await backend.select(row=1, column=1)

        log.info(
            "focus=%.3f mm  exposure=%.2f ms  gain=%.1f",
            focal_height_mm, exposure_ms, gain,
        )
        await backend.set_exposure(exposure_ms)
        await backend.set_gain(gain)
        await backend.set_focus(focal_height_mm)
        await backend.set_position(0.0, 0.0)

        log.info("Acquiring image ...")
        backend.start_acquisition()
        try:
            img = await backend._acquire_image()  # noqa: SLF001 - primitive
        finally:
            backend.stop_acquisition()
            await backend.led_off()

        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out = OUTPUT_DIR / f"A1_brightfield_{ts}_f{focal_height_mm:.2f}_e{exposure_ms:.1f}.png"
        PILImage.fromarray(img).save(out)
        log.info("Saved %s  (%sx%s)", out, img.shape[1], img.shape[0])
        log.info(
            "Pixel stats: min=%s mean=%.1f max=%s  (8-bit mono; tune --focus / --exposure based on this)",
            int(img.min()), float(img.mean()), int(img.max()),
        )
    finally:
        log.info("Disconnecting ...")
        await backend.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--focus", type=float, default=10.0,
                        help="Focal height in mm (4.5-13.88). Default 10.0.")
    parser.add_argument("--exposure", type=float, default=5.0,
                        help="Exposure time in ms. Default 5.0.")
    parser.add_argument("--gain", type=float, default=0.0,
                        help="Camera gain in dB. Default 0.0.")
    args = parser.parse_args()
    asyncio.run(run(args.focus, args.exposure, args.gain))


if __name__ == "__main__":
    main()
