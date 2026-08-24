# Absorbance through Gen5 — what the COM API can do, and how fast

Established on the bench 2026-08-23 against the live Cytation 5 (serial
23030927) in Gen5 mode. Everything here is measured, not inferred from docs.

The question that started it: *is a full-spectrum UV-Vis read faster through
Gen5 than through PyLabRobot?* The reference was
`captures/20260817T1748_C3_spectrum` — 230–800 nm in 10 nm steps on one well,
as 58 single-wavelength PyLabRobot reads, 9.81 s each, **568.8 s total**.

---

## 1. Three hard limits in the Gen5 COM XML API

All three are reported by Gen5's own validating parser at `NewExperimentEx`,
before any hardware moves — so probing them is free.

**No spectral scan.** `ReadType = Spectrum` is refused:

```
Value Spectrum of token BTIProcedure | StepList | ReadStep | ReadType
is not supported in this version.
```

Identical at schema `Version` 1.00 and 2.00, and for `Spectral`,
`SpectrumScan`, `Scan`, `WavelengthScan`. **`EndPoint` is the only accepted
value.** The monochromator can sweep — Gen5's own `GetDataSetInfo` exposes
`spectrum_wavelengths` / `spectrum_start` / `spectrum_step` — but
`NewExperimentEx` does not let you *define* one. A real spectral scan must be
authored in the Gen5 GUI, saved as a `.prt`, and run with
`Application.new_experiment(path)`.

**Six wavelengths per read.** A seventh `<Measurement>` is refused:

```
<BTIProcedure | StepList | ReadStep | Measurements | Measurement Index="7">
property unexpected.
```

So 58 wavelengths is ≥10 reads.

**No partial plate.** Every `<Wells>` value that would enable runtime
selection (`Partial Plate`, `Runtime Selection`, `Partial`, `Selected Wells`,
`Runtime`) is refused at definition time, and `SetPartialPlate` answers
*"Procedure does not support runtime well selection"*. **All 96 wells or
nothing.** Note `biotek_driver` logs that refusal rather than raising, so a
`try/except` around `set_partial_plate` will not catch it.

---

## 2. Read-time model (measured)

Full plate, three clean reads:

| wavelengths | seconds |
|---|---|
| 2 | 31.3 |
| 4 | 52.2 |
| 6 | 71.2 |

≈ **10.0 s per wavelength + ~11 s fixed per read.** Wavelengths dominate; the
plate traverse does not. Packing six per read still helps (it amortises the
11 s), but only modestly.

> An earlier note in this repo claimed the cost was *all* fixed overhead,
> from a batch of four wavelengths taking the same 71 s as six. That batch was
> not actually running four wavelengths — see §3. The claim was wrong and is
> withdrawn.

### Read settings

| `ReadSpeed` / `MeasurementsPerDataPoint` / delay | 6 λ × 96 wells |
|---|---|
| `Normal` / 8 / 100 ms (Gen5 default) | 312.52 s |
| `Normal` / 1 / 0 ms | 254.41 s |
| `Sweep` / 8 / 100 ms | 254.33 s |
| **`Sweep` / 1 / 0 ms** | **72.51 s** |

**Neither knob does much alone; together they do everything.** Each saves
~58 s on its own; both together save 240 s. The two single-knob configs land
within 0.08 s of each other, which is the tell: a *continuous* traverse is
only possible when the reader never stops at a well, which requires exactly
one measurement per point. Ask for 8 and it must stop and average regardless
of `ReadSpeed`; ask for 1 in `Normal` and it still stops. The fast path needs
both conditions at once.

Accepted values: `ReadSpeed` ∈ {`Normal`, `Sweep`} (`Fast`/`Slow` refused);
`MeasurementsPerDataPoint` ∈ {1, 2, 4, 8, 16}; delay ∈ {0, 10, 100}.

### Full sweep, measured end to end

**58 wavelengths × 96 wells in 709.4 s (11.8 min)** at `Sweep`/1/0
(`captures/20260823T1804_fullplate_sweep_gen5`). Per-batch spread across
twenty reads was under 0.5 s.

| | 58 λ × 1 well | 58 λ × 96 wells | per well-wavelength |
|---|---|---|---|
| PyLabRobot (libusbK) | **568.8 s** | 54 605 s (15.2 h) | 9.81 s |
| Gen5 `Normal`/8/100 | — | 3 021 s (50.4 min) | 0.546 s |
| Gen5 `Sweep`/1/0 | — | **709.4 s (11.8 min)** | **0.128 s** |

