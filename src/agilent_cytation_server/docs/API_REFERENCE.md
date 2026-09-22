# Cytation 5 — API reference

Base: the deployed service's `http://<host>:8040` (the code's own default port
is 9333; `config.toml` wins). All timestamps UTC ISO-8601. Exact
request/response schemas: `/openapi.json`. Tags: `spec`, `claim`, `control`,
`documentation`.

A note on status codes before the tables: several `/control/*` routes return
**204 No Content** at runtime while the generated OpenAPI document declares
`200`, because the handler returns a bare `Response` rather than a typed
model. The codes below are the ones actually sent.

## Read (always available, side-effect-free, no claim)

| route | returns |
|---|---|
| `GET /` | `ProbeResponse` — `{equipment_id, equipment_name, protocol_version}` (`"1.2"`) |
| `GET /health` | `{"status": "healthy"}` |
| `GET /status` | full `EquipmentStatus` envelope — see below |
| `GET /docs/agent` | the versioned JSON agent guide: `documentation_version`, `protocol_version`, `api_version`, `discovery`, `agent_boundaries`, `plate_requirements`, per-capability `validation_status` + `limitations`, `maintenance`, and `action_schemas` (the `/control/*` slice of the OpenAPI paths plus its `components`). No claim, no hardware I/O. |
| `GET /openapi.json`, `GET /docs` | OpenAPI document, Swagger UI |
| `GET /agent-docs`, `GET /agent-docs/api-reference`, `GET /llms.txt` | this documentation (`text/markdown`, `text/markdown`, `text/plain`) |

### `GET /status` envelope

`protocol_version`, `equipment_id`, `equipment_name`, `equipment_kind`
(`"plate_reader"`), `equipment_version`, `host`, `equipment_status`,
`activity`, `activity_since`, `message`, `required_actions`,
`allowed_actions`, `device_time`, `uptime_seconds`, `components`, `metrics`,
`last_error`, `details`.

| `components` key | states |
|---|---|
| `optics` | `idle`, `busy`, `disconnected` |
| `incubator` | `off`, `at_setpoint`, `heating`, `cooling`, `unknown`, `disconnected` |
| `plate_stage` | `in`, `out`, `unknown` (dead reckoning — see the agent guide) |
| `shaker` | `idle`, `shaking`, `disconnected` |
| `imaging` | `idle`, `busy`, `disconnected`; present only when `[imaging].enabled`. `connected` tracks the camera, not the flag. |

| `metrics` key | unit | notes |
|---|---|---|
| `actual_temperature` | `C` | cached readback; omitted when unavailable |
| `setpoint_temperature` | `C` | present only while a setpoint is commanded |
| `last_read_seconds_ago` | `s` | present after the first read |
| `read_count` | `count` | measurements only, excludes captures |
| `cycles_total` | `count` | §2.3.1 reserved key: measurements **and** captures |

| `details` key | meaning |
|---|---|
| `drawer` | `in` / `out` / `unknown` |
| `backend` | configured driver backend |
| `imaging_enabled` | the `[imaging].enabled` flag |
| `loaded_plate` | what `state.json` remembers, or `null` |
| `plate_in_reader` | whether the driver's resource tree holds a plate — only this permits a read |
| `plate_restored_at_startup` | the plate was asserted from disk, not loaded by an operator |
| `claimed_by`, `claims_enforced` | current claim holder; whether the gate is armed |
| `readback_age_s` | staleness of the cached temperature |
| `dry_run` | present and `true` only in dry-run |
| `instrument_serial`, `temperature_range_c` | reported by the instrument when it answers |
| `imaging` | `{camera_ready, camera_error, installed_objectives, installed_filters, objective_slots, filter_slots, objectives_source, objectives_verified, phase_contrast_available}` |

## Claim protocol

