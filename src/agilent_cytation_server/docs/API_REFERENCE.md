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
