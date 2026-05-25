# Plate state — what's saved, where, and how to track a plate across devices

This document describes:

1. **What this service stores** about the currently-loaded plate.
2. **Where and how** it's persisted on disk.
3. **The recommended cross-device strategy** for tracking one plate as
   it moves through dosing → liquid handling → reading → sealing.

It is the authoritative reference for any workflow / orchestrator code
that wants to round-trip per-well state through the Cytation.

---

## 1. What the device stores

The Cytation service tracks one currently-loaded plate at a time
(one stage = one plate). Two Pydantic shapes
(`src/agilent_cytation_server/models.py`):

```python
class WellSample(BaseModel):
    well: str                  # "A1" .. "H12"
    sample_id: str | None      # orchestrator-assigned identifier
    volume_ul: float | None    # >= 0; mutated by dispense / read ops
    notes: str | None          # free-form, e.g. JSON-encoded richer state

class LoadedPlate(BaseModel):
    plate_id: str              # orchestrator-assigned (typically a barcode)
    model: str                 # "custom_96" | "agilent_shallow_96" (config.toml)
    loaded_at: datetime        # UTC timestamp when /control/plate/load fired
    wells: list[WellSample]    # always 96 entries, in row-major order
```

`LoadedPlate` is surfaced under `EquipmentStatus.details.loaded_plate`
in `GET /status`, so the dashboard and any SDK poller see it without
a side call.

### Ownership rules

The fields look uniform but their authority is split:

| Field             | Authority    | Mutated by                                       |
|-------------------|--------------|--------------------------------------------------|
| `plate_id`        | orchestrator | only on `plate.load`; **never** changed in place |
| `model`           | orchestrator | only on `plate.load`                             |
| `loaded_at`       | device       | set by the service when `plate.load` fires       |
| `wells[].well`    | constant     | derived from the 96-well grid                    |
| `wells[].sample_id` | orchestrator | `plate.load` (initial) or `well.update`        |
| `wells[].volume_ul` | **device**   | `well.update` from workflow; reads do not move it |
| `wells[].notes`   | orchestrator | `well.update`                                    |

The split matters: the device is the truth for "what's physically in
the well *right now*" — if a workflow crashes mid-dispense, the
device's persisted state reflects what actually happened, not what
the workflow intended. Orchestrator-owned fields (`sample_id`, `notes`)
the device just stores verbatim.

> **Today, reads do NOT mutate `volume_ul`.** Absorbance / fluorescence /
> luminescence are non-destructive at the volumes we use, so we don't
> charge them. If a future read modality consumes sample (e.g. an
> aspirated read), the device's `service.read_*` methods should call
> `update_well` themselves before returning.

---

## 2. How persistence works

### File location

Configurable via `[plates].state_path` in `config.toml`
(default: `./state.json` relative to the project root). The
`PlateStateStore` resolves relative paths to the project root, not
the current working directory, so a Windows-service restart in a
different CWD still finds the same file.

In production on `sdl2-pc-03-cytation`:
`C:\Users\sdl2\Projects\agilent-cytation-server\state.json`
(gitignored).

### Write semantics

Every mutation that lands inside the service —
`load_plate`, `unload_plate`, `update_well` — calls
`PlateStateStore._persist_locked()`, which:

1. Serialises the current state to JSON (`indent=2`, `sort_keys=True`
   for diffability).
2. Writes to `state.json.tmp` in the same directory.
3. Renames `state.json.tmp` → `state.json` (atomic on Windows + POSIX).

`os.replace` is atomic, so an interrupted write **cannot** leave a
half-written `state.json`. A reader that opens the file mid-rename
either sees the old version or the new version, never garbage.

### Read semantics

`PlateStateStore.__init__` reads `state.json` once at service start.
After that, all reads are served from the in-memory `self._plate`
copy under `threading.Lock`. The file is **not** re-read on each
`/status` poll — there's no inotify, and changes made out-of-band
(editing `state.json` while the service is running) are ignored
until the next restart.

### What survives a restart

Everything in `state.json`. After `nssm restart cytation`:

- `details.loaded_plate.plate_id` — preserved.
- `details.loaded_plate.wells[].sample_id` — preserved.
- `details.loaded_plate.wells[].volume_ul` — preserved.
- `details.loaded_plate.wells[].notes` — preserved.
- The drawer state (`details.drawer`), `read_count`, `last_read_at`
  — **lost**, recomputed from scratch. The device does not assume
  a plate is physically present just because `state.json` says so;
  if the operator pulled the plate while the service was down,
  the orchestrator should call `plate.unload` on first reconnect.

### Corruption handling

