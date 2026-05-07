# BioTek (Agilent) Cytation 5 — Python driver + REST API

PyLabRobot-backed Python driver and REST API service for the **BioTek (Agilent) Cytation 5 Multi-Mode Reader**, communicating over USB. Driver layer is `pylabrobot.plate_reading.PlateReader` + `pylabrobot.plate_reading.biotek.Cytation5Backend`. The service exposes the unified lab equipment status spec so the AC Organic Self-Driving Lab dashboard can poll it like any other device.

> **API conformance:** This repo conforms to **lab status spec v1.0 (read-only)** — see `docs/STATUS_SPEC.md` in the [`ac-organic-lab`](https://github.com/cyrilcaoyang/ac-organic-lab) monorepo. There is no `/control/*` claim/heartbeat/release surface yet; specific unit operations (`read.absorbance`, `read.fluorescence`, `read.luminescence`, `imaging.capture`, `plate.load`, `plate.unload`, `drawer.open`, `drawer.close`) graduate to `/control/*` and v1.1 in a follow-up release. Until then, the SDK in `ac-organic-lab` keeps `do_not_call_connect: true` for this device.

## Roadmap

| Phase | Output | Status |
|---|---|---|
| **0+1** | STATUS_SPEC v1.0 read-only API on the Cytation PC; `equipment.yaml` flips from `mock` to `http`. | this PR |
| **2** | Per-well sample tracking via PyLabRobot `Container`/`Plate`/`Well` (volume tracking + orchestrator-assigned `sample_id`). Surfaced under `details.loaded_plate`. | follow-up |
| **3** | STATUS_SPEC v1.1: `POST /control/claim`, `/heartbeat`, `/release`, `allowed_actions`, full `/control/*` write surface (drawer, reads, plate load/unload, imaging capture, incubator). | follow-up |
| **4** | `lab_skills/skill_catalog/plate_reader.py` registered in the monorepo so workflows can `await session.role("plate_reader").read_absorbance(...)`. | follow-up |

## Prerequisites

- **Windows 10 / 11** lab PC (PyLabRobot's USB transport binds via Zadig + libusbK).
- **Python 3.10+**.
- **PyLabRobot** with the USB extras (installed via `--extra plr --extra windows`).
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
C:\SDL_Tools\uv.exe run --extra api agilent-cytation-serve --port 9333 --dry-run

# Production sync (pylabrobot + pyusb + libusb-package + fastapi)
C:\SDL_Tools\uv.exe sync --extra api --extra plr --extra windows
```

For **production deployment** (NSSM-wrapped Windows Service that auto-starts on boot, logs to `C:\SDL_Logs\cytation.{out,err}.log`), follow the canonical recipe in [`ac-organic-lab/docs/DEVICE_PC_SETUP.md`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/docs/DEVICE_PC_SETUP.md). The Cytation 5 PC also runs the xArm service on port 8000; this Cytation service uses port **9333** to avoid conflicts.

## USB driver setup

PyLabRobot's USB backends speak through `pyusb`/`libusb`. On Windows you must rebind the Cytation USB endpoint from the vendor driver to **libusbK** the first time:

1. Install [Zadig](https://zadig.akeo.ie/).
2. Plug the Cytation in and power it on.
3. Open Zadig **as Administrator** → menu **Options** → **List All Devices**.
4. Find the Cytation in the dropdown.
5. Pick **libusbK (vX.X.X.X)** as the target driver and click **Replace Driver**.

After that, `python -c "from pylabrobot.plate_reading.biotek import Cytation5Backend"` should import without error and the service can connect.

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
| `[service].host` / `[service].port` | bind address and port (default `0.0.0.0:9333`) |
| `[service].dry_run` | force stub at startup (use `true` for development) |
| `[service].cors_origins` | CORS whitelist; `["*"]` is fine on Tailnet |
| `[service].startup_connect_timeout_s` | give-up timeout for the lifespan auto-connect |
| `[dashboard].equipment_id` | **must equal `cytation_5`** (matches `equipment.yaml` in `ac-organic-lab`) |

## REST API

Spec-mandated endpoints (always available):

| Method | Path | Returns |
|---|---|---|
| GET | `/` | `{equipment_id, equipment_name, protocol_version}` |
| GET | `/health` | `{status: "healthy"}` |
| GET | `/status` | full `EquipmentStatus` envelope (always 200 unless the process is broken) |
| GET | `/openapi.json` | OpenAPI document (FastAPI auto-generates) |

### Quick check

```bash
curl http://sdl2-pc-03-cytation:9333/
curl http://sdl2-pc-03-cytation:9333/health
curl http://sdl2-pc-03-cytation:9333/status | jq
```

### Spec conformance notes

- `GET /status` is **side-effect-free** — polling it never moves the drawer, never triggers a measurement, never re-initialises the reader. Polling at 2-3 s is the dashboard default.
- `GET /status` always returns **HTTP 200** when the process is alive. Hardware-not-yet-initialised is reported as `equipment_status: requires_init` with `required_actions: ["startup"]` (the eventual `POST /control/startup` lands in v1.1).
- `equipment_id` matches the `id` in the dashboard's `equipment.yaml` (`cytation_5`). Do not change it without coordinating with the dashboard repo.
- No `equipment_ip` / `equipment_tailscale` self-discovery — the dashboard registry is the single source of truth for "where to reach this device".
- `models.py` is a verbatim copy of the spec from `ac-organic-lab/docs/STATUS_SPEC.md` and will eventually be replaced by `from lab_status_contract import ...`.

Reference snapshots live in `tests/fixtures/status_*.json` covering `requires_init`, `ready`, `busy`, and `dry_run`. They are regenerated by `pytest` and committed so reviewers can eyeball schema changes.

## Project structure

```
agilent-cytation-server/
├── README.md
├── LICENSE
├── pyproject.toml
├── config.example.toml          # template — copy to config.toml
├── config.toml                  # local settings (gitignored)
├── state.json                   # PyLabRobot Deck snapshot (Phase 2; gitignored)
├── src/agilent_cytation_server/
│   ├── __init__.py
│   ├── __main__.py              # CLI: `python -m agilent_cytation_server`
│   ├── config.py                # config.toml loader
│   ├── models.py                # lab status spec v1.0 Pydantic models
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

Once the service is up on the Cytation PC, register it in [`ac-organic-lab/equipment.yaml`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/equipment.yaml) (one-line PR). The slot already exists with `adapter: mock`; flip it to:

```yaml
- id: cytation_5
  name: BioTek Cytation 5
  platform: hte
  kind: plate_reader
  adapter: http
  protocol: "1.0"
  base_url: http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:9333
  status_path: /status
  poll_timeout_seconds: 5.0
  do_not_call_connect: true
  tile: { w: 2, h: 2 }
```

Then `sudo systemctl restart ac-dashboard-api.service` per `docs/EQUIPMENT_INTEGRATION.md` §1.C in the monorepo.

## Legal / Licensing

- **Intended use**: This package is provided for **research and internal evaluation only**. For any **commercial** or regulated use, contact **Agilent Technologies** for appropriate licenses.
- **PyLabRobot**: this driver is an integration shim around PyLabRobot's open-source `Cytation5Backend`. Refer to the PyLabRobot project for its own licensing and contribution guidelines.
- **No affiliation**: independent, unofficial integration helper; not affiliated with, endorsed by, or supported by Agilent Technologies or BioTek.
- **No warranty / misuse**: provided **"as is", without warranty of any kind**. You are solely responsible for safe operation of equipment and compliance with all applicable laws, regulations, and vendor licenses.

MIT licensed — see `LICENSE`.