**Choose by well count, not by driver preference:**

- **One or a few wells** → stay on PyLabRobot/libusbK. Gen5 cannot read fewer
  than 96 wells, so its floor is ~11.8 min against PyLabRobot's 9.5 min.
- **Whole plate** → Gen5 is ~77× faster per measurement. 12 minutes against
  15 hours. The driver swap pays for itself in a single run.
- **A true spectrum** (fine steps, or the full 230–999 nm) → neither. Author a
  `.prt` in the Gen5 GUI, per §1.

---

## 3. `biotek_driver` defects found

All three fail **quietly**, in ways that look like a result. Guard accordingly.

1. **`SetProcedure` failure is logged, not raised.** Gen5 refuses procedure
   edits once a dependent plate has been read:
   *"Modifying a procedure requires that none of the dependent plates be
   read."* Building a sweep as ten plates inside one experiment therefore
   re-reads **batch 1's wavelengths ten times**, silently. This produced a
   full 12-minute sweep of apparently valid data that was entirely wrong.
   **Guard:** after `set_procedure`, read `get_procedure()` back and assert
   every wavelength appears. `scripts/gen5_full_sweep.py` does this.
   **Fix:** one experiment per batch, closed between.

2. **`get_raw_data()` needs `Application.DataExportEnabled`.** Documented in
   its own docstring, not enforced — without it, every call returns empty
   arrays and a success status. Set `app.data_export_enabled = True` first.
   It also returns **one plate row at one wavelength per call** and removes
   that data from the plate, so it must be drained in a loop (8 rows ×
   wavelengths = up to 48 calls per read). `wavelength_index` is 0 on every
   call; wavelength is identified by call order.

3. **`Experiment.close()` raises `AttributeError`.** It calls
   `self._plates_object.release_all_plates()`, which `Plates` does not define,
   three lines before its own `_invoke_method("Close")` — so the document
   never closes and the next `new_experiment_ex` fails with *"A document is
   already in memory."* Work around by calling `_invoke_method("Close")`
   directly (see `_close()` in `scripts/gen5_full_sweep.py`).

Also: `get_data_set_names()` returns the **first** plate's dataset names for
every subsequent plate in an experiment. Do not trust them to label data.

---

## 4. Instrument temperature limits (same session)

`GetReaderCharacteristics` on this unit:

| characteristic | value |
|---|---|
| `eTemperatureControlOption` | `True` |
| `eTemperatureMin` | **18** |
| `eTemperatureMax` | **65** |
| `eTemperatureGradientMax` | 2 (spatial lid gradient, not a ramp in time) |

PyLabRobot hardcodes `(4.0, 45.0)` for every Cytation — `supports_cooling`
returns `True` unconditionally, manufacturing the 4 °C floor, and 45.0 is
commented *"default BioTek max"*. **Both ends are wrong for this instrument.**
See `TEMPERATURE_MIN_C` / `TEMPERATURE_MAX_C` in `models.py` and the
`_RangeCorrectedBackend` subclass in `reader.py::setup()`, which overrides the
upstream property because `set_temperature` validates against it.

Still unverified: whether this unit can actually hold a setpoint below
ambient. 18 °C is the *declared* floor; no low setpoint has ever been
commanded. Worth an upstream PR — the same hardcode is still present on
pylabrobot `main` at `pylabrobot/agilent/biotek/plate_reader_base.py:177`.

---

## 5. Consumable limits, measured

Read on a **quartz plate with a clear pierceable seal** (Agilent), 96 wells:

- **230–300 nm: 96/96 wells off-scale at every wavelength.**
- **310 nm: 2/96 off-scale. 320 nm and above: none.**

The cutoff is sharp and sits between **300 and 310 nm** — the polypropylene
cutoff of a clear pierceable film. The quartz plate is not the limit: the
17 Aug reference read 0.163 AU at 230 nm on quartz with no seal. **Sealed,
the entire UV region is unmeasurable.**

The trade is evaporation control against the short half of the spectrum, and
this consumable cannot give both. Options, in the order worth trying:

1. Seal for incubation, peel for reading (12 min per plate), re-seal.
   Piercing does not help — the beam crosses the intact film either way.
2. Read visible-only while sealed and use turbidity as the signal. This keeps
   an unattended run possible.
3. Source a UV-transparent sealing film — and verify it against a blank the
   same way, rather than trusting a datasheet.

### These are scattering curves, not spectra

