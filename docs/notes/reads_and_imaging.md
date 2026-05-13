# Reads, imaging, and dynamic well selection

Lab-authored reference. Distilled from operating experience plus the BioTek
manuals listed in [`../INDEX.md`](../INDEX.md). No vendor text is reproduced
verbatim — for exact specs, consult the relevant PDF in `docs/vendor/`.

---

## 1. The three optical paths in a Cytation 5

The Cytation 5 has **two physically distinct measurement systems** in one
chassis. Knowing which one a request needs determines which backend, which
USB endpoint, and which driver chain handles it.

| System | Physical hardware | What it produces | USB | Driver |
|---|---|---|---|---|
| **Reader** | xenon flash lamp + monochromators / filter wheel + transmission detector + PMT | numeric well values (absorbance A, fluorescence RFU, luminescence RLU) | FTDI USB-serial | FTDI vendor (Gen5) **or** libusbK (PyLabRobot) — mutually exclusive |
| **Imager** | white-light LED + multi-color LED cube(s) + filter cubes + objectives + Point Grey/FLIR Blackfly camera | images (TIFF/PNG per well per channel) | USB3 camera | Spinnaker SDK (shared between Gen5 and PyLabRobot) |

The drawer, plate stage, and incubator are part of the reader subsystem and
move under reader control. The camera shares the same plate stage but is
addressed through the imager subsystem.

## 2. Numeric reads (reader subsystem)

### Absorbance (incl. UV)

- Wavelength range: deep-UV through visible. (Exact range: see Cytation 5
  spec sheet in `docs/vendor/cytation5_specsheet.pdf`.)
- Endpoint read: pick one wavelength → get one A value per well.
- Spectrum read: scan a wavelength range (start / stop / step nm) → get a
  full spectrum per well.
- "UV absorbance" typically means an endpoint at 260 nm (DNA), 280 nm
  (protein), or 230 nm (peptide bond), or a UV scan 200–400 nm.

### Fluorescence

- Excitation wavelength + emission wavelength + focal height + gain.
- Per-well RFU value.
- "Emission" reads in the spectroscopy sense = fluorescence emission
  intensity at a chosen ex/em pair.

### Luminescence

- No external excitation; integration time + gain only.
- Listed for completeness — same backend path as fluorescence.

### What both backends expose

| Capability | PyLabRobot `Cytation5Backend` | North `biotek_driver` |
|---|---|---|
| Absorbance endpoint | direct call: `read_absorbance(plate, wells, wavelength)` | `.prt` template + `set_partial_plate(xml)` |
| Absorbance spectrum | direct call with start/stop/step | `.prt` template per scan range |
| Fluorescence endpoint | direct call with ex/em | `.prt` template |
| Luminescence | direct call | `.prt` template |
| Dynamic per-call wells | yes (argument) | yes (partial-plate XML) |
| Dynamic per-call wavelength | yes (argument) | only if you patch the `.prt` template before loading; otherwise pre-built per wavelength |
| Driver swap on Windows | yes — Zadig + libusbK | no — uses FTDI VCP, coexists with Gen5 |

## 3. Imaging (imager subsystem)

### Channels

A Cytation 5 with the imaging option has a small set of fixed LED + filter-cube
channels, plus brightfield. The exact set depends on which filter cubes are
installed in your unit; see `docs/vendor/cytation5_imaging_user_guide.pdf`
and the objective/filter catalog.

| Channel name | Light source | Typical use |
|---|---|---|
| Brightfield | white LED, transmitted | morphology, cell counting |
| Phase contrast | white LED, transmitted, with phase ring | label-free live cells |
| DAPI / UV | UV LED (~358 nm ex), DAPI emission filter | nuclear stain |
| GFP | blue LED (~469 nm ex), green emission filter | GFP / FITC / Alexa 488 |
| RFP / Texas Red | green LED (~531 nm ex), red emission filter | RFP / mCherry / Alexa 568 |
| Cy5 | red LED (~628 nm ex), far-red emission filter | Cy5 / Alexa 647 |

> "UV" in microscopy usually means the DAPI / 358 nm channel. "Emission" in
> microscopy is ambiguous — it generally refers to capturing on one of the
> fluorescence channels above (each consists of a specific excitation LED +
> a paired emission filter), as opposed to "transmitted" which means
> brightfield/phase.

### Per-channel parameters at capture time

- **Objective** — 4× / 10× / 20× / 40× (whichever is installed, see catalog).
- **Focal height (mm)** — height of focal plane above the well bottom; varies
  by objective and plate. The Cytation has laser autofocus and image-based AF
  modes; many production workflows pre-calibrate per plate type.
- **Exposure (ms)** — channel-dependent. Brightfield is short (~1–10 ms),
  fluorescence is longer (~50–500 ms).
