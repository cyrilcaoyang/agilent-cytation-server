"""Verify the Zadig libusbK swap worked — reader subsystem only, no camera.

Run from the repo root:

    C:\\SDL_Tools\\uv.exe run --project . python scripts\\verify_reader.py

Success criteria:
  - PyLabRobot's FTDI/libusbK transport opens the Cytation 5.
  - We read firmware version, temperature, and we can open + close the drawer.
  - No PySpin / Spinnaker SDK required (use_cam=False).

If this fails with "no backend found" or "device not found", the Zadig
swap probably didn't take — check Device Manager: the Cytation's FTDI
endpoint should now appear under "libusbK USB Devices" instead of
"USB Serial Bus Devices".
"""

from __future__ import annotations

import asyncio
import logging

from pylabrobot.plate_reading.agilent.biotek_cytation_backend import Cytation5Backend


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    log = logging.getLogger("verify_reader")

    backend = Cytation5Backend()
    log.info("Connecting to Cytation 5 reader (no camera) ...")
    await backend.setup(use_cam=False)
    try:
        log.info("Firmware version: %s", backend.version)

        try:
            temp = await backend.get_current_temperature()
            log.info("Current temperature: %s C", temp)
        except Exception as exc:
            log.warning("get_current_temperature failed (non-fatal): %s", exc)

        log.info("Opening drawer ...")
        await backend.open()
        await asyncio.sleep(2)
        log.info("Closing drawer ...")
        await backend.close(plate=None)
        log.info("OK — Zadig swap appears to be working.")
    finally:
        log.info("Disconnecting ...")
        await backend.stop()


if __name__ == "__main__":
    asyncio.run(main())