Every trace is flat from 350 to 800 nm with a monotonic rise toward the blue
(C5: 2.56 → 2.51 AU across 450 nm), with **no absorption peak anywhere** —
expected, since neither glucose nor citric acid absorbs visible light. The
signal is turbidity from undissolved solid. Empty wells show the same shape at
1/20th the height, identifying the blue rise as the seal's own absorbance tail.

Wells above ~2.5 AU (C5, D5) are at the detector ceiling; treat them as
"opaque", not as quantitative values.

### `mpd=1` noise is free at this signal level

| | AU |
|---|---|
| read noise (sd, 90 low wells @ 700 nm) | **0.0037** |
| median \|C5 − D5\| (glucose replicates) | 0.125 |
| median \|C7 − D7\| (citric acid replicates) | 0.252 |

Replicates disagree by 34–68× the read noise. That spread is real sample
heterogeneity, so restoring 8-fold averaging would cost 4.3× in time and buy
nothing measurable. **Take `Sweep`/1/0.**

---

## 6. Escaping the driver swap — the D2XX transport

Everything in §1–§5 was gated on the reader link being bound to the *right*
driver, and RUNBOOK §4 documents why getting back is GUI-only. `ftd2xx_shim.py`
removes that constraint rather than automating around it.

**The idea.** PyLabRobot reaches the reader through `pylabrobot.io.ftdi.FTDI`,
which wraps `pylibftdi.Device` and therefore **libusb** — and on Windows libusb
needs libusbK/WinUSB bound, which is exactly what hides the chip from Gen5.
FTDI's **D2XX** API talks *through* the vendor driver instead. With the D2XX
transport the reader stays on FTDI permanently, both stacks coexist at the
driver level, and switching between them becomes `nssm stop cytation` — no
Zadig, no `pnputil`, no GUI, and therefore remotely operable by `sdl-lab-hostops`.

**The surface is small.** `FTDI` touches `pylibftdi.Device` through six device
members and nine `ftdi_fn` entry points, of which the BioTek backend exercises
twelve. So the shim substitutes that one object rather than reimplementing the
transport. It also replaces `FTDI._resolve_device_serial`, which is half the
payoff: the stock version enumerates with pyusb, cannot open a
vendor-driver-bound device, and aborts with `NotImplementedError: Operation not
supported` — the error a Gen5-mode reader produces today. D2XX enumerates via
`FT_CreateDeviceInfoList`, which works *because* the vendor driver is bound.

Two mappings needed care and are covered by tests:

- **Return convention.** libftdi returns `0`/`-1` and `FTDI` checks it; ftd2xx
  raises. Every wrapper translates, so a driver error surfaces as `-1` rather
  than escaping as a crash.
- **Read semantics.** D2XX's read blocks until the byte count is satisfied;
  libftdi returns what is buffered. The BioTek backend runs its own
  `_read_until` timeout loop and depends on the latter, so the shim reads
  `min(requested, FT_GetQueueStatus())` and returns `b""` on an empty queue.

libftdi's 1.5-stop-bit value has no D2XX equivalent and is refused with `-1`
rather than silently landing on `STOP_BITS_2` and mis-framing every byte. Flow
control needs no remap — libftdi's `SIO_*_HS` constants are byte-identical to
D2XX's `FLOW_*`.

