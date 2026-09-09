# Getting fluorescence imaging working

**Status 2026-09-09: blocked on hardware that was never fitted.** This is the
plan to unblock it, in the order that spends the least money on the wrong part.

Fluorescence *reads* already work and need none of this — that is the
monochromator path, verified on hardware 2026-08-31 and again 2026-09-07
(Rhodamine 6G in column 9 read 1.7 M counts at ex 526 / em 555). If the
question is "how much is emitting in each well", the instrument answers it
today. Everything below is for **imaging**: where in the well the emission is,
and what it looks like.

## What is actually missing

Three separate things, and only the first is a surprise.

| | state | evidence |
|---|---|---|
| Objectives | **none fitted** | Turret opened 2026-09-08. One unmarked wide-field lens (~0.6x), one `4X` blanking plug, remaining positions empty. See [`BENCH_2026-09-08.md`](BENCH_2026-09-08.md). |
| Filter cubes | **none fitted**, 4 empty bores | `installed_filters: []`; bores photographed 2026-09-08 |
| LED cubes | **unknown** | The driver does not report them at all — see the warning below |

A fluorescence channel needs **all three**: an objective to form the image, an
LED cube to excite, and a filter cube to separate excitation from emission.
Missing any one of them produces nothing.

> **Check the LED cubes before ordering anything.** They live in their own
> turret and PyLabRobot never queries it, so `/status` cannot tell you what is
> there. A filter cube bought without its matching LED cube is inert. This has
> to be a physical look, the same way the objective turret was.

## Step 1 — measure the emitter before buying a cube

**Do this first.** Filter cubes are roughly $1-2k each and need a matched LED
cube on top, so a wrong band is expensive waste. The instrument can tell you
the right one: the fluorescence read path spans **ex 250-700 nm, em 250-700 nm**
and is known good.

The plate that motivated this carries an emissive solid suspended in water in
column 12, plus Rhodamine 6G in column 9 as a known reference. Scanning both
gives the target band and a sanity check on the method in the same run.

Suggested shape, coarse then fine:

```
# Coarse: find the ridge. ~25 nm steps, one well of the unknown emitter
#   ex 300 -> 650, em (ex + 30) -> 700
# Fine: 5 nm around the coarse maximum, both axes
# Reference: repeat on the Rhodamine 6G well; its published maxima are
#   ex ~526 nm, em ~555 nm, so a scan that recovers those is trustworthy
```

Two constraints that will bite otherwise:

- **Emission must be at least ~20-30 nm redder than excitation**, or the
  monochromator passes scattered excitation light and you measure the lamp.
- **Focal height must be reachable.** On the 19 mm plate anything below
  ~5.7 mm is refused with `5B00` (measured 2026-09-08). Use 7.0 mm.

Record: the ex/em maxima of the unknown emitter, its emission bandwidth, and
whether the signal is bright enough to image rather than merely to read.
A read integrates a whole well; imaging spreads the same photons over a few
million pixels, so a weak read means a hopeless image.

**A cleaner variant, if the solid is precious or the plate is busy:** put the
emitter alone in a fresh clear-bottom plate. Cleaner spectra, no cross-talk
from the methylene blue series, and no risk to the dilution work.

## Step 2 — choose the cubes from the measurement

Match the cube to the measured maxima, not to a guess.

| filter cube | part | LED cube needed |
|---|---|---|
| DAPI | `1225100` | (confirm — UV/blue) |
| GFP | `1225101` | `1225001` (505 nm, GFP/CFP) |
| RFP | `1225103` | `1225002` (623 nm, Red/Texas Red) |
| CY5 | `1225105` | (confirm — far red) |

Note for Rhodamine 6G specifically: it emits at ~555 nm, while the RFP cube's
emission window sits redder than that. An RFP cube catches the tail, not the
peak. That is exactly the kind of mismatch Step 1 exists to prevent.

Four slots are free, so there is room to grow — but buy the one band the
current chemistry needs, prove the path end to end, then extend.

## Step 3 — an objective, because a cube alone still images nothing

Ordered 2026-09-09: **4X phase, part `1320515`** (Plan Fluorite, NA 0.13,
WD 17 mm). Its phase annulus `1320520` is already installed, so phase contrast
works on arrival with no further purchase.

For fluorescence specifically, be aware what NA 0.13 means: **light collection
scales with NA squared**, so a 4X at 0.13 gathers roughly an eighth of what a
20X at 0.45 does. On a dim emitter the 4X may need exposures long enough to
bleach it. If Step 1 shows the emission is weak, a higher-NA objective matters
more than another cube — `1320517` (20X phase, NA 0.45) is the next step, and
its annulus `1320521` is also already fitted.

## Step 4 — verify the path end to end when the parts arrive

In this order, stopping at the first failure:

1. **Physical fit-out.** Objective seated, filter cube seated, matching LED
   cube seated. Photograph each turret so the record exists.
2. **`/status` reports it.** `installed_filters` should stop being empty.
   Note that `installed_objectives` will *still* be wrong until the driver is
   fixed — it reads condenser annuli, not the objective turret, and will keep
   claiming 4X/20X/40X regardless of what is fitted.
3. **Brightfield first.** A focused brightfield image with a credible scale,
   measured against the 9 mm well pitch. Until brightfield is right,
   fluorescence cannot be.
4. **Fluorescence capture** on the well Step 1 characterised, at the measured
   band. Compare its intensity against the *read* of the same well: they
   should agree in rank order across a dilution series.
5. **Scale calibration.** With a real objective fitted, measure the field
   against a known spacing and record um/px. Every image in `captures/` before
   this point is ~0.6x and uncalibrated.

## Open questions worth one email to Agilent

1. Which **LED cubes** are currently fitted to this unit, and which are needed
   for the band Step 1 identifies?
2. Is a **10X phase annulus** available separately, and its part number? (A
   10X objective `1320516` would otherwise be brightfield-only — the fitted
   annuli are 4X, 20X and 40X only.)
3. Were objectives ever supplied with this instrument? The annuli for 4X, 20X
   and 40X are installed, which suggests the unit was specified with all three
   and they were never fitted or have been removed.

Assembly numbers from the 2026-09-08 photographs, for reference in that email:
`1850503` (imaging module housing) and `1380500 REV E` (cube turret assembly).

## What the software will do meanwhile

Independent of any purchase, and being done now:

- `installed_objectives` stops claiming objectives it cannot see. It reports
  condenser annuli and will say so.
- `_resolve_objective` stops *refusing* objectives on the strength of that
  inference — it currently enforces an inventory that does not describe the
  turret.
- `imaging.capture` declares its frames as uncalibrated ~0.6x overview, so
  nothing downstream sizes a feature from them.
- The capability matrix records **microscopy unavailable — hardware not
  fitted**, stated separately from `camera_ready`, which is true and
  misleading on its own.

## See also

- [`BENCH_2026-09-08.md`](BENCH_2026-09-08.md) — the turret inspection and its evidence
- [`BENCH_2026-09-04.md`](BENCH_2026-09-04.md) — the focus-encoding fix and the measurements that led here
- [`IMPLEMENTATION.md`](IMPLEMENTATION.md) — the capability matrix
