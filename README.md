# BioTek (Agilent) Cytation 5 — Python driver + REST API

PyLabRobot-backed Python driver and REST API service for the **BioTek (Agilent) Cytation 5 Multi-Mode Reader**, communicating over USB. Driver layer is `pylabrobot.plate_reading.PlateReader` + `pylabrobot.plate_reading.agilent.biotek_cytation_backend.CytationBackend` (not the deprecated `Cytation5Backend` alias). The service exposes the unified lab equipment status spec so the AC Organic Self-Driving Lab dashboard can poll it like any other device.

> **API conformance:** This repo conforms to **lab status spec v1.2** — see `docs/STATUS_SPEC.md` in the [`ac-organic-lab`](https://github.com/cyrilcaoyang/ac-organic-lab) monorepo. The wire types come from the shared `sdl-lab-contract` package. The full `/control/*` write surface is wired (drawer, reads, plate load/unload, imaging capture, claim/heartbeat/release), and `/status` reports v1.2 `activity` observed from the instrument — see [Activity and utilization](#activity-and-utilization-v12).
>
> **Hardware verification status (2026-08-12).** `imaging.capture` is
> **verified end-to-end on the real instrument** through the REST surface
> (claim → plate.load → brightfield capture of A1 → 2448×2048 PNG →
> `cycles_total` incremented). The **reads are not yet verified**: the call
> now reaches the instrument with correct arguments, but the driver's
> acknowledgement assertion fails with no plate physically present, so
> confirming absorbance / fluorescence needs a plate in the reader and
> someone at the bench. The test plan for that session — what to bring, what
> to run, and what to record — is [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

### Activity and utilization (v1.2)

`equipment_status` answers *is this reader healthy and fit to run*; `activity` answers *is it working right now*. They are independent, and each is derived from its own observation — `activity` comes from the in-flight-operation flag the control methods set, never from `equipment_status` (which §2.3 forbids, since it would add no information).

**Primary operation** is a **measurement** (absorbance / fluorescence / luminescence) or an **image capture**. A drawer move and **shaking** also report `activity: "running"` — the instrument is executing a commanded operation and cannot start a read until it finishes — but neither is counted in `cycles_total`, which counts measurements and captures only.

Shaking is observed from the driver's own flag rather than from a span the
service opens, because the shake command returns as soon as motion starts and a
background task keeps the plate moving: bracketing the request would report
milliseconds of `running` for minutes of motion. Holding a temperature setpoint
deliberately does **not** count as activity — that is a maintained condition,
not an operation in progress, and `components.incubator` is where it shows up.

| Situation | `equipment_status` | `activity` | `cycles_total` |
|---|---|---|---|
| Driver not connected | `requires_init` | `idle` | — |
| Idle, connected | `ready` | `idle` | unchanged |
| Measurement or capture in flight | `busy` | `running` | +1 on success |
| Drawer moving | `busy` | `running` | unchanged |
| Readback failing (e.g. temperature sensor) | `degraded` | observed (`idle` or `running`) | unchanged |
| Error inside the recent-error window | `error` | observed | unchanged |

`metrics["cycles_total"]` is the spec's reserved counter (§2.3.1). It matters because a read finishes well inside the dashboard's 60 s poll: a sampled `activity` series does not undercount those reads, it misses them entirely. The poll-to-poll delta of this counter is the accountable number. It resets on service restart, by contract. The repo's original `read_count` metric is kept alongside it and stays measurement-only.

`activity_since` is the instant the current span began, so a reader can recover an in-progress read's true elapsed time. While `activity == "running"`, `allowed_actions` advertises only the claim verbs plus `shake.stop` — nothing that would start a second concurrent operation, but never without a way to stop the one in progress (§2.3).

**Why `/status` does not take the reader lock.** Every operation holds an `asyncio.Lock` for its full duration. When `/status` shared that lock, a poll issued during a read returned only *after* the read completed, with the busy flag already cleared — so `busy` and `activity: "running"` were unobservable from outside, defeating the point of the field. `/status` now composes its envelope from in-memory state plus a short-TTL readback cache, and will wait at most 50 ms for the lock to refresh that cache. `details.readback_age_s` reports how stale the cached instrument reading is.

## Roadmap

| Phase | Output | Status |
|---|---|---|
| **0+1** | STATUS_SPEC v1.0 read-only API on the Cytation PC; `equipment.yaml` flips from `mock` to `http`. | ✅ shipped |
| **2** | Per-well sample tracking via persistent `PlateStateStore`; surfaced under `details.loaded_plate`. See [`docs/PLATE_STATE.md`](docs/PLATE_STATE.md) for the cross-device strategy. | ✅ shipped |
| **3** | STATUS_SPEC v1.1: `POST /control/claim`, `/heartbeat`, `/release`, `allowed_actions`, full `/control/*` write surface (drawer, reads, plate load/unload, imaging capture). | ✅ shipped — imaging verified on hardware; reads still pending ([`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)) |
| **4** | `lab_skills/skill_catalog/plate_reader.py` registered in the monorepo so workflows can `await session.role("plate_reader").read_absorbance(...)`. | patch ready in [`docs/LABSKILLS.md`](docs/LABSKILLS.md); apply from the central server |
| **5** | STATUS_SPEC v1.2: `sdl-lab-contract` types, `activity` / `activity_since` observed from the instrument, reserved `cycles_total`, and a `/status` path that stays answerable mid-operation. | ✅ shipped |

## Prerequisites

- **Windows 10 / 11** lab PC (PyLabRobot's USB transport binds via Zadig + libusbK).
- **Python 3.10+**.
- **PyLabRobot** with the USB extras (`--extra plr --extra windows`), plus `--extra imaging` and the PySpin wheel for the microscopy path.
- **Cytation 5** powered on, USB cable connected.
- **Zadig** has replaced the Cytation USB endpoint's driver with **libusbK** (one-time per PC; see [USB driver setup](#usb-driver-setup) below).

## Installation

[uv](https://docs.astral.sh/uv/) is the canonical environment manager for this repo and the rest of the [`ac-organic-lab`](https://github.com/cyrilcaoyang/ac-organic-lab) stack — see `docs/DEVICE_PC_SETUP.md` in that monorepo for the canonical install recipe (uv at `C:\SDL_Tools\uv.exe`, NSSM service wrapping, log paths). The short version:

```powershell
# Clone and configure
cd C:\Users\sdl2\Projects
git clone https://github.com/cyrilcaoyang/agilent-cytation-server.git
cd C:\Users\sdl2\Projects\agilent-cytation-server
Copy-Item config.example.toml config.toml
notepad config.toml      # set [service].port, [imaging].enabled, USB serial, plate dimensions

# Sync dev deps (no pylabrobot / pyusb — uses the in-memory stub for tests)
C:\SDL_Tools\uv.exe sync --extra dev

# Run tests in dry_run; no hardware needed
C:\SDL_Tools\uv.exe run pytest -q

# Run the service in the foreground for a quick smoke check
# (9333 on purpose: the deployed NSSM service already holds 8040)
C:\SDL_Tools\uv.exe run --extra api agilent-cytation-serve --port 9333 --dry-run

# Production sync (pylabrobot + pyusb + libusb-package + fastapi + numpy/pillow)
C:\SDL_Tools\uv.exe sync --extra api --extra plr --extra windows --extra imaging
# PySpin is NOT in the lockfile (FLIR does not publish to PyPI) - install the
# wheel separately and note the service runs `uv run --no-sync` so it is never
# pruned. See RUNBOOK.md §3.1.
```

For **production deployment** (NSSM-wrapped Windows Service that auto-starts on boot, logs to `C:\SDL_Logs\cytation.{out,err}.log`), follow the canonical recipe in [`ac-organic-lab/docs/DEVICE_PC_SETUP.md`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/docs/DEVICE_PC_SETUP.md). The code's default port is 9333, but the deployed instance runs on port **8040** (set in `config.toml` — the Cytation PC hosts several services; see DEVICE_PC_SETUP §7 for the port map). Do **not** give the service a `DependOnService` on Tailscale or anything else — see `RUNBOOK.md` §6 for the 2026-08-10 outage that rule comes from.

For **day-to-day operations on the lab PC** (driver swaps for Gen5 ↔ PyLabRobot, log tailing, restart, update from `git pull`), see [`RUNBOOK.md`](./RUNBOOK.md).

## USB driver setup

The Cytation 5 has **two** USB connections to the host PC:

- **Reader (drawer / optics / incubator / shaker)** — driven by an internal FTDI USB-serial chip. Bound to FTDI's vendor driver out of the box (which is what Gen5 uses). To let PyLabRobot drive the reader, this chip must be rebound to **libusbK** via [Zadig](https://zadig.akeo.ie/) — but doing so prevents Gen5 from seeing the device until you swap back. **The two stacks cannot both have the FTDI chip at the same time.**
- **Microscopy camera (Cytation 5 imaging module)** — Point Grey / FLIR Blackfly under the **Spinnaker SDK**. Same driver Gen5 already uses. **No swap needed.**

If your lab does not use Gen5 on this PC, run Zadig once and forget about it. If your lab still uses Gen5, run [`RUNBOOK.md`](./RUNBOOK.md) procedure §3 / §4 to toggle between modes — the cytation REST service stays installed in both, falling back to `dry_run` while Gen5 has the FTDI chip so the dashboard tile never goes orphan.

If multiple Cytations are on the same PC, set `[instrument].usb_serial = "..."` in `config.toml` to lock onto a specific one.

## Configuration

All instrument-specific settings live in `config.toml` (gitignored). Copy `config.example.toml` and edit:

| Section / key | Purpose |
|---|---|
| `[instrument].backend` | `"cytation5"` (real driver) or `"dry_run"` (stub) |
| `[instrument].usb_serial` | optional — pin to a specific device |
| `[imaging].enabled` | `true` for Cytation 5 with the microscopy module |
| `[plates].default_model` | `custom_96` or `agilent_shallow_96` |
| `[plates.custom_96]` / `[plates.agilent_shallow_96]` | plate geometry (mm) and well max volume (µL) |
| `[service].host` / `[service].port` | bind address and port (code default `0.0.0.0:9333`; the deployed PC sets `8040` in `config.toml`) |
| `[service].dry_run` | force stub at startup (use `true` for development) |
| `[service].cors_origins` | CORS whitelist; `["*"]` is fine on Tailnet |
| `[service].startup_connect_timeout_s` | give-up timeout for the lifespan auto-connect |
| `[dashboard].equipment_id` | **must equal `cytation_5`** (matches `equipment.yaml` in `ac-organic-lab`) |

## REST API

Spec-mandated read endpoints (always available):

| Method | Path | Returns |
|---|---|---|
| GET | `/` | `{equipment_id, equipment_name, protocol_version}` |
| GET | `/health` | `{status: "healthy"}` |
| GET | `/status` | full `EquipmentStatus` envelope (always 200 unless the process is broken) |
| GET | `/openapi.json` | OpenAPI document (FastAPI auto-generates) |

v1.1 claim protocol:

| Method | Path | Body / headers | Returns |
|---|---|---|---|
| POST | `/control/claim` | `{owner, session_id, ttl_s?}` | `{claim_token, heartbeat_interval_s, expires_at}` |
| POST | `/control/heartbeat` | `X-Claim-Token` | 204 (extends TTL) |
| POST | `/control/release` | `X-Claim-Token` | 204 (idempotent) |

v1.1 control verbs (all require `X-Claim-Token` when `[service].enforce_claims = true`; otherwise advisory):

| Method | Path | Body |
|---|---|---|
| POST | `/control/startup` | — |
| POST | `/control/shutdown` | — |
| POST | `/control/drawer/open` | `{}` |
| POST | `/control/drawer/close` | `{}` |
| POST | `/control/plate/load` | `{plate_id, model?, wells?}` |
| POST | `/control/plate/unload` | — |
| POST | `/control/well/update` | `{well, sample_id?, volume_ul?, notes?, clear_sample_id?, clear_notes?}` |
| POST | `/control/read/absorbance` | `{wells, wavelength_nm}` — 230–999 nm |
| POST | `/control/read/fluorescence` | `{wells, excitation_nm, emission_nm, focal_height_mm?}` — ex/em 250–700 nm |
| POST | `/control/read/luminescence` | `{wells, focal_height_mm?, integration_time_s?}` |
| POST | `/control/imaging/capture` | `{well, channel, objective?, focal_height_mm?, exposure_ms?, gain?, led_intensity?, autofocus?, auto_exposure?}` |
| POST | `/control/incubator/set_temperature` | `{celsius}` — 4–45 °C |
| POST | `/control/incubator/stop` | — ends temperature control |
| POST | `/control/shake/start` | `{pattern?, displacement_mm?}` |
| POST | `/control/shake/stop` | — |

Bounds mirror what PyLabRobot's BioTek backend enforces itself, so an
out-of-range request is a 422 naming the field rather than a 500 from inside
the driver. `focal_height_mm` is 4.5–13.88 on all three reads.

**The reads take no gain parameter, and passing one is a 422.** PyLabRobot's
Cytation backend exposes no gain control on any read, and silently dropping
the field would return a plausible number measured at some *other* gain — a
wrong result that looks right. (`imaging.capture`'s `gain` is unrelated: it is
the Spinnaker camera's analog gain in dB.)

### Preconditions

Both reads and captures are gated, and per STATUS_SPEC §6.2 the gates are
mirrored in `allowed_actions` — an action that would be refused is never
advertised:

| Precondition | Refusal | Applies to |
|---|---|---|
| A plate must be loaded (`POST /control/plate/load`) | 412 `plate_not_loaded` | all reads + capture |
| The carrier must be in (`POST /control/drawer/close`) | 412 `drawer_open` | all reads + capture |
| The camera must have initialised | 412 `camera_not_ready` | capture |
| A fluorescence channel's filter cube must be fitted | 422 naming the fitted cubes | capture |

The plate requirement is not bookkeeping: PyLabRobot addresses wells through
the `Plate` resource assigned to the `PlateReader`, and raises `NoPlateError`
without one. Loading a plate assigns that resource as well as recording
sample metadata.

The drawer gate **blocks only on a carrier this service knows is out** — one
it opened and did not close. There is no position query anywhere in the
driver's command set, so the tracked state is dead reckoning and an
`unknown` drawer is allowed through deliberately: a stale `in` costs the same
driver assertion as having no interlock at all, while a stale `out` would
refuse every read on a correctly loaded instrument with no way for the
operator to override it. It therefore does **not** catch someone pressing the
front-panel eject button; nothing in software can, and nothing in software
can disable that button either (see `HANDOFF.md`).

### `last_error.code` taxonomy

Best practice #6 asks each repo to publish a stable set of codes so clients
branch on `code` rather than string-matching `message`. The set lives in one
place — `LAST_ERROR_CODES` in `service.py` — and `_record_error` warns when
something outside it is recorded.

| Code | Means |
|---|---|
| `startup` | Connecting to the instrument failed (USB enumeration, D2XX handle held by Gen5, no reply). |
| `drawer.open` / `drawer.close` | The carrier move failed. |
| `read.absorbance` / `read.fluorescence` / `read.luminescence` | The measurement failed mid-execution. |
| `incubator.set_temperature` / `incubator.stop` | The setpoint command failed. |
| `shake.start` / `shake.stop` | The shake command failed. |
| `imaging.capture` | The capture failed (camera, focus, or write). |
| `link_desync` | A shake abort left the serial link answering the *previous* command's reply, and the resync probes could not recover it. Reconnect (`shutdown` then `startup`). The only code naming a condition rather than the action that failed — `shake.stop` itself succeeded. |

Every other code is the name of the action that failed, so the set stays
derivable from the `/control/*` surface instead of being separately invented.

**Precondition codes are not in this taxonomy.** `drawer_open`,
`plate_not_loaded` and `camera_not_ready` ride the 412 body's `precondition`
field and must never reach `last_error` at all: a healthy device declining an
inapplicable request is not a failure (§6.3).

`last_error` clears on the first 2xx from any operational `/control/*` action
(§6.4), so it reads "the most recent failure since the last successful
action", not "since process start". Refusals do not clear it — a 412 is no
evidence that whatever broke earlier has been fixed — and neither `/status`
nor the claim verbs do.

### What this instrument can actually do

Read from `details.imaging` on `/status` rather than assuming — the fit-out is
queried from the instrument's own configuration. As of 2026-08-12 on
`sdl2-pc-03-cytation`:

| Capability | State | Needs |
|---|---|---|
| Absorbance (UV-Vis), 230–999 nm | ✅ available | nothing |
| Fluorescence intensity reads, ex/em 250–700 nm | ✅ available | nothing |
| Luminescence | ✅ available | nothing |
| Brightfield imaging (bottom-up) | ✅ **verified live** 2026-08-12 | nothing |
| Autofocus / auto-exposure on capture | ✅ available | nothing |
| Incubation, 4–45 °C | ✅ **verified live** 2026-08-12 | nothing |
| Shaking (linear / orbital) | ✅ **verified live** 2026-08-12 | nothing — but see the ceiling below |
| Phase-contrast imaging | ✅ driver permits it (firmware 2.09) | phase annulus in the condenser; not yet imaged |
| Fluorescence imaging (DAPI/GFP/RFP/Cy5…) | ❌ **blocked** | ≥1 filter cube — the wheel reports **4 slots, all empty** |
| Absorbance/fluorescence **spectral scans** | ❌ not available | upstream work; PyLabRobot has no spectrum method |
| Fluorescence polarization | ❌ not available | no FP method on the BioTek backend |
| Polarized-light imaging | ❌ not available | no polarizer in the driver's command set |

Objective turret: 6 slots, 3 fitted — `O_4X_PL_FL_Phase`, `O_20X_PL_FL_Phase`,
`O_40X_PL_FL_Phase`. Camera: FLIR Chameleon3 `CM3-U3-50S5M`, 2448×2048 mono
(so `color_brightfield` is not usable on this unit). Firmware 2.09, instrument
serial 23030927 — the firmware revision matters because the driver refuses
phase contrast on Cytation1, which it identifies by a version starting with
"1"; `details.imaging.phase_contrast_available` reports the verdict.

### Shaking: know the ceiling before you rely on it

The instrument takes shake duration as a parameter with a **16-minute
maximum**, so PyLabRobot fakes continuous shaking with a background task that
re-issues the command each time it lapses — and warns in its own docstring
that the door may briefly open at each boundary. Good for a mix before a read;
not something to leave running unattended.

Two consequences the service enforces:

- **Reads and captures are withheld while shaking.** Partly because reading a
  moving plate is meaningless, but mainly because `send_command` has no
  internal lock: the shake task talks to the instrument on its own schedule,
  so a concurrent read would interleave writes on the serial link and corrupt
  both exchanges. `/status` also stops refreshing its temperature readback
  while shaking, for the same reason — `details.readback_age_s` shows the
  cache going stale.
- **`shake.stop` stays in `allowed_actions` while shaking**, per §2.3. Motion
  outlives the request that started it, so without that the only documented
  way to stop the plate would be `shutdown`.

`displacement_mm` (1–6) is PyLabRobot's `frequency` argument renamed, because
it is not a frequency: it is orbit displacement in mm and runs *inversely* to
speed — 6 mm ≈ 360 CPM, 1 mm ≈ 1096 CPM.

### Autofocus and auto-exposure

`autofocus` runs a golden-section search on focal height maximising PyLabRobot's
Sobel-gradient sharpness metric; `auto_exposure` binary-searches exposure until
the peak pixel sits near 80 % of full scale. Both cost extra exposures on the
sample — a photobleaching budget as much as a time one — so each is capped at 8
rounds. The response echoes the **resolved** `focal_height_mm` and
`exposure_ms`, with the search detail under `details.tuning`.

Auto-exposure deliberately uses PyLabRobot's `max_pixel_at_fraction` rather than
its `fraction_overexposed`: the latter counts pixels strictly greater than 255
in a uint8 array, which is never satisfiable, so it drives exposure upward until
the frame clips.

### Quick check

```bash
curl http://sdl2-pc-03-cytation:8040/
curl http://sdl2-pc-03-cytation:8040/health
curl http://sdl2-pc-03-cytation:8040/status | jq
```

### Spec conformance notes

- `GET /status` is **side-effect-free** — polling it never moves the drawer, never triggers a measurement, never re-initialises the reader. Polling at 2-3 s is the dashboard default.
- `GET /status` always returns **HTTP 200** when the process is alive. Hardware-not-yet-initialised is reported as `equipment_status: requires_init` with `required_actions: ["startup"]`, cleared by `POST /control/startup`.
- `equipment_id` matches the `id` in the dashboard's `equipment.yaml` (`cytation_5`). Do not change it without coordinating with the dashboard repo.
- No `equipment_ip` / `equipment_tailscale` self-discovery — the dashboard registry is the single source of truth for "where to reach this device".
- `models.py` no longer vendors the contract: the wire types are imported from the shared [`sdl-lab-contract`](https://github.com/AccelerationConsortium/sdl-lab-contract) package (pinned to the tag whose major.minor matches the spec revision) and re-exported, so every `from .models import ...` in this repo keeps working.

Reference snapshots live in `tests/fixtures/status_*.json` covering `requires_init`, `ready`, `busy`, and `dry_run`. They are regenerated by `pytest` and committed so reviewers can eyeball schema changes.

## Where to read about the optics / dynamic well selection

The imperative control surface (per-call wells, per-call wavelength, per-call imaging channel) is live — see [REST API](#rest-api) above. Design notes for it — including how brightfield, UV/DAPI imaging, fluorescence emission, and absorbance reads map onto `POST /control/read.*` and `POST /control/imaging.capture` — live in [`docs/notes/reads_and_imaging.md`](./docs/notes/reads_and_imaging.md). The catalogue of vendor manuals you should download into `docs/vendor/` (gitignored) is in [`docs/INDEX.md`](./docs/INDEX.md).

## Project structure

```
agilent-cytation-server/
├── README.md
├── RUNBOOK.md                   # day-to-day ops on the lab PC (driver swaps, restart, update)
├── LICENSE
├── pyproject.toml
├── config.example.toml          # template — copy to config.toml
├── config.toml                  # local settings (gitignored)
├── state.json                   # PyLabRobot Deck snapshot (Phase 2; gitignored)
├── docs/
│   ├── README.md                # how the docs/ folder is organised
│   ├── INDEX.md                 # catalogue of BioTek/Agilent manuals (which doc covers what)
│   ├── notes/
│   │   └── reads_and_imaging.md # lab-authored reference: optics, channels, dynamic-well API
│   ├── vendor/                  # BioTek/Agilent PDFs — gitignored, lab PC only
│   └── protocols/               # Gen5 .prt protocol files — gitignored by default
├── src/agilent_cytation_server/
│   ├── __init__.py
│   ├── __main__.py              # CLI: `python -m agilent_cytation_server`
│   ├── config.py                # config.toml loader
│   ├── models.py                # re-exports the sdl-lab-contract wire types
│   ├── errors.py                # typed preconditions -> HTTP 412 bodies
│   ├── control_args.py          # /control/* request + response models
│   ├── claims.py                # v1.1 cooperative claim manager
│   ├── plate_state.py           # per-well sample tracking (state.json)
│   ├── plates.py                # custom_96 + agilent_shallow_96 factories
│   ├── reader.py                # CytationReader (real) + StubCytationReader
│   └── service.py               # state machine + asyncio.Lock + dry-run path
│   └── api.py                   # FastAPI app (spec endpoints)
└── tests/
    ├── conftest.py              # TestClient fixtures
    ├── test_api.py              # spec conformance + fixture writer
    ├── test_plates.py           # custom_96 / agilent_shallow_96 geometry
    ├── test_service.py          # state-machine transitions
    └── fixtures/
        ├── status_dry_run.json
        ├── status_requires_init.json
        ├── status_ready.json
        └── status_busy.json
```

## Equipment registry entry

The service is registered in [`ac-organic-lab/equipment.yaml`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/equipment.yaml); the live entry (registry schema v2 — `tiles:` keyed by section, no `platform:` field) is:

```yaml
- id: cytation_5
  name: BioTek Cytation 5
  kind: plate_reader
  adapter: http
  protocol: "1.2"
  base_url: http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8040
  tailscale_ip: 100.64.254.16
  status_path: /status
  poll_timeout_seconds: 3.0
  tiles:
    hte: { w: 2, h: 1 }
```

After a registry change, restart the dashboard API per `docs/EQUIPMENT_INTEGRATION.md` §1.C in the monorepo (equipment.yaml loads at startup).

## Legal / Licensing

- **Intended use**: This package is provided for **research and internal evaluation only**. For any **commercial** or regulated use, contact **Agilent Technologies** for appropriate licenses.
- **PyLabRobot**: this driver is an integration shim around PyLabRobot's open-source `Cytation5Backend`. Refer to the PyLabRobot project for its own licensing and contribution guidelines.
- **No affiliation**: independent, unofficial integration helper; not affiliated with, endorsed by, or supported by Agilent Technologies or BioTek.
- **No warranty / misuse**: provided **"as is", without warranty of any kind**. You are solely responsible for safe operation of equipment and compliance with all applicable laws, regulations, and vendor licenses.

MIT licensed — see `LICENSE`.