**Enabling it:** `uv pip install ftd2xx` (extra: `d2xx`; the DLL itself ships
with FTDI's CDM driver and is already on any PC running Gen5), then
`[instrument].ftdi_transport = "d2xx"`. Default stays `"libusb"`.

### Bench verification — DONE 2026-08-23, transport confirmed

Verified on the real reader (serial 23030927) with the chip on FTDI's vendor
driver and **no libusbK bind anywhere**:

- `list_devices()` returns the reader — the premise. Under libusbK it returns
  `[]`, which is what the exclusivity looks like from the D2XX side.
- Every transport call passes: open, baudrate, line property, flow control,
  latency timer, both purges, RTS, non-blocking read, close.
- The service reaches `ready` with all five components connected, firmware
  `2.09`, camera up with three objectives, and a live temperature refreshing
  every ~2 s — all real command/response traffic through the vendor driver.
- **Absorbance reads agree with the Gen5 sweep to three decimals**, which is
  the check that matters, because it crosses both transports *and* both
  software stacks:

  | well | D2XX / PyLabRobot | Gen5 sweep |
  |---|---|---|
  | C5 | 2.5207 | 2.52 |
  | D5 | 2.636 | 2.63 |
  | D7 | 1.563 | 1.57 |
  | A12 | 0.0861 | ~0.08 |

**Step 5, answered: they cannot share, but they no longer have to swap.**
D2XX takes the device exclusively on open. With the service running, Gen5
connects and reports `Serial Number: 23030927, Status: Ready` — both from its
*stored* reader config — while `Temperature: ???` shows it is not actually
communicating. Stop the service and Gen5 reads temperature immediately. The
service is unaffected throughout: it stayed `ready` with a 0.0 s-old readback
while Gen5 was attempting to connect.

**Operational hazard worth knowing:** Gen5's status line lies. Serial and
"Ready" come from configuration, not from the instrument. **Temperature is the
honest indicator** — `???` means Gen5 does not have the reader, whatever the
status line says.

So the final shape is:

| | before | with D2XX |
|---|---|---|
| PyLabRobot → Gen5 | `pnputil` delete + rescan | `nssm stop cytation` |
| Gen5 → PyLabRobot | **Zadig / Device Manager, GUI-only** | `nssm start cytation` |
| remotely operable | no | yes (hostops, `cytation` is restartable) |

Not the full prize — concurrent access would have been better — but it turns a
20-minute GUI procedure with a GUI-only return leg into two service commands.

The exclusivity has one failure mode worth recognising, and it is now in the
error text: a device another process holds **still enumerates**, with empty
serial and description, so `resolve_device_serial` cannot match it and the
service comes up `requires_init`. Observed live 2026-08-23 with Gen5 still
connected. Disconnect in Gen5, restart the service.

### Unrelated bug found on the way: column 1 is unreadable via PyLabRobot

Any `read.absorbance` whose region includes **column 1** is refused instantly
(HTTP 503, `assert resp == b"\x060000\x03"` at `biotek_backend.py:373` — the
`"O"` start-read command NAKs). Anything else works:

```
A1  REJECTED      A12  0.0861
B1  REJECTED      H12  0.3838
C5,D5,C7,D7       all fine
```

**Not a transport fault.** Gen5 read A1 on this same plate the same evening
(0.0906 at 450 nm), the plate geometry checks out (12x8, A1 at y=70.99, 9 mm
pitch), and the command is index-based rather than coordinate-based — so the
suspicion is PyLabRobot's partial-region command format at `min_col = 1`.
Whether it reproduces on the libusb transport is **untested**; that needs a
driver swap back, which is exactly what this shim exists to avoid.

Practical consequence today: full-plate work through PyLabRobot will fail until
this is understood. Gen5 is unaffected.

The 22 unit tests verify the *mapping* against a fake handle. They cannot
verify it against a reader, because **D2XX cannot see the chip while it is
bound to libusbK/WinUSB** — `createDeviceInfoList()` returned 0 on this PC for
exactly that reason. Before trusting it:

1. Swap the reader to FTDI (RUNBOOK §5 — scriptable, no GUI).
2. `python -c "from agilent_cytation_server.ftd2xx_shim import list_devices; print(list_devices())"`
   — expect the reader's serial. This alone proves the premise.
3. Set `ftdi_transport = "d2xx"`, restart, confirm `/status` reaches `ready`
   with all components connected.
4. Drive one real read and one `imaging.capture`; compare against the values in
   `captures/20260823T1804_fullplate_sweep_gen5/`.
5. With the service running, confirm Gen5 can still connect — that is the whole
   point, and the one thing no unit test can establish.

If step 5 holds, RUNBOOK §4/§5 become historical and the `[instrument]` default
should flip.

---

## 7. Scripts

- `scripts/gen5_full_sweep.py` — the full sweep. One experiment per batch,
  procedure verified after being set, raw data drained in a loop,
  `keep_plate_in_after_read()` so the drawer never opens on a loaded plate.
- `scripts/gen5_spectrum.py` — the smaller batched-EndPoint timing harness;
  stops before reading unless `--read` is passed.

Both require Gen5 mode (FTDI vendor driver bound) and the `cytation` service
stopped. See [`RUNBOOK.md`](../RUNBOOK.md) §4/§5 for the driver swap, and note
that on this PC it can be done entirely with `pnputil` — no Zadig, no cable
pull. There is exactly one FTDI device (`USB\VID_0403&PID_6001\23030927`) and
the libusbK package Zadig generated is preserved at
`C:\Users\sdl2\usb_driver\Cytation5.inf`.

---

## See also

- [`RUNBOOK.md`](../RUNBOOK.md) — driver swap procedure
- [`ARAVIS_MIGRATION.md`](ARAVIS_MIGRATION.md) — the *camera* driver, unrelated
  to the reader-link swap above