If `state.json` is unreadable (truncated, manually edited to invalid
JSON), the service logs the exception and starts with `plate = None`.
The file is overwritten on the next mutation; the old contents are
lost. There is no backup rotation here — that's the workflow's
responsibility (see §3 below).

---

## 3. Cross-device data strategy

`state.json` is a **single-device working copy**, not a
cross-device source of truth. The same plate passing through the
solid doser, OT-2, Cytation, and sealer will exist in four
different `state.json` files (one per device, written when the
plate is loaded there), and **none of them is authoritative for
the plate's full history**.

The right architecture has three tiers:

```
┌─────────────────────────────────────────────────────────────────┐
│ Workflow code (project repo, e.g. solubility-screening)          │
│ - Owns plate_id, sample_id, the recipe                          │
│ - Drives the orchestration                                      │
│ - Reads /status from each device for the LIVE working copy      │
│ - Writes consolidated history to lab.db on transitions          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
       ┌───────────────────┼──────────────────┐
       │                   │                  │
       ▼                   ▼                  ▼
┌──────────────┐   ┌───────────────┐   ┌──────────────┐
│ dose_every_  │   │ ot2 (OT-2     │   │ cytation_5   │
│ well         │   │ liquid hand.) │   │ (reader)     │
│              │   │               │   │              │
│ state.json   │   │ state.json    │   │ state.json   │
│ (per-device  │   │ (per-device   │   │ (per-device  │
│ working copy)│   │ working copy) │   │ working copy)│
└──────────────┘   └───────────────┘   └──────────────┘
                           │
                           ▼
                  ┌──────────────────────────────┐
                  │ lab.db on dashboard host     │
                  │ /opt/ac-organic-lab/data/    │
                  │                              │
                  │ runs + well_results tables   │
                  │ (authoritative history)      │
                  └──────────────────────────────┘
```

### The contract

1. **Device `state.json`** = "what's on my stage right now."
   Lives for the duration the plate is loaded on that device.
   Cleared on `plate.unload`.