| route | body / header | responses |
|---|---|---|
| `POST /control/claim` | `{owner, session_id, ttl_s}` | **200** `{claim_token, heartbeat_interval_s, expires_at}`; **409** `{detail, claimed_by, retry_after_s}` + `Retry-After` when another session holds it. `ttl_s` clamped to 1–600; `heartbeat_interval_s` = ttl/3 (min 1). Idempotent for the same `session_id` — rotates the token, extends the expiry. |
| `POST /control/heartbeat` | header `X-Claim-Token` (required) | **204**; **401** `{detail}` when the token is unknown or stale; **422** when the header is absent |
| `POST /control/release` | header `X-Claim-Token` (required) | **204**, idempotent — an unknown token is not an error |

## Control

Every route below takes an optional `X-Claim-Token` header that becomes
mandatory when `[service].enforce_claims` is true (the default). Without a
valid one: **423** `{detail, claimed_by}`.

### Lifecycle

| route | body | responses |
|---|---|---|
| `POST /control/startup` | — | **204**; **503** `{detail}` on connect failure (records `last_error.code: "startup"`). No-op when already connected. Re-assigns the persisted plate and sets `details.plate_restored_at_startup`. |
| `POST /control/shutdown` | — | **204** always — best-effort disconnect, never raises. Device then reports `requires_init` and does not reconnect on its own. |

### Drawer

| route | body | responses |
|---|---|---|
| `POST /control/drawer/open` | `{}` (empty object, optional) | **204**; **503** on driver failure (`last_error.code: "drawer.open"`) |
| `POST /control/drawer/close` | `{}` (empty object, optional) | **204**; **503** (`last_error.code: "drawer.close"`) |

Neither is gated on a precondition: the drawer interlock guards the *optical*
actions, not the carrier itself.

### Plate and wells

| route | body | responses |
|---|---|---|
| `POST /control/plate/load` | `{plate_id, model?, wells?}` — `plate_id` 1–128 chars; `model` defaults to the previous model for the same `plate_id`, else `[plates].default_model`; `wells` omitted preserves the map on a same-ID reload and otherwise seeds 96 empty wells | **200** `LoadedPlate` `{plate_id, model, loaded_at, wells}`; **422** unknown plate model, an invalid or duplicated well id; **503** on driver failure. Does **not** move the drawer. |
| `POST /control/plate/unload` | — | **200** the removed `LoadedPlate`, or `null` when none was loaded; **503** on driver failure |
| `POST /control/well/update` | `{well, sample_id?, volume_ul?, notes?, clear_sample_id?, clear_notes?}` — `well` is `A1`..`H12`; the `clear_*` flags win over a supplied value | **200** `WellSample`; **409** when no plate is loaded **or** the well is not on the loaded plate (both are `LookupError`); **422** on a negative volume; **503** when the state file cannot be written |

### Reads

All three reject unknown body fields (**422**), take `wells` of 1–96 names,
and are gated on `plate_not_loaded` and `drawer_open`. Each returns
`ReadResponse` `{wells: {name: float|null}, over_range: [name]}` — a `null`
value always means over-range and is named in `over_range`.

| route | body | bounds |
|---|---|---|
| `POST /control/read/absorbance` | `{wells, wavelength_nm}` | `wavelength_nm` 230–999 |
| `POST /control/read/fluorescence` | `{wells, excitation_nm, emission_nm, focal_height_mm?}` | ex 250–700, em 250–700, focal 4.5–13.88 (default 7.0) |
| `POST /control/read/luminescence` | `{wells, focal_height_mm?, integration_time_s?}` | focal 4.5–13.88 (default 7.0), integration 0.1–60 s (default 1.0) |

Responses: **200**; **412** `{detail, precondition: "plate_not_loaded" | "drawer_open", ...}`; **422** a well not on the plate or a bound violated; **503** on an instrument failure, recording `last_error.code: "read.absorbance" | "read.fluorescence" | "read.luminescence"`.

### Taking a wavelength sweep

Verified end to end 2026-09-22 on serial 23030927: 46 absorbance points
(350-800 nm) plus 31 emission points (400-700 nm at 360 nm excitation) over
wells A1-C3, 693 values, no refusals.

