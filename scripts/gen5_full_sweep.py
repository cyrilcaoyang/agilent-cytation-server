r"""Full 230-800 nm absorbance sweep through Gen5 in Sweep mode.

58 wavelengths in 10 nm steps, delivered as 10 EndPoint reads of <=6 wavelengths
(Gen5's per-read limit), each covering the whole 96-well plate (Gen5 refuses
runtime well selection through this API, so partial-plate is not available).

Read settings measured on 2026-08-23: ReadSpeed=Sweep, MeasurementsPerDataPoint=1,
DelayAfterPlateMovementMSec=0. Full-plate read time scales ~10.0 s per wavelength
plus ~11 s fixed (2 lambda -> 31.3 s, 4 -> 52.2 s, 6 -> 71.2 s), against 312.52 s
for six wavelengths at the Normal/8/100 default. Expect ~12 min for the sweep.

Values come back through GetRawData, which returns empty arrays unless
`Application.DataExportEnabled` is set first - that omission is why the
2026-08-23 baseline run produced timing and no data.

REQUIRES Gen5 mode (FTDI vendor driver bound) and the cytation service stopped.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r"C:\Users\sdl2\Projects\biotek_driver")

from biotek_driver.biotek import Biotek
from biotek_driver.xml_builders.procedure_builder import build_absorbance_procedure_xml
from biotek_driver.xml_builders.protocol_builder import build_bti_protocol_xml

WAVELENGTHS = list(range(230, 801, 10))     # 58 points
MAX_PER_READ = 6                            # Gen5 EndPoint limit
READ_SPEED, MPD, DELAY_MS = "Sweep", 1, 0
COM_PORT, READER_NAME = 4, "Cytation5"
INDEXES = "<Indexes><Index>1</Index></Indexes>"

BATCHES = [WAVELENGTHS[i:i + MAX_PER_READ] for i in range(0, len(WAVELENGTHS), MAX_PER_READ)]
OUT = Path(r"C:\Users\sdl2\Projects\agilent-cytation-server\captures") / (
    datetime.now().strftime("%Y%m%dT%H%M") + "_fullplate_sweep_gen5")

log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    log_lines.append(line)


def procedure_for(wavelengths):
    return build_absorbance_procedure_xml(
        detection="Absorbance", read_type="EndPoint", wells="Full Plate",
        wavelengths=wavelengths, use_lid=False, read_speed=READ_SPEED,
        measurements_per_datapoint=MPD, delay_ms=DELAY_MS)


def _close(experiment) -> None:
    """Close the Gen5 document, working around a biotek_driver bug.

    `Experiment.close()` calls `self._plates_object.release_all_plates()`, which
    `Plates` does not define, so it raises AttributeError *before* reaching its
    own `_invoke_method("Close")` - leaving the document open and the next
    `new_experiment_ex` failing with "A document is already in memory".
    """
    try:
        experiment.close()
    except AttributeError:
        experiment._invoke_method("Close")   # noqa: SLF001 - see above


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"=== full sweep {WAVELENGTHS[0]}-{WAVELENGTHS[-1]} nm step 10, "
        f"{len(WAVELENGTHS)} points in {len(BATCHES)} reads, "
        f"{READ_SPEED}/{MPD}/{DELAY_MS}ms, full 96-well plate ===")

    bt = Biotek(reader_name=READER_NAME, communication="serial", com_port=COM_PORT)
    if bt.app.test_reader_communication() != 1:
        log("reader not connected; aborting before any hardware action")
        return 1

    # Without this every GetRawData comes back with empty arrays - it is what
    # made the 2026-08-23 baseline run produce timing and no values. Gen5 only
    # retains raw data as the reader produces it when this is on.
    bt.app.data_export_enabled = True
    log(f"data_export_enabled = {bt.app.data_export_enabled}")

    # ONE EXPERIMENT PER BATCH. Gen5 refuses `SetProcedure` once a dependent
    # plate has been read ("Modifying a procedure requires that none of the
    # dependent plates be read") and biotek_driver only LOGS that failure - so
    # reusing one experiment silently re-read batch 1's wavelengths ten times
    # on 2026-08-23. Gen5 also holds one document at a time, hence close().
    results, t_all = [], time.perf_counter()
    for i, batch in enumerate(BATCHES, 1):
        proc = procedure_for(batch)
        experiment = bt.app.new_experiment_ex(
            definition_type=1,
            definition_str=build_bti_protocol_xml(
                protocol_type="Standard", calibration_plates=0,
                custom_procedure_xml=proc))
        if experiment is None:
            log(f"batch {i}: Gen5 refused the protocol definition; stopping")
            break
        plate = experiment.plates.add()
        plate.set_procedure(proc)
        plate.keep_plate_in_after_read()   # sealed plate loaded; never open the drawer

        # Verify the procedure actually took. This is the check whose absence
        # cost a whole broken sweep: SetProcedure fails silently.
        live = plate.get_procedure() or ""
        missing = [w for w in batch if f"<Wavelength>{w}</Wavelength>" not in live]
        if missing:
            log(f"batch {i}: procedure did NOT take - missing {missing}; stopping")
            _close(experiment)
            break

        status, message = plate.validate_procedure(False)
        if status != 1:  # BTI_OK
            log(f"batch {i}: validate_procedure -> {status} {message!r}; stopping")
            _close(experiment)
            break

        t0 = time.perf_counter()
        monitor = plate.start_read()
        if monitor is None:
            log(f"batch {i}: start_read returned None; stopping")
            break
        while monitor.read_in_progress:
            time.sleep(0.1)
        dt = time.perf_counter() - t0
        errs = monitor.get_all_errors() if monitor.errors_count else []

        # GetRawData hands back ONE plate row at ONE wavelength per call and
        # removes it from the plate, so it must be drained in a loop: 8 rows x
        # len(batch) wavelengths = up to 48 calls. `wavelength_index` is 0 on
        # every call, so the wavelength is identified by call order - calls
        # arrive in groups of 8 (one per plate row) in the batch's order.
        points, call = [], 0
        while True:
            status, raw = plate.get_raw_data()
            vals = list(raw.get("value", []) or []) if isinstance(raw, dict) else []
            if not vals:
                break
            wl = batch[min(call // 8, len(batch) - 1)]
            for r_, c_, v, ps in zip(raw["row"], raw["column"], vals, raw["primary_status"]):
                points.append({"well": f"{'ABCDEFGH'[int(r_)]}{int(c_) + 1}",
                               "wavelength_nm": wl, "value": float(v),
                               "primary_status": int(ps),
                               "dataset_name": str(raw.get("dataset_name", "")),
                               "call": call})
            call += 1
            if call > 200:
                log(f"batch {i}: safety stop draining at {call} calls")
                break
        real = [q for q in points if q["value"] > -9999]
        results.append({"batch": i, "wavelengths_nm": batch, "seconds": round(dt, 2),
                        "errors": errs, "calls": call, "points": len(points),
                        "real": len(real), "off_scale": len(points) - len(real)})
        log(f"batch {i}/{len(BATCHES)}: {batch[0]}-{batch[-1]} nm in {dt:.2f} s"
            f"  {len(points)} pts, {len(real)} real, {len(points)-len(real)} off-scale"
            + (f"  ERRORS {errs}" if errs else ""))
        (OUT / f"batch{i:02d}.json").write_text(
            json.dumps(points, indent=1), encoding="utf-8")
        _close(experiment)   # free the single Gen5 document slot for the next batch

    total = time.perf_counter() - t_all
    pts = sum(len(r["wavelengths_nm"]) for r in results)
    log(f"TOTAL {total:.1f} s ({total/60:.1f} min) for {pts} wavelengths x 96 wells")

    (OUT / "run.json").write_text(json.dumps({
        "kind": "Gen5 full-plate absorbance sweep, Sweep mode",
        "wavelengths_nm": WAVELENGTHS,
        "read_speed": READ_SPEED,
        "measurements_per_data_point": MPD,
        "delay_after_plate_movement_ms": DELAY_MS,
        "wells_read": "full plate (96) - partial plate unsupported via this API",
        "transport": f"Gen5 COM over serial COM{COM_PORT} (FTDI vendor driver)",
        "total_seconds": round(total, 2),
        "wavelengths_measured": pts,
        "seconds_per_well_wavelength": round(total / (pts * 96), 4) if pts else None,
        "baseline_normal_8_100": {"seconds_for_6_wavelengths": 312.52},
        "reference_pylabrobot": {"path": "captures/20260817T1748_C3_spectrum",
                                 "seconds": 568.8, "wavelengths": 58, "wells": 1},
        "batches": [{k: v for k, v in r.items() if k != "datasets"} for r in results],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    (OUT / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    log(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