2. **Central `lab.db`** = "what happened to every well of every plate,
   ever." Append-only. Queryable across devices. Schema in
   [`docs/OBSERVABILITY.md §4`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/docs/OBSERVABILITY.md#4-central-sqlite-schema-labdb)
   in the monorepo.

3. **Workflow code in your project repo** = the only thing that sees
   the whole picture. It:
   - Mints `plate_id` once at run start.
   - Calls `plate.load(plate_id, wells=[...])` on each device with the
     **current** wells state, hydrated from `lab.db` or from the
     previous device's `/status`.
   - Receives back per-well results from `read.*` and writes them to
     `lab.db` via `POST /api/ingest/wells` (per OBSERVABILITY.md).
   - Calls `plate.unload` on each device before moving the plate.

### Why this split

The plate is one physical object that passes through n devices. If
device A held the canonical state, device B would have to know about
A's REST API to pull "current state" — n-way coupling. If a central
"plate broker" service held it, every device would have to push to
it on every change — write amplification + dependency on a central
service for local operation.

Pushing the authoritative store one layer **above** the devices
(`lab.db` on the dashboard host) and one layer **below** the
workflow (the orchestrator chooses what to send to each device)
keeps device APIs uniform and devices independent. It also makes
each device's `state.json` valuable on its own — for a quick service
restart while the plate is mid-cycle on that device, you don't
need to round-trip to a database to recover.

### The handoff protocol

For each device transition (e.g. doser → liquid handler):

```python
# 1. Read out the state from the device that's giving up the plate.
src_status = await doser_client.get("/status")
wells_now = src_status["details"]["loaded_plate"]["wells"]
plate_id  = src_status["details"]["loaded_plate"]["plate_id"]

# 2. Persist to lab.db (authoritative history).
await httpx.post(
    f"{DASHBOARD}/api/ingest/wells",
    json={
        "run_id": run_id,
        "ts": utc_now(),
        "device_id": "dose_every_well",
        "wells": wells_now,
    },
)

# 3. Unload from the source device.
await doser_client.post("/control/plate/unload", headers=H_doser)

# 4. Physically move the plate (xArm or human).
await xarm.move_plate(from_="doser", to_="liquid_handler")

# 5. Load into the destination device, passing the current state.
await ot2_client.post(
    "/control/plate/load",
    headers=H_ot2,
    json={"plate_id": plate_id, "model": "custom_96", "wells": wells_now},
)

# 6. OT-2 now does its work, mutating volume_ul via /control/well/update
# as it dispenses.
```

### What about reads going back to the orchestrator

Reads do not mutate device state, so the right destination is the
central history DB, not back into `state.json`:

```python
abs_data = (await reader_client.post(
    "/control/read/absorbance",
    headers=H, json={"wells": ALL_WELLS, "wavelength_nm": 260.0},
)).json()["wells"]

await httpx.post(
    f"{DASHBOARD}/api/ingest/wells",
    json={
        "run_id": run_id,
        "ts": utc_now(),
        "device_id": "cytation_5",
        "wells": [
            {"well": w, "metric": "absorbance_260", "value": v}
            for w, v in abs_data.items()
        ],
    },
)
```

This requires extending the existing `well_results` table to handle
non-dose metrics (the current schema is dose-specific:
`target_mg` / `actual_mg` / `converged`). That extension lives in
the `ac-organic-lab` monorepo, not here — track it under "schema
gaps" in §4.

---

## 4. Schema gaps you'll hit, and where to put things

### `WellSample` only tracks one numeric quantity (`volume_ul`)

For solid + liquid + read tracking, four numeric quantities matter:

| Quantity            | Lives where today                | Recommended home          |
|---------------------|----------------------------------|---------------------------|
| `volume_ul`         | `WellSample.volume_ul`           | stays here                |
| `solid_mass_mg`     | nowhere — JSON-encode in `notes` | extend `WellSample`       |
| Read results        | nowhere on device                | `lab.db.well_results`     |
| Per-component mass / volume (composition) | nowhere | `lab.db.well_results` per metric, or a future structured field |

**Recommendation:** until a workflow proves the need, JSON-encode
extra fields into `notes`:

```python
import json

await client.post(
    "/control/well/update",
    headers=H,
    json={
        "well": "A1",
        "sample_id": "compound-42",
        "volume_ul": 200.0,                       # post-dispense liquid volume
        "notes": json.dumps({                     # device stores opaquely
            "solid_mass_mg": 5.2,
            "solvent": "DMSO",
            "dispensed_at": "2026-05-24T03:11:00Z",
        }),
    },
)
```

The device stores `notes` opaquely; the workflow parses it on read-back.
This is intentionally low-effort — once you have two or more workflows
that need the same structured field, promote it to a first-class
`WellSample` attribute (and add a migration for any in-flight
`state.json` files).

### `plate.load` is destructive

Calling `plate.load` with a new `plate_id` overwrites whatever
plate was previously loaded, even if you forgot to `plate.unload`
first. The device does not enforce "must unload before load" because
a hardware reset or operator intervention might have physically
removed the plate without a clean handoff. The workflow is
responsible for ensuring the previous plate is physically off
the stage.

### No backups inside the device

`state.json` has no backup rotation. If `lab.db` is being written
correctly by the workflow at every transition, you can lose
`state.json` and only forfeit "what the device thinks is loaded right
now" — the authoritative history survives. **Do not rely on
`state.json` as a recovery store; rely on `lab.db`.**

---

## 5. End-to-end example: dose → dispense → read

A representative solubility-screening flow. Pseudocode; real
implementation goes in your project repo (e.g.
`solubility-screening/`), not here.

```python
import json, uuid, httpx
from datetime import datetime, timezone

DASH = "http://ac-organic-lab.tail6a1dd7.ts.net:8001"
DOSER = "http://sdl2-pi5-minicnc.tail6a1dd7.ts.net:8000"
OT2   = "http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8020"
READER = "http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:9333"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

ALL_WELLS = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]

run_id = str(uuid.uuid4())
plate_id = "P-2026-05-24-001"

# ── 0. Register the run in lab.db ─────────────────────────────────
httpx.post(f"{DASH}/api/ingest/runs", json={
    "id": run_id, "started_at": utc_now(),
    "device_id": "multi", "plate_id": plate_id,
    "config_name": "solubility_v3", "n_wells": 96,
})

# ── 1. Solid dosing ───────────────────────────────────────────────
H_doser = claim(DOSER, owner=f"run:{run_id}")
httpx.post(f"{DOSER}/control/plate/load", headers=H_doser,
           json={"plate_id": plate_id, "model": "custom_96"})

dose_results = httpx.post(  # hypothetical doser endpoint
    f"{DOSER}/control/dose/plate",
    headers=H_doser,
    json={"target_mg": 5.0, "wells": ALL_WELLS, "compound_id": "caffeine"},
).json()

# Doser also updated each well's notes with solid_mass_mg.
src = httpx.get(f"{DOSER}/status").json()
wells_after_dose = src["details"]["loaded_plate"]["wells"]

# Persist to lab.db, then unload.
httpx.post(f"{DASH}/api/ingest/wells", json={
    "run_id": run_id, "ts": utc_now(), "device_id": "dose_every_well",
    "wells": wells_after_dose,
})
httpx.post(f"{DOSER}/control/plate/unload", headers=H_doser)
release(DOSER, H_doser)

# ── 2. Liquid dispense (OT-2) ────────────────────────────────────
H_ot2 = claim(OT2, owner=f"run:{run_id}")
httpx.post(f"{OT2}/control/plate/load", headers=H_ot2, json={
    "plate_id": plate_id, "model": "custom_96",
    "wells": wells_after_dose,             # <-- hydrate from dose stage
})

# OT-2 dispenses 200 uL DMSO into each well. Hypothetical OT-2 endpoint.
httpx.post(f"{OT2}/control/dispense/plate", headers=H_ot2, json={
    "wells": ALL_WELLS, "volume_ul": 200.0, "reagent": "DMSO",
})

# OT-2's workflow code calls /control/well/update for each well it touched,
# setting volume_ul = 200.0.
for w in ALL_WELLS:
    httpx.post(f"{OT2}/control/well/update", headers=H_ot2, json={
        "well": w, "volume_ul": 200.0,
    })

src = httpx.get(f"{OT2}/status").json()
wells_after_dispense = src["details"]["loaded_plate"]["wells"]

httpx.post(f"{DASH}/api/ingest/wells", json={
    "run_id": run_id, "ts": utc_now(), "device_id": "ot2",
    "wells": wells_after_dispense,
})
httpx.post(f"{OT2}/control/plate/unload", headers=H_ot2)
release(OT2, H_ot2)

# ── 3. Plate-reader (Cytation) ───────────────────────────────────
H_cyt = claim(READER, owner=f"run:{run_id}")
httpx.post(f"{READER}/control/plate/load", headers=H_cyt, json={
    "plate_id": plate_id, "model": "custom_96",
    "wells": wells_after_dispense,          # <-- hydrate again
})

abs_260 = httpx.post(
    f"{READER}/control/read/absorbance", headers=H_cyt,
    json={"wells": ALL_WELLS, "wavelength_nm": 260.0},
).json()["wells"]

# Reads go to lab.db directly -- they don't modify on-device state.
httpx.post(f"{DASH}/api/ingest/wells", json={
    "run_id": run_id, "ts": utc_now(), "device_id": "cytation_5",
    "metric": "absorbance_260",
    "wells": [{"well": w, "value": v} for w, v in abs_260.items()],
})

httpx.post(f"{READER}/control/plate/unload", headers=H_cyt)
release(READER, H_cyt)

# ── 4. Close out the run ────────────────────────────────────────
httpx.post(f"{DASH}/api/ingest/runs/{run_id}/complete", json={
    "finished_at": utc_now(), "status": "complete",
})
```

After this run, the **full provenance** of every well lives in
`lab.db.well_results`, keyed by `run_id` + `well`. The individual
device `state.json` files are empty (post-unload). You can query
"every well that ever held caffeine" or "every read on plate
P-2026-05-24-001" by hitting the central DB; no per-device round-trip
is needed for history.

---

## 6. Quick-reference recipe

When in doubt:

- **"What's currently on the Cytation stage?"** → `GET /status`,
  read `details.loaded_plate`.
- **"What did device X do to well A1?"** → query `lab.db.well_results`
  for `run_id=…, well='A1', device_id='X'`.
- **"What's the current volume in A1?"** → if the plate is still on a
  device, `GET /status` on that device. If it's between devices, the
  last `well_results` row for that well is the most recent truth.
- **"Should I write this to `state.json` or `lab.db`?"** → if the
  next device needs to see it on `plate.load`, write to `state.json`
  via `/control/well/update`. If it's history nobody needs to act on,
  write to `lab.db`. Most things are both.

---

## See also

- `src/agilent_cytation_server/models.py` — `WellSample` / `LoadedPlate`
- `src/agilent_cytation_server/plate_state.py` — `PlateStateStore`
- `src/agilent_cytation_server/service.py` — `load_plate` /
  `unload_plate` / `update_well` methods
- `src/agilent_cytation_server/api.py` — `/control/plate/*` /
  `/control/well/update` endpoints
- [`docs/phase4_handoff.md`](phase4_handoff.md) — the central server's
  skill-catalog patch; mirrors `WellSample` / `PlateLoadArgs` /
  `WellUpdateArgs` from the device-side models documented above.
- [`ac-organic-lab/docs/OBSERVABILITY.md`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/docs/OBSERVABILITY.md)
  — central `lab.db` schema for the cross-device tier.
- [`ac-organic-lab/docs/STATUS_SPEC.md`](https://github.com/cyrilcaoyang/ac-organic-lab/blob/main/docs/STATUS_SPEC.md)
  — the contract this device implements.
