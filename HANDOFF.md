# Handoff — current state

**Last updated 2026-08-19.** If you are picking this repo up cold, read this
first, then [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for what still
needs bench time and [`RUNBOOK.md`](RUNBOOK.md) for day-to-day operations.

This file is a *status* document. It gets rewritten, not appended to — an
earlier version described an overnight batch whose every claim ("68/68 tests",
"hardware not touched", "do not flip protocol yet") had become false, which is
worse than no handoff at all.

## Where things stand

The service is deployed on `sdl2-pc-03-cytation` as the NSSM service
`cytation`, port 8040, reporting STATUS_SPEC **v1.2** against real hardware.
106 tests pass. The Cytation's FTDI chip is bound to **libusbK**, so Gen5 and
`biotek_driver` cannot reach the reader until you swap back (RUNBOOK §5).

| Subsystem | State |
|---|---|
| Reader connection, drawer, plate tracking | working |
| Imaging — brightfield | **verified through REST** on hardware |
| Imaging — autofocus / auto-exposure | implemented, never run on a real subject |
| Imaging — phase contrast | driver permits it (firmware 2.09), never imaged |
| Imaging — fluorescence | **blocked**: 4 filter-cube slots, all empty |
| Incubator | verified to ramp; never confirmed to reach setpoint |
| Shaker | verified empty; never with liquid |
| Reads (absorbance / fluorescence / luminescence) | **never completed on hardware** |

The read path is the one significant gap. The call now reaches the instrument
with correct arguments, but on an empty carrier the driver's acknowledgement
assertion fails, so it needs a plate and a person. That is what
`docs/IMPLEMENTATION.md` exists for.

## Two things that will bite you

**The live service runs from the working tree.** The venv holds an editable
install pointing at the checkout, so whatever is checked out *is* what
executes — there is no build step between `git checkout` and what the
instrument does.

This is currently benign: `fix/optical-write-surface` was merged to `main`
(fast-forward, 2026-08-12) and the PC sits on `main`, so the deployed code and
the default branch agree. It stops being benign the moment someone works on a
branch here — checking out anything that predates a fix silently reverts the
reader, with no error until the next restart. If you need a working branch on
this PC, stop the service first or use a separate clone.

**PySpin is not in the lockfile and `uv run` deletes it.** FLIR does not
publish to PyPI, so the wheel is installed out-of-band and any plain `uv run`
prunes it — which is how this deployment lost its camera between 2026-05-21
and 2026-08-12 without anyone noticing. The NSSM service therefore runs
`uv run --no-sync`. If you rebuild the venv, re-install the wheel and check:

```powershell
C:\SDL_Tools\uv.exe run --no-sync python -c "import PySpin; print('PySpin OK')"
```

A missing PySpin is not silent any more: `/status` reports
`components.imaging.connected: false` with the reason in
`details.imaging.camera_error`, and captures return 412 `camera_not_ready`
instead of a 500.

## Still to do

1. **Bench-verify the reads** — [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).
2. ~~**Apply the skill-catalog patch**~~ — applied 2026-08-19 in
   `ac-organic-lab`: 15 SkillDefs matching the live OpenAPI, plus a typed
   `PlateReaderClient`. See [`docs/LABSKILLS.md`](docs/LABSKILLS.md).
3. **Decide on filter cubes** if fluorescence imaging matters. Nothing in
   software unblocks it.
4. **Watch pylabrobot v1** — PR #1000 restructures the machine interfaces and
   touches `biotek_backend.py`; a `CytationMicroscopyBackend` on a side branch
   suggests reader and imager split apart. Unreleased; we pin 0.2.1.

## Repo map

```
src/agilent_cytation_server/
  api.py            # FastAPI app: spec endpoints + /control/* (16 verbs)
  service.py        # state machine, activity/§2.3, allowed_actions, components
  reader.py         # the ONLY module importing pylabrobot; real + stub readers
  errors.py         # typed precondition failures -> HTTP 412 bodies
  control_args.py   # request/response models; arg ranges mirror the driver's
  claims.py         # v1.1 cooperative claim manager
  plate_state.py    # per-well sample tracking, persisted to state.json
  plates.py         # custom_96 / agilent_shallow_96 geometry -> PLR Plate
  models.py         # re-exports the sdl-lab-contract wire types

docs/
  IMPLEMENTATION.md # bench verification plan  <- start here for testing
  LABSKILLS.md      # lab-skills catalog patch for the central server
  PLATE_STATE.md    # per-well tracking design
  INDEX.md          # which vendor manual answers which question
  notes/            # lab-authored reference (optics, channels, read paths)

RUNBOOK.md          # driver swaps, logs, restart, update  <- start here for ops
```
