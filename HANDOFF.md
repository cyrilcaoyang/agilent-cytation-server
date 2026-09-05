# Handoff — current state

**Last updated 2026-09-04.** If you are picking this repo up cold, read this
first, then [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for what still
needs bench time and [`RUNBOOK.md`](RUNBOOK.md) for day-to-day operations.

This file is a *status* document. It gets rewritten, not appended to — an
earlier version described an overnight batch whose every claim ("68/68 tests",
"hardware not touched", "do not flip protocol yet") had become false, which is
worse than no handoff at all.

## Where things stand

The service is deployed on `sdl2-pc-03-cytation` as the NSSM service
`cytation`, port 8040, reporting STATUS_SPEC **v1.2** against real hardware.
220 tests pass.

**The Zadig driver swap is retired.** The reader stays on FTDI's vendor
driver and a D2XX transport shim talks through it (`config.toml` has
`ftdi_transport = "d2xx"`), so this PC no longer binds the chip to libusbK
and RUNBOOK §4/§5 are history. D2XX still opens the device exclusively, so
Gen5 and the service cannot both hold it — but trading is now
`nssm stop cytation` / `nssm start cytation`, not a 20-minute GUI procedure.
Watch out for Gen5's status line: it keeps showing "Status: Ready" from
stored config while the service holds the device; "Temperature: ???" is what
actually tells you it is not communicating.

| Subsystem | State |
|---|---|
| Reader connection, drawer, plate tracking | working |
| Imaging — brightfield | captures work, but see 2026-09-04: the "4X" frame is a ~0.6× field formed by **no objective**; 20X/40X positions image nothing |
| Imaging — focus axis | **moves, verified on the wire 2026-09-04** — PyLabRobot's command was malformed below 9.3993 mm; fixed in `_set_focus`. Only accepted at turret code 1 |
| Imaging — autofocus / auto-exposure | auto-exposure works; autofocus is meaningless until an objective is in the path (§4 of `docs/BENCH_2026-09-04.md`) |
| Imaging — objective turret | **open**: five of six positions give one identical blank frame; needs a physical look at what is fitted |
| Imaging — phase contrast | driver permits it (firmware 2.09); same illumination as brightfield on 2026-09-04 (both black through an opaque plate) |
| Imaging — fluorescence | **blocked**: 4 filter-cube slots, all empty |
| Incubator | verified to ramp; never confirmed to reach setpoint |
| Shaker | verified empty; never with liquid |
| Reads — absorbance | **verified on hardware**, matches a Gen5 sweep |
| Reads — fluorescence / luminescence | never completed on hardware |
| Incubator — sub-ambient setpoint | never commanded; 18 °C is only the *declared* floor |

Absorbance landed 2026-08-23/24 (A1 0.0841 vs Gen5's 0.084, C5 2.5394 vs
2.525). It had been failing for a non-obvious reason worth knowing before you
debug a read: PyLabRobot's command checksum (`sum(cmd) % 100`) is **rejected
by the instrument whenever it lands in 94-99**, which surfaces as the reader
"not being in a state to run it" and sends you chasing plate state instead.
`_pad_for_checksum` grows the read rectangle until the checksum clears and
discards the extra wells. 41-93 are unreachable with a single-well region and
remain untested, so the guard is conservative. The earlier "column 1 is
unreadable" finding was wrong and is retracted.

Fluorescence and luminescence are now the read-path gap — see
[`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

**Incubator temperature is unreadable while the shaker runs** (measured
2026-08-24) — a hardware limit, not a locking bug: the `"h"` query gets no
reply at all, and the late reply is then consumed as the *next* command's
response (a 0.0 °C reading observed where the truth was 23.6). `/status`
skips the query while shaking and says so in `components.incubator.message`.
This already cost a campaign: the 2026-08-21 30 h solubility run recorded no
temperature across its entire six-hour heated phase. Getting a series during
a shaken incubation means **pausing the shaker around each read**, which is
actuation and belongs in workflow code, never in a side-effect-free poll.

## Bench session 2026-08-31 — what is now verified on hardware

Ran the shipped-but-unproven set against the instrument. Four things passed,
two new bugs fell out, and one design decision was proved wrong on its first
day. Detail in `docs/BENCH_2026-08-31.md`.

| Item | Result |
|---|---|
| Shake abort leaves the link usable | **PASS** — link answered after the abort; `activity: running` observed mid-shake |
| Drawer interlock | **PASS** — all six checks: 412 `drawer_open`, correct body shape, actions withheld, `last_error` untouched, read succeeds once closed |
| `last_error` auto-clear (§6.4) | **PASS** — a stale `read.luminescence` cleared on the next successful read |
| Fluorescence reads | **PASS**, first time ever on hardware — 7 command shapes, several wells and wavelength pairs |
| Luminescence reads | **PASS except any region whose maximum corner is H12** — see below |
| Imaging autofocus + auto-exposure | auto-exposure **PASS**; the autofocus half is **retracted 2026-09-04** — every focus command it issued below 9.3993 mm was rejected by the instrument |
| Objective selection | ~~FAILS for 40X~~ — **retracted 2026-09-04**, see the bench session below |

### New bug: luminescence rejects any region ending at H12

Reproducible. `H12` alone, `H12+H11`, `G11..H12`, and the **whole plate
`A1..H12`** all fail; `H11`, `G12`, `A1..H11`, `A1..G12`, `A1`, `D3`, `B7`
and `A1-A4` all succeed. Absorbance and fluorescence read H12 fine, so it is
luminescence-only.

**It is not the checksum band**, which was the first guess. Computing PLR's
luminescence checksum (`(sum(cmd) + 8) % 100`) predicts the opposite of what
happens: `H11` → 99 and `B7` → 97 are both inside the rejected 94-99 band and
both succeed, while `H12` → 01 and `H12+H11` → 00 sit outside it and both
fail. So `_pad_for_checksum` is the wrong tool here and extending it to
luminescence would not help.

Consequence worth weighing: a **full-plate luminescence read is impossible
today**, which is the common case for a luminescence assay, not an edge case.
Single-well failures come back in 0.1 s (rejected at the start command) while
whole-plate fails after ~12 s (runs, then the response assertion fails) — two
different failure points, so there may be two causes.

### Retracted 2026-09-04: "the 40X objective returns the 20X image"

The 2026-08-31 evidence was pixel identity between the 20X and 40X frames and
a timing argument. Both are explained by what the 2026-09-04 turret probe
found — every turret position other than code 1 produces the **same** blank
illumination gradient, and the turret demonstrably moves for each code — so
this was never an objective-selection bug. Kept here so the retraction is as
visible as the claim was; detail in `docs/BENCH_2026-09-04.md` §5–6.

### Retracted 2026-09-04: "settle the 4X field of view with a calibration target"

No target needed. The "4X" frame spans 13.9 mm; PyLabRobot's own `capture()`
puts a 4X field at 3.474 mm; the ratio is 4.00. The frame also ignores 9 mm
of proven Z travel. It is not formed by a 4X objective — see below.


## Bench session 2026-09-04 — the focus axis, and what the turret is doing

Full evidence in `docs/BENCH_2026-09-04.md`. Every item below rests on the
instrument's own replies, read from the D2XX byte trace
(`[instrument].ftdi_trace = true`, `scripts/trace_frames.py`).

| Item | Result |
|---|---|
| Focus command encoding | **BUG, FIXED** — PLR sends a 6-digit field below 9.3993 mm; the instrument wants 7 and refuses (`570F`, ~15 ms). `_set_focus` pads to 7 and checks the reply. Re-traced: all accepted, latency scales with travel |
| Focus at 20X / 40X | **refused at every position**, 94 probes at 0.1 mm, plate height irrelevant — the window is per turret code and only code 1 has one |
| Turret probe (service stopped, raw `P0e01`–`06`) | code 1: plate visible at ~0.6×, Z-independent. Codes 2–6: **one identical blank gradient**, focus refused everywhere. Inventory (`h2–h7`) is condenser annuli, not objectives |
| Opaque plate | `agilent_96_700ul_square_flat` is ~OD 5.7 to the brightfield LED — cannot be imaged in transmission; LED verified fine on an empty carrier |
| `5A00` | carrier-stage "motion complete" (`A` close, `W6` well select) — not an error, never was |
| Manual insert via front-panel button | service keeps `drawer: out`; `drawer.close` resyncs (idempotent). Design options below |

**The one that matters:** everything optical observed since August — the
4× too-wide field, infinite depth of field, blank 20X/40X, identical frames
across positions — fits **no objective in the light path at any turret
position**. Whether the objectives are missing, fitted where the codes don't
reach, or present with a misrouted camera is a *physical inspection*. Until
that is done, treat every capture in `captures/` as a ~0.6× overview image,
not microscopy, and treat `details.imaging.installed_objectives` as "which
phase annuli the condenser reports", which is what it actually is.

### Human intervention at the drawer

`_drawer` is dead reckoning and the front-panel button makes the service a
non-exclusive actor, so a stale belief is guaranteed eventually. Three moves,
in increasing cost; the second is the one to take:

1. **Belief with provenance** — `details.drawer_source: commanded | assumed`.
   Honest, cheap, stops nothing.
2. **Ensure-closed instead of refuse** — `read.*` / `imaging.capture` send `A`
   first rather than 412-ing on a belief. They are already actuation. Keep the
   412 for a drawer *we* opened and never closed (a genuine known-open);
   auto-close only when the belief is assumed. Refuse what we know; fix what
   we merely assume.
3. **Detect it** — untested whether the firmware volunteers bytes on an eject
   press. With the trace on and the idle baseline one `h` pair per 4.4 s, it
   is a one-press experiment.

The asymmetry to remember: a stale `out` is annoying (one `drawer.close`); a
stale `in` is the dangerous one — plate removed, service never learns, next
read returns numbers from an empty carrier.

### The option-A assertion was wrong on day one

`_restore_persisted_plate` asserted last Friday's solubility plate at
startup. The plate physically on the carrier was a *different* one — the
crystallization plate from Friday — and the service had no way to know. This
is exactly the failure mode argued in the option-A discussion, observed
within a day of shipping. Nothing broke, because `plate.load` metadata is
bookkeeping and reads address wells positionally, but it means
`details.plate_restored_at_startup` is doing real work and a reader should
treat a restored plate as a claim. It also cost a real sample disturbance:
the shake test in item 1 ran on a crystallization plate believed to be blank.

### Smaller gap found in the §6.4 work

`plate.load` / `plate.unload` / `well.update` do not clear `last_error` —
they are not bracketed by `_operation`. `plate.load` does drive the
instrument (it sends `set_plate`), so by §6.4's table it should clear. A
stale `last_error` was observed sitting on a `ready` device after a
successful `plate.load`.

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
5. **A campaign lock (`workflow.start` / `workflow.end`)** — see below.
6. ~~**A drawer interlock**~~ — shipped 2026-08-30. The three reads and
   `imaging.capture` now raise `DrawerOpen` → 412 `drawer_open` while the
   carrier is out, mirrored in `allowed_actions` through one helper per §6.2.
   It blocks only on a **known-open** drawer: there is no position query in
   the driver's command set, so `_drawer` is dead reckoning (seeded `in` at
   connect, moved only when we move it), and a stale `out` would refuse every
   read on a correctly loaded instrument with no operator override. It
   therefore does **not** catch the front-panel eject button — nothing can,
   see below.
7. ~~**`last_error` never auto-cleared**~~ — shipped 2026-08-30 (§6.4). Only
   `startup` used to clear it, so one transient fault sat on the tile through
   every later successful read until a restart. Now cleared on the first 2xx
   from any operational action; refusals, `/status`, and the claim verbs do
   not clear it. This was the last unticked §6 item on the v1.1 checklist.
8. ~~**No published `last_error.code` taxonomy**~~ — shipped 2026-08-30
   (best practice #6): `LAST_ERROR_CODES` in `service.py`, table in the
   README, and a test deriving the codes from the call sites so a new action
   cannot quietly introduce an undocumented one.

### Trap: a restart drops the reader's plate, and reloading wipes the samples

Found 2026-08-30 while verifying a restart. Two behaviours compose into a
data-loss footgun; neither is new, and both are worth knowing before you
touch `plate.load` on a plate that matters.

1. **`_plate_loaded()` asks the reader, not the store** — deliberately, and
   its docstring says so: the store survives a restart, the reader's
   PyLabRobot `Plate` resource does not. So after any service restart
   `details.plate_in_reader` is `false` while `details.loaded_plate` still
   names a plate, and **`read.*` / `imaging.capture` drop out of
   `allowed_actions`** until a plate is assigned again. Both facts are on the
   envelope, so nothing is hidden — but they read as a contradiction if you
   only look at `loaded_plate`.

2. **`plate.load` without a `wells` array replaces the wells with 96 empty
   ones** (`PlateStateStore.load_plate`: `wells if wells is not None else
   self._empty_wells_96()`). So the obvious fix for (1) — re-POST
   `plate.load` with just a `plate_id` — silently destroys every
   `sample_id` / `volume_ul` / `notes` entry recorded for that plate.

**Resolved 2026-08-31 by re-assigning the plate at startup** (option A).
`_restore_persisted_plate` now hands the store's plate back to the reader
after a successful connect, so a restart no longer produces the
contradiction or the pressure to re-`plate.load` blind. Footgun (2) still
exists — `plate.load` without `wells` still blanks them — but you no longer
have a reason to reach for it after a restart.

**What the restore asserts.** Nothing on this instrument reports whether a
plate is physically present, so this is the service trusting a file about
the state of the world: lift the plate out while the service is down and the
reader will claim one is there. That was the argument against option A and
it is a real cost, accepted deliberately against a destroyed well map. It is
kept visible rather than silent — `details.plate_restored_at_startup` is
`true` for a plate asserted from disk and `false` once an operator's own
`plate.load` supersedes it. **Anything reasoning about a restored plate
should treat it as a claim, not an observation.** The restore is best-effort:
a failure logs and leaves the old behaviour (no plate, optical actions
withheld), never a failed startup.

### Deferred: a campaign lock for long workflows

**Decided 2026-08-30, not started.** A workflow that holds the reader for
hours was invisible on the dashboard. The immediate half shipped in
`ac-organic-lab` (`PlateReaderTile` now renders `details.claimed_by` as an
"In use by …" banner and disables its controls), which is enough to *see* the
lock but inherits the claim's lifetime: a claim dies when its heartbeat stops,
so a crashed orchestrator silently releases a reader with a plate still
incubating in it.

The deferred half is a device-side lock that outlives individual claims,
copying `agilent-hplcms-server`'s proven shape rather than inventing one:

- `POST /control/workflow/start {owner, run_id}` / `POST /control/workflow/end`
  (idempotent), with `details.workflow = {active, owner, run_id, started_at}`
  on `/status`.
- Non-holder `/control/*` → **423** with `{"error": "workflow_active", ...}`;
  `workflow.start` leaves `allowed_actions` while a workflow is active and
  `workflow.end` joins it, per §6.2.
- Released only by an explicit `workflow.end` — **not** by a TTL. That is the
  whole point, and the one thing the claim cannot do.

`equipment_status` stays `ready` and `activity` stays `idle` between steps.
Reporting `busy` for the lock's duration was considered and rejected: §2.3
requires `activity` to be observed from hardware and pairs `busy` with
`running`, so it would make `cycles_total` and every utilization series
meaningless, and would hide real reads inside a multi-hour fake span.

Also needs, outside this repo: a `plate_reader` SkillDef pair in
`ac-organic-lab`'s catalog, tile copy for the 423 shape, and a decision on
whether `execute_plan` should hold one claim for a whole run instead of one
per step (it currently releases between steps).

### Not possible: blocking the front-panel eject button

Asked 2026-08-30, answered no. The Cytation 5's carrier eject button is an
unconditional front-panel control ("located above the reader's power switch",
IFU p22 / p38) and there is **no lockout command** anywhere in PyLabRobot's
Cytation surface — the whole set is `J` open, `A` close, `i` home, `C`/`e`
identity, `h`/`g` temperature, `y` set_plate, `t` config, `D`/`O` read, `x`
abort, `&` slow mode. Whether the firmware itself ignores the button mid-read
is undocumented and would take a bench test to establish.

So the only software play is detection, not prevention, and the drawer
interlock above is what makes an ejected plate report as a named precondition
rather than an unexplained driver assertion.

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
