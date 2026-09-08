> **Historical report:** this describes the first overnight branch and its
> environment incident. Integration against `b77c2f1` and the subsequent three
> fixes are recorded in [the current to-do list](TODO_2026-09-08.md).

# Overnight reliability work — 2026-09-08

Prepared on `fix/overnight-reliability` in an isolated checkout. **Not deployed.**
Use [the morning checklist](IMPLEMENTATION.md) before any supervised deployment
or hardware validation.

## Changes prepared

- Same-ID plate reloads retain omitted well maps and plate models. An explicit
  well list still replaces the map; a different plate ID starts empty.
- Proposed loads are validated before changing reader resources. Disk failures
  return HTTP 503 and retain the old memory/file state. Reader assignments are
  rolled back; failed rollback disconnects the reader.
- Startup cancellation disposes of the partial reader. Capture cleanup covers
  illumination setup and acquisition startup failures, attempts LED shutdown
  even when acquisition stop fails, and preserves the primary exception.
- Real captures use unique PNG names and JSON sidecars with plate, settings,
  UTC timestamp, software provenance, tuning, and calibration status. Metadata
  save failures are visible and retain the image for investigation.
- D2XX unit tests no longer need the vendor package/DLL. Added failure-injection
  tests and a Linux/Windows, Python 3.10/3.12 CI matrix using the lockfile.
- Added `scripts/bench_check.py`: plan-only by default; explicit supervised
  execution, plate/state checks, claims/heartbeats, error journals, no automatic
  retries, restarts, USB access, heating, or shaking.
- Replaced the outdated bench plan with the current capability matrix and
  reconciled the handoff and plate-state documentation.

## Validation

The final Windows Python 3.10 suite passed: **273 passed, 2 skipped** in
10.71 seconds. Both skips are expected (an unreachable checksum case and
a fallback test superseded when PLR is installed); two existing HTTP-422
deprecation warnings remain. Full output is in the accompanying
`test-results.txt`. The minimal Python 3.12 test environment also passed the
D2XX tests without installing `ftd2xx`: **24 passed, 1 skipped** (optional PLR
integration unavailable). Python compilation and `git diff --check` passed.
CI configuration is prepared, not yet run on GitHub. Hardware checks were not
performed and the instrument was not commanded.

## Environment incident and recovery

During test-environment setup, a sandbox-local checkout was not visible to the
Windows `uv` process. Despite receiving a nonexistent `--project` directory,
`uv sync` warned and fell back to the live project. The requested test Python
version differed from the live environment, so it began removing the live
`.venv` and failed on a locked file in `Scripts`. The live `Lib` directory and
activation helpers were removed. **This was an assistant error.**

Recovery was completed before continuing the software work:

1. Exported exact dependencies from the existing, unchanged `uv.lock`.
2. Built a separate recovery environment in Windows Temp with Python 3.10.20,
   the lockfile's dependencies, and the cached PySpin 4.3.0.190 CPython 3.10
   wheel. Restored cached editable metadata pointing to the original source.
3. Verified imports for PySpin, NumPy 1.26.4, OpenCV, D2XX, PyLabRobot, FastAPI,
   Uvicorn, and the service API before copying libraries back.
4. Restored the missing live `Lib` directory and activation helpers. Repeated
   imports with the live interpreter; the dependency check reported **44
   compatible packages**. The restored set includes all locked extras and
   PySpin; it is not a forensic reconstruction of undocumented prior installs.
5. Queried Windows service state: `cytation` was **RUNNING**, with service and
   Windows exit codes both zero. No service restart, Gen5 launch, driver swap,
   or instrument command was performed.

The service source, `config.toml`, `state.json`, and measurement files were not
changed by recovery. Running-state and import checks do not establish hardware
function after a fresh restart; leave that to the supervised morning check.
The recovery environment remains at
`C:\Users\sdl2\AppData\Local\Temp\cytation-recovery-20260907`.

Further Windows test runs used an explicitly verified Temp source directory;
the runner asserted the imported source path. No subsequent package command
used project discovery to select an environment. Future setup must verify paths
in the same filesystem/process context as the package tool before installation.

## Still open

Physical plate/drawer confirmation and durable campaign reservation remain
unimplemented. Optical fit-out, calibrated microscopy, H12/full-plate
luminescence, quantitative fluorescence, stable incubation, and shaking with
liquid still need the supervised checks in the capability matrix. The prepared
code intentionally retains startup plate restoration with its provenance flag.

## Concurrent work and integration

The user confirmed Claude is also working in the live checkout. This isolated
branch starts at `2dcc3c1fee7ebc86247988438a602308bc19d3a0`. During delivery,
the live checkout had advanced to `1a507f3` (saturation reporting) and contained
additional edits in `reader.py` plus a new `reply_check.py`. Those changes were
left untouched and are not included in this branch's test results.

Integration needs particular attention in `reader.py`: preserve Claude's reply
checker and focus-refusal handling alongside this branch's capture cleanup and
provenance changes. Preserve the newer saturation-reporting API as well. Apply
this branch's commit through Git with conflict review after coordinating the
other work, then run the combined suite before deployment. The independent
checkout is a review artifact, not a replacement for the newer live tree.
