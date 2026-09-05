#!/usr/bin/env python3
"""Drive the objective turret by raw slot code and see what the camera gets.

Why this exists. On 2026-09-04 the focus axis was proven to move (accepted
``F`` commands, latency scaling with distance) while the "4X" image did not
change at all across 9 mm of travel — and that image spans ~13.9 mm, four
times PyLabRobot's own 3.474 mm figure for a 4X field. Both fit a light path
with **no objective in it**. PyLabRobot reads the slot inventory with
``i h{spot+1}`` ("+1 for some reason, eg first is h2") but selects with
``P0e{index+1}``, so the first physical slot may be code 2, not code 1 —
which would make every "4X" capture in this repo an empty-slot capture.

This talks to the backend directly, so the `cytation` service must be
stopped first (D2XX is exclusive):

    nssm stop cytation                      # elevated
    .venv\\Scripts\\python.exe scripts\\probe_objective_slots.py
    nssm start cytation; sc continue cytation

For each slot code 1-6 it: selects the slot, re-selects H12 with that code
in the Z command, probes a spread of raw focus counts (recording accept /
reject and latency), grabs one 8 ms brightfield frame, and writes the frame
plus a JSON summary under ``captures/<stamp>_slot_probe/``. Nothing here
depends on a focus being right; the questions are "which codes hold glass"
and "which focus window each one accepts".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agilent_cytation_server import config as _config  # noqa: E402
from agilent_cytation_server.reader import CytationReader  # noqa: E402

WELL_ROW, WELL_COL = 8, 12  # H12
EXPOSURE_MS = 8.0
# Counts spanning well below, inside, and well above PyLabRobot's 4.5-13.88 mm
# encoding (47876-147670). The firmware range-checks (`570F`), so out-of-range
# values are refused, not executed.
FOCUS_COUNTS = [10000, 30000, 47876, 74473, 100007, 147670, 200000, 300000, 500000, 800000]


def lapvar(a: np.ndarray) -> float:
    from numpy.lib.stride_tricks import sliding_window_view

    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], float)
    return float((sliding_window_view(a.astype(float), (3, 3)) * k).sum(axis=(-1, -2)).var())


async def cmd(backend, letter: str, param: str | None = None, timeout=None):
    t0 = time.perf_counter()
    resp = await backend.send_command(letter, param, timeout=timeout)
    ms = (time.perf_counter() - t0) * 1000
    text = bytes(resp or b"").replace(b"\x06", b"").replace(b"\x03", b"").decode("latin")
    return text, round(ms, 1)


async def main() -> int:
    from PIL import Image
    from pylabrobot.plate_reading.standard import ImagingMode, Objective

    stamp = datetime.now().strftime("%Y%m%dT%H%M")
    out = REPO / "captures" / f"{stamp}_slot_probe"
    out.mkdir(parents=True, exist_ok=True)

    reader = CytationReader(usb_serial=str(_config.get("instrument", "usb_serial", "")) or None)
    print("connecting ...")
    await reader.setup()
    backend = reader._backend  # noqa: SLF001
    if not reader._camera_ready:  # noqa: SLF001
        print("camera not ready:", reader._camera_error)  # noqa: SLF001
        await reader.stop()
        return 1
    reader.load_plate(plate_id="slot_probe", model="square_96_19mm")
    await backend.set_plate(reader._plate)  # noqa: SLF001

    summary: dict = {"inventory": {}, "slots": []}
    for q in ("h2", "h3", "h4", "h5", "h6", "h7", "o1", "o2"):
        try:
            summary["inventory"][q] = await cmd(backend, "i", q)
        except Exception as exc:  # the o* queries are the version-1 form; may NAK
            summary["inventory"][q] = f"ERR {exc}"
    print("inventory:", json.dumps(summary["inventory"], indent=1))

    # PLR's helpers look at _objective; point them at *something* so they run.
    backend._objective = Objective.O_4X_PL_FL_Phase  # noqa: SLF001

    try:
        for code in range(1, 7):
            rec: dict = {"code": code}
            rec["select"] = await cmd(backend, "Y", f"P0e{code:02}", timeout=60)
            print(f"\n== slot code {code}: P0e{code:02} -> {rec['select']}")

            backend._imaging_mode = None  # noqa: SLF001 - force the mode commands
            await backend.set_imaging_mode(ImagingMode.BRIGHTFIELD, led_intensity=10)

            rec["well"] = await cmd(backend, "Y", f"W6{WELL_ROW:02}{WELL_COL:02}", timeout=60)
            rec["position"] = await cmd(
                backend, "Y", f"Z{code}56{WELL_ROW:02}{WELL_COL:02}000000000000", timeout=60
            )
            backend._row, backend._column = WELL_ROW, WELL_COL  # noqa: SLF001
            backend._pos_x, backend._pos_y = 0, 0  # noqa: SLF001

            rec["focus"] = []
            for counts in FOCUS_COUNTS:
                text, ms = await cmd(backend, "i", f"F5{counts:07d}")
                rec["focus"].append({"counts": counts, "reply": text, "ms": ms})
                print(f"   F5{counts:07d} -> {text:<6s} {ms:7.1f} ms")

            await backend.set_exposure(EXPOSURE_MS)
            backend.start_acquisition()
            try:
                img = await backend._acquire_image()  # noqa: SLF001
            finally:
                backend.stop_acquisition()
            arr = np.asarray(img)
            path = out / f"slot{code}_H12_bf_e{EXPOSURE_MS:g}.png"
            Image.fromarray(arr).save(path)
            rec["frame"] = {
                "file": path.name,
                "mean": round(float(arr.mean()), 2),
                "std": round(float(arr.std()), 2),
                "min": int(arr.min()),
                "max": int(arr.max()),
                "lap_var": round(lapvar(arr), 2),
            }
            print("   frame:", rec["frame"])
            await backend.led_off()
            summary["slots"].append(rec)
    finally:
        try:
            await backend.led_off()
        except Exception:
            pass
        # Leave the turret where the service expects it after a restart.
        try:
            await cmd(backend, "Y", "P0e01", timeout=60)
        except Exception:
            pass
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        await reader.stop()
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