**There is no spectrum verb.** `read.absorbance` takes one `wavelength_nm`;
`read.fluorescence` takes one `excitation_nm` and one `emission_nm`. Both bodies
reject unknown fields, so a `wavelength_start_nm` / `step_nm` request is refused
with **422** rather than partially honoured. Gen5 cannot define a scan either —
`ReadType = Spectrum` is rejected by its own parser, `EndPoint` being the only
accepted value. A sweep is therefore **one call per wavelength**, and a 43-point
"spectrum" is 43 requests.

A sweep outlives a claim. `ttl_s` is clamped to **1-600 s**, and a 46-point
absorbance sweep takes ~11 min, so you **must** heartbeat:

```
POST /control/claim   {"owner": "...", "session_id": "...", "ttl_s": 600}
   -> {"claim_token": "...", "heartbeat_interval_s": 200.0, "expires_at": "..."}

for each wavelength:
    POST /control/heartbeat            X-Claim-Token: <token>     # 204
    POST /control/read/absorbance      X-Claim-Token: <token>
         {"wells": ["A1","A2",...], "wavelength_nm": 350}

POST /control/release                  X-Claim-Token: <token>     # also on failure
```

Measured per-read cost: **~14 s** absorbance, **~20 s** fluorescence, rising to
**~35 s** when the read region is padded (below). Persist results as they
arrive — a sweep that dies at point 40 of 46 should not lose the first 39.

**Read the response correctly.** A `null` in `wells` *always* means over-range
and the well is named in `over_range`; it never means "not measured". On a
dilution series the saturated wells are the most concentrated points, so code
that treats `null` as missing quietly fits its curve to the tail and reports a
confident wrong slope.

**Padding.** The instrument refuses any read command whose checksum falls in a
rejected band, answering the start-read with status `2D06`. The driver steps
around it by growing the read region until the checksum is acceptable — which is
why a padded read is slower, and why more wells than you asked for may be
measured. The response still contains only the wells you requested. A **503**
naming `2D06` means that guard was bypassed or has regressed: report it rather
than retrying.

Bounds that bite, all of them hard refusals rather than clamps:

| | limit | note |
|---|---|---|
| absorbance | 230-999 nm | |
| excitation | 250-700 nm | |
| **emission** | **250-700 nm** | 700 nm is a **hardware ceiling**, not a driver choice: the Cytation 5 spec sheet gives emission as 300-700 nm (`docs/INDEX.md`). Emission above 700 nm is not measurable on this instrument by any route, Gen5 included. |
| em vs ex | em ~20-30 nm redder | closer and the monochromator passes scattered excitation light — you measure the lamp |
| focal height | 4.5-13.88 mm | on a 19 mm plate anything below ~5.7 mm is refused with `5B00`; leave the 7.0 default |

Two state rules: reads are refused **412** while the drawer is open (close it
first — idempotent), and withheld entirely while the shaker runs, because the
read and the shake task share the serial link.

#### Wells that cannot be measured — the luminescence H12 corner

**`read.luminescence` fails with 503 for any region whose maximum corner is
exactly H12**, the whole plate included. Measured 2026-08-31:

| region | result |
|---|---|
| `H11`, `G12`, `A1..H11`, `A1..G12` | read fine |
| `H12`, `H12+H11`, `G11..H12`, `A1..H12` | **503** |

So a full-plate luminescence read is not available. Work around it by reading
around the corner — `A1..H11` plus `A1..G12` covers 95 of 96 wells — or read
H12 by absorbance or fluorescence, both of which return it normally.

This is **not** the rejected-checksum band and the padding described above does
not fix it: on this path the band predicts the *opposite* of what happens
(`H11` and `G12` compute checksums inside the band and succeed; `H12` computes
one outside it and fails). Luminescence is therefore deliberately left
unpadded — padding could grow a working region into the H12 corner and break
it. Absorbance and fluorescence read H12 without trouble.

### Incubator

