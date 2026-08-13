# Bench verification plan

**Written 2026-08-12 for a bench session on 2026-08-13.** Everything below
needs a human at `sdl2-pc-03-cytation` with a physical plate. The service,
drivers and REST surface are already deployed and healthy; what is missing is
the half of the write surface that cannot be exercised on an empty reader.

Read [`RUNBOOK.md`](../RUNBOOK.md) for day-to-day operations (driver swaps,
logs, restart). This file is only the test plan.

---

## 0. State before you start

| Thing | Expected | How to check |
|---|---|---|
| Service | `RUNNING` | `sc.exe query cytation` |
| Envelope | `equipment_status: ready` | `curl http://127.0.0.1:8040/status` |
| Camera | `camera_ready: true` | `details.imaging.camera_ready` |
| FTDI driver | bound to **libusbK** | `Get-PnpDevice \| ? InstanceId -match VID_0403` → `Class: libusbk devices` |
| Firmware | `2.09` | `equipment_version` |
| Git branch | `main` | `git -C C:\Users\sdl2\Projects\agilent-cytation-server branch --show-current` |

> **The live service runs from whatever is checked out.** The venv holds an
> editable install pointing at the working tree, so there is no build step
> between `git checkout` and what the instrument does. The work described here
> was merged to `main` on 2026-08-12 and the PC is on `main`, so you can leave
> the checkout alone. Do not switch branches mid-session without stopping the
> service — a checkout that predates a fix reverts the reader silently, and you
> will not find out until the next restart.

Claims are **enforced**, so every `/control/*` call needs an `X-Claim-Token`.
Get one with `POST /control/claim {owner, session_id}` and release it at the
end. The helper script in §6 does this for you.

---

## 1. What to bring

- **A 96-well plate that fits `agilent_shallow_96` or `custom_96`** (geometry
  is in `config.toml`). Clear flat bottom for imaging.
- **An absorbance standard.** Anything with a known peak works for a first
  pass — a dye dilution series, or even food colouring, is enough to prove the
  read path returns sane, well-varying numbers. A blank column matters more
  than a certified standard: it gives you the baseline to compare against.
- **A fluorescent standard** if you want to test FL — fluorescein (ex 485 /
  em 528) is the obvious choice and is within the 250–700 nm range.
- **Optional: something visible under 4×** for the imaging tests. Cells,
  beads, or printed text under the plate all work for confirming focus.
- **Black-walled clear-bottom plates** if you have them — worth comparing
  against a clear-walled plate for the glare question.

You do **not** need any new hardware for tests 1–7. Fluorescence *imaging*
(as opposed to fluorescence reads) is the one thing blocked on a purchase —
the filter wheel reports 4 slots, all empty.

---

## 2. Test 1 — absorbance read (the important one)

**This is the only major path never verified on hardware.** The plumbing is
fixed and correct as far as it can be checked without a plate, but on an empty
carrier the driver's acknowledgement assertion fails, so the last unknown is
whether a real read completes.

```
POST /control/plate/load        {"plate_id": "bench_20260813", "model": "agilent_shallow_96"}
POST /control/read/absorbance   {"wells": ["A1"], "wavelength_nm": 600}
```

Expected: `200` with `{"wells": {"A1": <float>}}`.

Then widen: several wells including a blank, and a wavelength where your dye
actually absorbs. Check that blanks read near zero and that wells differ from
each other — a read that returns identical values for every well is a bug, not
a measurement.

**If it fails**, the response now carries a real message instead of the empty
`{"detail": ""}` it used to. Capture:

```powershell
Get-Content C:\SDL_Logs\cytation.err.log -Tail 40
```

The most likely failure is still an `AssertionError` from
`biotek_backend.py:373` — the instrument rejecting the command. If that
happens *with* a plate loaded, the next thing to check is whether the drawer
is physically closed (our `drawer` state is assumed at startup, not observed),
then whether the plate geometry in `config.toml` matches the plate you used.

## 3. Test 2 — fluorescence and luminescence

```
POST /control/read/fluorescence {"wells": ["A1"], "excitation_nm": 485, "emission_nm": 528}
POST /control/read/luminescence {"wells": ["A1"], "focal_height_mm": 7.0}
```

Ranges are enforced client-side, so out-of-band values give you a 422 naming
the field rather than a driver crash: absorbance 230–999 nm, ex/em 250–700 nm,
focal height 4.5–13.88 mm.

**Note there is no `gain` parameter and passing one is a 422.** PyLabRobot's
Cytation backend exposes no gain control on any read. If your fluorescence
signal is weak, the levers are focal height and the sample itself — not gain.

## 4. Test 3 — imaging on a real sample

Brightfield through REST is already verified (2026-08-12, 2448×2048 PNG in
`captures/`), but only on an empty light path. With a sample:

```
POST /control/imaging/capture {"well": "A1", "channel": "brightfield",
                               "objective": "O_4X_PL_FL_Phase",
                               "focal_height_mm": 10.0, "exposure_ms": 8}
```

Then the two things worth learning:

**Autofocus / auto-exposure.** Both are implemented but have never run against
a real subject, where the sharpness and exposure metrics actually have
something to bite on:

```
POST /control/imaging/capture {"well": "A1", "channel": "brightfield",
                               "autofocus": true, "auto_exposure": true}
```

The response echoes the **resolved** `focal_height_mm` and `exposure_ms`, with
the search detail under `details.tuning`. Compare the resolved focal height
against what you'd pick by eye. Each search round is a real exposure, capped at
8 rounds.

