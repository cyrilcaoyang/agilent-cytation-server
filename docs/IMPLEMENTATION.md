# Current capability matrix and supervised bench plan

Updated **2026-09-08**. Hardware evidence now includes the **September 8** read-focus checks recorded
in commit `866a830`; the revised reliability branch is prepared separately and
**has not been deployed**. See [the current to-do list](TODO_2026-09-08.md).
This document supersedes the August 12 bench plan. Historical evidence is in
[BENCH_2026-09-04.md](BENCH_2026-09-04.md) and
[BENCH_2026-08-31.md](BENCH_2026-08-31.md).

## What is established

| Capability | Evidence / limitation | Next acceptance check |
|---|---|---|
| D2XX connection, drawer commands | Vendor FTDI driver is the deployed path. Gen5 and the service require exclusive access. | Verify the running service and actual plate before tests. No Zadig swap. |
| Plate metadata | Persisted and restored at startup; physical identity/presence is not sensed. Front-panel intervention can leave drawer state stale. | Match physical plate ID, geometry, contents, and orientation to the saved record. |
| Absorbance | Completed on hardware and compared with Gen5, August 23/24. Checksum workaround is in place. | A blank and reference well provide a useful session baseline. |
| Fluorescence plate reads | Commands completed August 31 across seven shapes. This establishes the read path, not quantitative assay calibration. | Compare a suitable standard and blank with Gen5 using recorded settings. |
| Read focus / refusals | September 8: the tested 19 mm plate model refused 4.5–5.6 mm with `5B00`; 5.8 mm and above worked. General command and read-body checks now surface refusals. | Match the physical geometry and validate the usable range; do not assume this resolves H12. |
| Saturation | `over_range` names saturated wells whose values serialize as null. | Verify consumers handle saturation explicitly. |
| Luminescence | Reads completed except regions ending at H12; a full plate also fails. | Compare H11, G12, H12, and full plate with Gen5, one case at a time. |
| Camera / brightfield captures | Frames and auto-exposure work. Observed field and focus behavior do not establish working microscopy. | Obtain a focused, credible-magnification image in Gen5 first, then reproduce via API. |
| Focus encoding | Seven-digit fix bench-verified September 4. Code 1 accepted the tested positions; other tested codes refused them. | Small focus series only after confirming the optical path; no further broad blind sweeps. |
| Objectives / phase contrast | Reported configuration may describe condenser annuli. Missing or misrouted objectives remain hypotheses requiring physical confirmation. | Verify fitted optics and Gen5 configuration through normal operator access. |
| Fluorescence imaging | Four cube slots reported empty; optical path also unresolved. | Confirm fit-out before deciding on parts. |
| Incubator | Ramp observed; stable arrival at setpoint and sub-ambient performance unverified. API range 18–65 °C is declared capability, not proof of performance. | Reach and hold an appropriate setpoint on a dedicated test plate; record time and stability. |
| Shaker | Empty operation and stop/link recovery verified. Liquid behavior unverified. Temperature cannot be queried reliably while shaking. | Short observed liquid test, then stop, read temperature, and verify link recovery. |

An HTTP 200, a camera connection, or a firmware acknowledgement is not by
itself a validated scientific measurement. In particular, do not label the
existing overview frames as calibrated 4× microscopy.

## Morning sequence

1. **Identify and preserve.** Check the actual plate before any movement,
   heating, or shaking. Save `state.json`, `/status`, the current Git revision,
   and the service log tail. Use a dedicated test plate; do not assume the
   plate restored from disk is the one currently on the carrier. Avoid
   `plate.load` without the complete well map on the currently deployed code.
2. **Resolve optics in Gen5 (45–60 minutes).** Stop the NSSM `cytation` service
   before opening Gen5. Keep the vendor FTDI driver. Verify what optics are
   fitted/configured through normal operator access, and use a clear-bottom
   test plate or suitable calibration target. Record objective, well, plate
   geometry, exposure, gain, focus, image dimensions, and scale. A Gen5
   "Ready" label is not proof of communication; verify an actual acquisition.
   If Gen5 cannot form a focused image, stop software focus experiments and
   resolve the hardware/configuration question.