| route | body | responses |
|---|---|---|
| `POST /control/incubator/set_temperature` | `{celsius}` — 18.0–65.0, the instrument's own declared range read over Gen5, not PyLabRobot's assumed (4, 45). Unknown fields rejected. | **204**; **422** out of range; **503** (`last_error.code: "incubator.set_temperature"`) |
| `POST /control/incubator/stop` | — | **204**; **503** (`last_error.code: "incubator.stop"`) |

Whether this unit can actually hold a setpoint below ambient is unverified —
18 °C is the declared floor, not a measured one.

### Shaker

| route | body | responses |
|---|---|---|
| `POST /control/shake/start` | `{pattern?, displacement_mm?}` — `pattern` `"orbital"` (default) or `"linear"`; `displacement_mm` 1–6 (default 3), the orbit displacement, **inverse** to speed. Unknown fields rejected. | **204** once motion has begun — there is no duration argument and no server-side timer; **422** unknown pattern or a displacement out of range; **503** (`last_error.code: "shake.start"`) |
| `POST /control/shake/stop` | — | **204**; **503** (`last_error.code: "shake.stop"`). May succeed and still leave `last_error.code: "link_desync"` with the device in `error` — see the agent guide. |

### Imaging

| route | body | responses |
|---|---|---|
| `POST /control/imaging/capture` | `{well, channel, focal_height_mm?, exposure_ms?, gain?, objective?, led_intensity?, autofocus?, auto_exposure?}` | **200** `ImagingCaptureResponse`; **412** `plate_not_loaded` / `drawer_open` / `camera_not_ready`; **422** unknown channel, a fluorescence channel with no matching cube fitted, unknown objective, well not on the plate, or a bound violated; **503** (`last_error.code: "imaging.capture"`) |

Bounds and defaults: `focal_height_mm` 4.5–13.88 (default 5.0), `exposure_ms`
0.01–10000 (default 10.0), `gain` 0–47 dB (default 0.0, camera analog gain —
unrelated to PMT gain, which no read exposes), `led_intensity` 1–10 (default
10), `objective` defaults to the lowest-magnification installed one. Unknown
fields rejected.

Channel ids: `brightfield` (`bright_field`, `bf`), `color_brightfield`,
`phase_contrast` (`phase`), `dapi` (`uv`), `gfp`, `fitc`, `rfp`, `texas_red`,
`cy5`, `cfp`, `yfp`. Brightfield, colour brightfield and phase contrast go
through the condenser and need no cube; every other channel needs its filter
cube fitted. Phase contrast additionally depends on the firmware —
`details.imaging.phase_contrast_available` answers it, and `null` means the
firmware version is unknown.

`ImagingCaptureResponse` is `{well, channel, focal_height_mm, exposure_ms,
gain, objective, image_path, details}` and reports the **resolved** channel,
focal height, exposure and objective, not the requested ones, so an autofocus
or auto-exposure caller learns what the instrument used.

## Refusal codes

**401** stale heartbeat token · **409** claim held elsewhere, or a
`well.update` against no/unknown well · **412** precondition unmet (branch on
`precondition`) · **422** the request itself is wrong · **423** no or invalid
claim · **503** execution failure. A 412 neither sets nor clears
`last_error`; a 2xx from an operational action clears it.

## Skill names (what `allowed_actions` lists)

Always: `claim`, `heartbeat`, `release`.

| state | additionally advertised |
|---|---|
| `requires_init` | `startup` |
| `ready` / `dry_run` | `shutdown`, `drawer.open`, `drawer.close`, `plate.load`, `plate.unload`, `well.update`, `incubator.set_temperature`, `incubator.stop`, `shake.start`; plus `read.absorbance`, `read.fluorescence`, `read.luminescence` when a plate is in the driver and the drawer is not known-open; plus `imaging.capture` when those hold and the camera is ready |
| `busy` | nothing |
| `error` / `degraded` | `shutdown` |
| any state with `activity == "running"` | `shake.stop`, and only while the shaker is actually running |

`startup` is advertised only from `requires_init`, and `shake.stop` only while
shaking — the latter matters because a shake outlives the request that started
it, so without it the only documented way to stop the plate moving would be
`shutdown`.
