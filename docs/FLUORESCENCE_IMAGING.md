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

> **Check the LED cubes before ordering anything.** Every `i` subcommand the
> driver issues has been audited: `q` (filters), `o`/`h` (objectives and
> annuli), `L` (LED **on/off only**), `F` (focus). There is **no LED inventory
> command at all**, so no amount of software work can report them — this is a
> physical look, the same way the objective turret was.
>
> The filter cubes, by contrast, *are* genuinely queried, and the raw replies
> confirm all four positions empty:
> `i q1`..`q4` each answered a blank field plus `0000`. Unlike
> `installed_objectives`, `installed_filters: []` describes the right turret.
>
> Note also from the manual: each position is **one LED cube with a filter cube
> screwed on top**, four positions total. So a channel needs its own LED cube —
> one 365 nm LED does not serve all four — and with no filter cubes fitted, any
> LED cube present will be sitting exposed in the slide.

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

## Step 1 result — measured 2026-09-09

Done. Column 12's emissive solid, scanned through the working read path with a
water blank subtracted on every point and Rhodamine 6G alongside as a control.

**Unknown emitter: excitation max ~390 nm, emission max ~490 nm.**

| scan | result |
|---|---|
| synchronous (em = ex + 60) | broad peak near ex 420, hard cutoff past 600 nm |
| emission at fixed ex 420 | 450: 65,935 / 470: 109,773 / **490: 137,101** / 512: 120,192 / 530: 95,045 / 550: 64,515 / 570: 38,231 / 630: 2,994 |
| excitation at fixed em 490 | 360: 78,718 / 375: 131,687 / **390: 159,866** / 405: 136,780 / 420: 147,010 / 435: 121,740 / 450: 100,604 |

Two properties that drive the cube choice:

* **Stokes shift is ~100 nm.** Standard cubes are built for the 20-60 nm shifts
  of fluorescent proteins, and their dichroics are placed accordingly. This
  emitter does not fit the common four well.
* **Excitation is broad** — everything from 375 to 450 nm sits above 60 % of
  peak — so the cube does not have to centre on 390.

**Control validated the method.** Rhodamine 6G peaked at ex 500 / em 560 on the
synchronous diagonal (1,189,443 counts, clean rise and fall either side), which
is where a fluorophore with ex ~526 / em ~555 lands on that constraint.

**Caveat on the Rhodamine wells:** they read 2.2-2.9 OD at 526 nm. Fluorescence
wants OD < 0.1; above ~2 the inner-filter effect flattens and apparently
red-shifts the excitation spectrum. The four-step dilution spanning only
2.2 → 2.9 OD is stray-light compression, not real. Those wells are outside the
linear range and should not be used quantitatively.

## Step 2 — choose the cubes from the measurement

Match the cube to the measured maxima, not to a guess.

Confirmed specifications, against the measured ex 390 / em 490:

| cube | part | EX | EM | fit for this emitter |
|---|---|---|---|---|
| DAPI | `1225100` | 377/50 (352-402) | 447/60 (417-477) | **excitation dead-centre on 390**; emission catches the blue half only, missing the 490 peak |
| GFP | `1225101` | 469/35 (452-487) | 525/39 (506-545) | excitation down at ~60 %; emission catches 506-545 where the emitter is still 70-88 % |
| CFP | *confirm* | ~434 | ~477 | best balance on paper — ~85 % excitation, emission near the peak |
| Texas Red | `1225102` | 586/15 | 647/57 (dichroic 605) | useless here — the emitter is dark past 630 |
| RFP | `1225103` | — | — | too red |
| CY5 | `1225105` | — | — | far too red |

LED cubes confirmed: `1225001` = 465 nm (GFP/CFP), `1225002` = 590 nm
(Texas Red), `1225004` = 505 nm. **A DAPI cube needs a violet/near-UV LED
whose part number is not confirmed** — `1225001` at 465 nm is far too red for
a 377/50 excitation filter.

### The full cube range — read the driver, not the catalogue

**Corrected 2026-09-10.** An earlier revision of this file said nothing pairs
UV excitation with red emission. That was wrong, and it was wrong because it
was researched from Agilent's public product pages, which list roughly half
the range. PyLabRobot's own `_load_filters` map is the better source:

| part | mode | note |
|---|---|---|
| `1225121` | **C377_647** | **377 nm excitation, 647 nm emission** — UV in, deep red out |
| `1225123` | **C400_647** | 400 nm excitation, 647 nm emission |
| `1225116` | GFP_CY5 | 469 -> 647 |
| `1225117` | RFP_CY5 | red -> far red |
| `1225115` | TAG_BFP | ~402 -> ~457 |
| `1225122` | OXIDIZED_ROGFP2 | ratiometric |
| `1225109` | ACRIDINE_ORANGE | ~500, dual emission |
| `1225107`, `1225110`, `1225119` | CFP, CFP-YFP FRET, FRET V2 | |
| `1225100`-`1225106`, `1225111`-`1225114`, `1225118` | the common set | DAPI, GFP, Texas Red, RFP, CY5, YFP, CY7, PI, PE, Chlorophyll A, CY5.5, CFP FRET V2 |

The naming convention matters: **`C<excitation>_<emission>`** is an explicit
family of cross-band cubes. `1225121` (C377_647) shares DAPI's 377/50
excitation filter, so it takes the same 365 nm LED (`1225007`).

**So ask Agilent by that convention** — "do you make a `C377_525` or
`C377_593`?" — rather than describing the requirement. If the C-family extends
to green and yellow emission, that answers the whole question; nothing in
either the driver map or the catalogue currently shows one.

**Recommendation: do not pick from the common four on the strength of a guess.
Send Agilent the measured spectrum.** A ~100 nm Stokes shift is exactly the
case where an off-the-shelf choice loses most of the signal.

If a stock cube must be chosen without that conversation, **DAPI `1225100`**
gives the best excitation match — its 352-402 window sits on the emitter's
peak — at the cost of collecting only the blue side of the emission. Confirm
its LED cube part number before ordering; the filter is useless without it.

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