- **Gain (dB)** — camera analog gain; trade SNR for sensitivity.
- **Binning** — 1×1, 2×2, 4×4 to trade resolution for SNR / speed.
- **LED intensity** — per-channel LED brightness, 1–10 typical.

### Driver path

Imaging is **only** exposed by `pylabrobot.plate_reading.agilent.biotek_cytation_backend.CytationBackend`
(which extends `BioTekPlateReaderBackend` for stage control + `ImagerBackend`
for the camera). The `biotek_driver` wrapper from North-Cytation does not
expose imaging in any code we've seen.

This forces a choice for production:

- **If imaging is required** → PyLabRobot backend → Zadig swap on FTDI →
  Gen5 stage/optics control is unavailable while the service is running.
  Camera sharing with Gen5 is fine because the Spinnaker driver is shared.
- **If only numeric reads are required** → `biotek_driver` backend works,
  no Zadig swap, Gen5 keeps full functionality. Simpler operationally.

## 4. The proposed REST API for dynamic per-call control

The orchestrator-facing surface is the same regardless of backend choice.
Wells, wavelengths, channels, objectives, and exposures are all per-call:

```http
POST /control/read.absorbance
{
  "wells": ["A1","A2","B1","B2"],
  "wavelength_nm": 280,
  "read_speed": "normal"
}
→ 200 { "request_id": "...", "results": { "A1": 0.412, "A2": 0.388, ... } }

POST /control/read.fluorescence
{
  "wells": ["C3","C4"],
  "excitation_nm": 485,
  "emission_nm": 528,
  "focal_height_mm": 7.0,
  "gain": "auto"
}

POST /control/read.spectrum
{
  "wells": ["A1"],
  "mode": "absorbance",
  "start_nm": 250,
  "stop_nm": 400,
  "step_nm": 5
}

POST /control/imaging.capture
{
  "wells": ["A1","B1"],
  "channel": "brightfield",
  "objective": "4X_PL",
  "exposure_ms": 8.0,
  "gain_db": 0.0,
  "focal_height_mm": 4.5,
  "autofocus": "image"
}
→ 202 { "request_id": "...", "image_uris": { "A1": "/files/...png", ... } }

POST /control/imaging.capture
{ "wells": ["A1"], "channel": "dapi",
  "exposure_ms": 200, "objective": "10X_PL",
  "focal_height_mm": 6.2 }
```

Backend translation:

| Request | PyLabRobot path | `biotek_driver` path |
|---|---|---|
| `read.absorbance` | `await reader.read_absorbance(plate, wells=..., wavelength=...)` | pick template `abs_<λ>.prt` (or patch one), build partial-plate XML for `wells`, `run_protocol(...)`, parse output |
| `read.fluorescence` | `await reader.read_fluorescence(plate, wells=..., excitation_wavelength=..., emission_wavelength=..., focal_height=...)` | template `fluo_<ex>_<em>.prt` (or patch), partial-plate XML, `run_protocol(...)` |
| `read.spectrum` | `await reader.read_absorbance_spectrum(plate, wells=..., start=..., stop=..., step=...)` | spectrum `.prt`, partial-plate XML |
| `imaging.capture` | `await imager.capture(plate=..., wells=..., mode=..., objective=..., exposure_ms=..., focal_height=..., gain_db=...)` | **not supported** — return 501 |

This is what the read-only `/status` API graduates to in Phase 3 (with the
v1.1 STATUS_SPEC claim/heartbeat/release wrapping each control call).

## 5. Practical "where do I start" matrix

| Goal today | Backend | Driver work | Notes |
|---|---|---|---|
| UV absorbance reads | either | `biotek_driver`: zero. PyLabRobot: Zadig. | If Gen5 must coexist, prefer `biotek_driver`. |
| Fluorescence reads | either | same | same |
| Brightfield imaging | PyLabRobot only | Zadig + Spinnaker SDK install | Spinnaker doesn't conflict with Gen5; FTDI does. |
| DAPI / GFP / RFP imaging | PyLabRobot only | Zadig + Spinnaker SDK install + correct filter cube physically present | Confirm cube installed in your unit. |
| Mix of imaging + Gen5-driven reads on same PC | not currently feasible without driver swap | RUNBOOK §3/§4 manual swap | Or: split into two PCs, one Gen5, one PyLabRobot. |

## 6. Open questions

- Whether `biotek_driver` is redistributable (its source/license is unknown,
  and it isn't on PyPI). If so we could ship a `biotek_driver` extra
  alongside the existing `plr` extra in `pyproject.toml`.
- Whether PyLabRobot's `imager.capture` accepts a sequence of wells in one
  call or needs a loop. Verify against pylabrobot HEAD when implementing.
- Whether autofocus state can be cached across wells in one capture batch
  (image-based AF is slow per well; laser AF is fast).
