r"""Absorbance sweep on one well through Gen5, for timing comparison.

Counterpart to captures/20260817T1748_C3_spectrum/, which swept 230-800 nm in
10 nm steps as 58 separate single-wavelength PyLabRobot reads (~9.8 s each,
568.8 s total).

What this can and cannot do, established against the live Gen5 on 2026-08-23:

  * `ReadType = Spectrum` is refused by `NewExperimentEx` at every schema
    version tried - "Value Spectrum ... is not supported in this version".
    Only `EndPoint` is accepted. A true spectral scan has to be authored in
    the Gen5 GUI, saved as a .prt, and run via `new_experiment(path)`.
  * An EndPoint ReadStep accepts at most SIX <Measurement> entries
    ("Measurement Index=7 ... unexpected").

So the fastest route the COM XML API allows is batches of 6 wavelengths per
read, which is what this measures: 58 points in ceil(58/6) = 10 reads.

REQUIRES Gen5 mode (FTDI vendor driver bound) and the cytation service stopped.

    .venv\Scripts\python.exe gen5_spectrum.py                 # validate only
    .venv\Scripts\python.exe gen5_spectrum.py --read          # all 10 batches
    .venv\Scripts\python.exe gen5_spectrum.py --read --batches 1
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r"C:\Users\sdl2\Projects\biotek_driver")

from biotek_driver.biotek import Biotek
from biotek_driver.utils.bti_status_codes import get_error_message
from biotek_driver.xml_builders.partial_plate_builder import build_bti_partial_plate_xml
from biotek_driver.xml_builders.procedure_builder import build_absorbance_procedure_xml
from biotek_driver.xml_builders.protocol_builder import build_bti_protocol_xml

WELL = "C3"
WAVELENGTHS = list(range(230, 801, 10))          # 58 points, matches the reference
MAX_PER_READ = 6                                 # Gen5's EndPoint limit
COM_PORT = 4
READER_NAME = "Cytation5"
REFERENCE = "captures/20260817T1748_C3_spectrum"
REFERENCE_SECONDS = 568.8
OUT_ROOT = Path(r"C:\Users\sdl2\Projects\agilent-cytation-server\captures")

DO_READ = "--read" in sys.argv
LIMIT = None
if "--batches" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--batches") + 1])

BATCHES = [WAVELENGTHS[i:i + MAX_PER_READ] for i in range(0, len(WAVELENGTHS), MAX_PER_READ)]
stamp = datetime.now().strftime("%Y%m%dT%H%M")
OUT = OUT_ROOT / f"{stamp}_{WELL}_spectrum_gen5"

log_lines: list[str] = []


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    log_lines.append(line)


def procedure_for(wavelengths: list[int]) -> str:
    # UseLid False: the plate in the reader is sealed, not lidded, and Gen5's
    # lid handling changes the height it reads at. Wells stays "Full Plate" -
    # Gen5 rejects "Partial Plate" there; set_partial_plate() narrows it.
    return build_absorbance_procedure_xml(
        detection="Absorbance",
        read_type="EndPoint",
        wells="Full Plate",
        wavelengths=wavelengths,
        use_lid=False,
        read_speed="Normal",
        measurements_per_datapoint=8,
    )


def main() -> int:
    todo = BATCHES if LIMIT is None else BATCHES[:LIMIT]
    log(f"=== Gen5 sweep, well {WELL}, {len(WAVELENGTHS)} wavelengths "
        f"{WAVELENGTHS[0]}-{WAVELENGTHS[-1]} nm step 10, "
        f"{len(todo)}/{len(BATCHES)} batches of <={MAX_PER_READ} ===")

    bt = Biotek(reader_name=READER_NAME, communication="serial", com_port=COM_PORT)
    code = bt.app.test_reader_communication()
    log(f"test_reader_communication -> {code} "
        f"({'CONNECTED' if code == 1 else get_error_message(code)})")
    if code != 1:
        log("not connected; aborting before any hardware action")
        return 1

    protocol_xml = build_bti_protocol_xml(
        protocol_type="Standard", calibration_plates=0,
        custom_procedure_xml=procedure_for(todo[0]))
    experiment = bt.app.new_experiment_ex(definition_type=1, definition_str=protocol_xml)
    if experiment is None:
        log("Gen5 refused the protocol definition")
        return 2
    log("experiment created")

    if not DO_READ:
        log("phase 1 only (no --read); definition validates, stopping before the read")
        _write(OUT, [], None, {"validated": True, "read": False})
        return 0

    results: list[dict] = []
    t_all = time.perf_counter()
    for i, batch in enumerate(todo, 1):
        plate = experiment.plates.add()
        if plate is None:
            log(f"batch {i}: plates.add() returned None; stopping")
            break
        plate.set_procedure(procedure_for(batch))
        # No partial plate: Gen5 answers SetPartialPlate with "Procedure does
        # not support runtime well selection", and every ReadStep <Wells> value
        # that would enable it is refused by NewExperimentEx. So this reads all
        # 96 wells - more work than the single-well reference, not less.
        # Never open the drawer unattended - a sealed sample plate is loaded.
        plate.keep_plate_in_after_read()

        status, message = plate.validate_procedure(False)
        if status != 1:  # BTI_OK == 1
            log(f"batch {i}: validate_procedure -> {status} {message!r}; stopping")
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
        raw = plate.get_raw_data()
        results.append({"batch": i, "wavelengths_nm": batch, "seconds": round(dt, 3),
                        "errors": errs, "raw": str(raw)})
        log(f"batch {i}/{len(todo)}: {batch[0]}-{batch[-1]} nm "
            f"({len(batch)} pts) in {dt:.2f} s"
            + (f"  ERRORS {errs}" if errs else ""))

    total = time.perf_counter() - t_all
    log(f"total {total:.2f} s for {sum(len(r['wavelengths_nm']) for r in results)} points "
        f"in {len(results)} reads")
    _write(OUT, results, total, {"validated": True, "read": True})
    log(f"wrote {OUT}")
    return 0


def _write(out: Path, results, total, meta) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "procedure_example.xml").write_text(procedure_for(BATCHES[0]), encoding="utf-8")
    if results:
        (out / "raw_data.txt").write_text(
            "\n\n".join(f"--- batch {r['batch']} {r['wavelengths_nm']} ---\n{r['raw']}"
                        for r in results), encoding="utf-8")
        rows = []
        for r in results:
            per_ms = round(r["seconds"] / len(r["wavelengths_nm"]) * 1000)
            for nm, val in _parse(r["raw"], r["wavelengths_nm"]):
                rows.append((nm, val, per_ms, r["batch"]))
        if rows:
            with (out / f"{WELL}_uvvis.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, quoting=csv.QUOTE_ALL)
                w.writerow(["wavelength_nm", "absorbance", "elapsed_ms", "batch"])
                w.writerows(sorted(rows))

    measured = sum(len(r["wavelengths_nm"]) for r in results) if results else 0
    projected = (total / measured * len(WAVELENGTHS)) if measured else None
    (out / "run.json").write_text(json.dumps({
        "kind": "Gen5 absorbance sweep, batched EndPoint reads",
        "well": WELL,
        "wells_read": "full plate (96) - partial plate unsupported via this API",
        "wavelengths_nm": WAVELENGTHS,
        "max_wavelengths_per_read": MAX_PER_READ,
        "transport": f"Gen5 COM over serial COM{COM_PORT} (FTDI vendor driver)",
        "spectrum_read_type": "refused by NewExperimentEx: 'Value Spectrum of token "
                              "BTIProcedure | StepList | ReadStep | ReadType is not "
                              "supported in this version'",
        "reference_run": REFERENCE,
        "reference_seconds": REFERENCE_SECONDS,
        "points_measured": measured,
        "total_seconds": None if total is None else round(total, 2),
        "projected_full_sweep_seconds": None if projected is None else round(projected, 1),
        "speedup_vs_reference": None if not projected else round(REFERENCE_SECONDS / projected, 2),
        "batches": [{k: v for k, v in r.items() if k != "raw"} for r in results],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
    }, indent=2), encoding="utf-8")
    (out / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")


def _parse(raw, wavelengths):
    """Gen5 raw data shape depends on the procedure; raw_data.txt always keeps
    the original so nothing is lost to a parsing guess."""
    nums = []
    for tok in str(raw).replace("\t", ",").replace("\n", ",").split(","):
        tok = tok.strip()
        try:
            nums.append(float(tok))
        except ValueError:
            continue
    if len(nums) == len(wavelengths):
        return list(zip(wavelengths, nums))
    return []


if __name__ == "__main__":
    raise SystemExit(main())