**Phase contrast.** Firmware 2.09 means the driver permits it
(`details.imaging.phase_contrast_available: true`) and all three objectives are
`PL_FL_Phase`, but it has never been imaged here:

```
POST /control/imaging/capture {"well": "A1", "channel": "phase_contrast"}
```

If it errors, the likely cause is the phase annulus not being in the condenser
— a hardware/setup issue, not software. This is the most promising answer to
the glare question, so it is worth the attempt on an unstained sample.

## 5. Test 4 — incubator and shaker with a plate in

Both were verified empty on 2026-08-12 (30 °C → `heating` / "Ramping to
30.0 C"; shake start → `activity: running`). What is still unknown:

- **Does it actually reach setpoint?** Set 37 °C, then poll `/status` until
  `components.incubator.state` flips from `heating` to `at_setpoint`. Time it.
  If it never arrives, the tolerance band is `_TEMPERATURE_TOLERANCE_C = 0.5`
  in `service.py`.
- **Does cooling work? Probably not — and the range is wrong at both ends.**
  The driver hardcodes `supports_cooling = True` and clamps to an absolute
  4–45 °C. The Cytation 5 spec sheet gives the incubator as **4 °C above
  ambient → 65 °C**, i.e. heating-only, which makes PyLabRobot's "4 °C" look
  like an absolute-vs-relative misreading and its 45 °C ceiling ~20 °C short
  of the instrument. Two things to settle at the bench:
  - set something below ambient and see whether the reading actually falls
    (expect: no);
  - note that 50 °C is currently **refused with a 422** by our arg model even
    though the instrument supports it. If you need above 45 °C, that bound
    lives in `control_args.py::TemperatureArgs` and in the driver — widening
    ours alone is not enough, since `set_temperature` re-checks the driver's
    `temperature_range`.
- **Shaking with liquid in the wells.** Empty shaking proves the command
  works; it says nothing about splashing at a given displacement. Start at
  `displacement_mm: 3` and watch before trusting 1 (which is the *fastest*
  setting — the parameter runs inversely to speed).

Remember the 16-minute ceiling: PyLabRobot re-issues the shake command each
time it lapses and warns the door may briefly open at the boundary. Do not
leave it shaking unattended.

## 6. Running the tests

The scratch scripts used on 2026-08-12 are a working starting point — they do
the claim/release dance and print the interesting fields:

- `verify_read.py` — claim → plate.load → absorbance → status → unload
- `verify_capture.py` — claim → plate.load → brightfield capture → DAPI refusal
- `verify_new.py` — identity → incubator → shaker

They are not committed (they live in the session scratchpad). If you want them
in the repo, `scripts/capture_a1.py` is the existing model to follow.

A minimal manual session:

```powershell
$body = @{owner="bench"; session_id=[guid]::NewGuid().ToString()} | ConvertTo-Json
$claim = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8040/control/claim -Body $body -ContentType application/json
$H = @{"X-Claim-Token" = $claim.claim_token}

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8040/control/plate/load `
  -Headers $H -ContentType application/json `
  -Body '{"plate_id":"bench","model":"agilent_shallow_96"}'

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8040/control/read/absorbance `
  -Headers $H -ContentType application/json `
  -Body '{"wells":["A1"],"wavelength_nm":600}'

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8040/control/release -Headers $H
```

## 7. Preconditions you will hit (and what they mean)

These are refusals, not faults — the device is healthy and declining an
inapplicable request. None of them writes `last_error` or turns the tile red.

| Response | Meaning | Fix |
|---|---|---|
| `412 plate_not_loaded` | No plate assigned in the reader | `POST /control/plate/load` |
| `412 camera_not_ready` | Camera did not initialise | Check PySpin; `shutdown` then `startup` |
| `422` naming a filter cube | Fluorescence channel with no cube fitted | Buy a cube; not a software issue |
| `422` naming a field | Out-of-range wavelength / focal height / a `gain` on a read | Fix the request |
| `423` | Someone else holds the claim | Wait, or check `details.claimed_by` |

`GET /status.allowed_actions` always tells you what is currently permitted, and
never advertises something the endpoint would refuse.

## 8. What to record

Worth writing down, because it feeds decisions rather than just the log:

1. **Whether absorbance completed**, and the numbers for a blank vs a sample.
   This is the gate on the whole read path being declared verified.
2. **The resolved autofocus focal height** for your plate + objective. Once
   known, it becomes the sensible default to hard-code per plate type instead
   of searching every capture.
3. **Time to reach 37 °C**, and whether cooling does anything.
4. **Whether phase contrast produced an image**, and how it compares to
   brightfield on the same well for glare.
5. **Any `AssertionError`** with the surrounding log lines — those are the
   instrument rejecting a command, and the message now says so rather than
   being empty.

Update `README.md`'s capability table with whatever you learn, and
[`ROADMAP.md`](https://github.com/cyrilcaoyang/ac-organic-lab) in the monorepo
if the read path graduates to verified.

## 9. Known-unverified list (as of 2026-08-12)

| Path | State |
|---|---|
| `read.absorbance` / `read.fluorescence` / `read.luminescence` | never completed on hardware |
| `imaging.capture` brightfield | ✅ verified through REST |
| `imaging.capture` phase contrast | driver permits it; never imaged |
| `autofocus` / `auto_exposure` | implemented; never run on a real subject |
| `incubator.set_temperature` | verified to ramp; never confirmed to arrive |
| Cooling below ambient | driver claims support; unconfirmed on this unit |
| `shake.start` / `shake.stop` | verified empty; never with liquid |
| Fluorescence imaging | blocked — 4 cube slots, all empty |