3. **Compare the API (about 30 minutes).** Close Gen5 and confirm it releases
   the instrument, then start the service. Physically verify the test plate
   again. Match the Gen5 well, objective, gain, and LED intensity; use fixed exposure and at most
   five nearby focus positions. The helper exposes `--gain` and `--led-intensity`
   (defaults 0 and 10). Save images and the D2XX trace. Judge actual
   image structure and scale, not only a focus-score maximum.
4. **Luminescence boundary (about 30 minutes).** Use the same dedicated test
   plate/settings in Gen5 and the API. Test H11, G12, H12, and full plate
   separately. Record both the command rejection and any response-parser
   failure. After a failure inspect status and the trace before the next case;
   no automated retry, reconnect, checksum padding, or omission of H12.
5. **Thermal/shaker qualification if time remains.** Use a dedicated liquid
   plate, record ambient temperature and fill volume, reach and hold the
   selected setpoint, then perform a short observed shake. Start with the
   previously tested displacement of 3 mm. Stop shaking before temperature
   reads, then check link recovery. Do not start an unattended campaign.

The desired deliverable is a focused Gen5 image and matching API image, **or
an explicit hardware/configuration blocker**, plus an H12 comparison journal.

## Repeatable API helper

`scripts/bench_check.py` uses only the Python standard library and talks to
the already running API. It never opens USB, restarts the service, changes
USB drivers, heats, or shakes. Without `--execute`, it prints a plan and makes
no connection. Run with an existing interpreter directly; avoid `uv run`
without `--no-sync` on the device PC because PySpin is installed separately.

Examples from the checkout containing this helper (PowerShell):

```powershell
# Snapshot only: no claim or /control request.
.\.venv\Scripts\python.exe scripts\bench_check.py snapshot --execute

# Inspect the plan first. Use the objective/focus established in Gen5.
.\.venv\Scripts\python.exe scripts\bench_check.py imaging `
  --plate-id bench_20260908 --objective O_4X_PL_FL_Phase `
  --focus-mm 9.8 10.0 10.2 --exposure-ms 8

# Only after physically confirming the dedicated test plate and closed drawer:
.\.venv\Scripts\python.exe scripts\bench_check.py imaging `
  --plate-id bench_20260908 --objective O_4X_PL_FL_Phase `
  --focus-mm 9.8 10.0 10.2 --exposure-ms 8 --execute --confirm-test-plate

.\.venv\Scripts\python.exe scripts\bench_check.py luminescence `
  --plate-id bench_20260908 --case H11 --execute --confirm-test-plate
# Repeat separately with --case G12, H12, and full after inspecting each result.
```

The helper requires the matching plate to already be assigned, the API to be
ready/idle, and the drawer to be recorded in. Your physical confirmation is
still essential: the service's drawer state is an estimate. It claims the
reader, checks state again, and reasserts a restored plate **with its complete
well map**, which is compatible with the older service. Heartbeats run every
five seconds; a heartbeat failure stops subsequent measurements. Claims are
released in cleanup. A timed-out request may still be executing on the
instrument: inspect before retrying.

Each run gets a unique `captures/bench_<UTC>_<id>/` journal containing the
request parameters, response/error, status code, and elapsed time. Claim tokens
are excluded. API images remain at the server-returned paths; the helper does
not silently copy remote files. Add the Gen5 export, settings screenshots,
physical observations, and trace to the same evidence folder. Use `--output`
for an explicit archive location.

## Before deploying tonight's changes

The revised branch retains remote saturation/refusal handling, requires a
confirmed focus acknowledgement, releases the transaction lock on refused
setup commands, and preserves claim TTL across heartbeats. It also preserves
wells on same-ID reload, rejects invalid plate
loads before changing the reader, rolls back failed persistence, handles
startup cancellation, improves capture cleanup, and saves unique PNG/JSON
pairs. Explicit `wells` still replaces the supplied map; another plate ID starts
with empty wells. Restored-plate and manual-drawer uncertainty remain.

Review the isolated branch and test report. Deployment requires a supervised
stop, preserving config/state/camera dependencies, applying the reviewed code,
and a restart followed by the bench checks above. Do not change the active
checkout while the live editable installation uses it.
