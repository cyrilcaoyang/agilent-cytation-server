# BioTek (Agilent) Cytation 5 — agent guide

This service fronts one BioTek / Agilent Cytation 5 multi-mode plate reader
(absorbance, fluorescence and luminescence reads, an incubator, a shaker, and
— when fitted — a microscopy camera) and speaks the AC Organic lab's
STATUS_SPEC **v1.2** (`equipment_kind: "plate_reader"`, `equipment_id`
defaults to `cytation_5`). Read this before driving it; the
[API reference](agent-docs/api-reference) lists every route with its body and
refusal codes, and `/openapi.json` carries the exact schemas.

The deployed instance listens on port **8040**; the code's own default is
9333, so trust `config.toml` over the constant.

## What "primary operation" means here

A **measurement** — `read.absorbance`, `read.fluorescence`,
`read.luminescence` — or an **image capture**. Those four are what
`metrics.cycles_total` counts.

Three neighbouring things are deliberately handled differently, because
getting them wrong is how a caller ends up either blind or misled:

| thing | `activity` | counted in `cycles_total` |
|---|---|---|
| a read or a capture | `running` | yes |
| a drawer move | `running` — the instrument is executing a commanded operation and cannot start a read until it finishes | no |
| shaking | `running`, observed from the driver's own `is_shaking()` flag | no |
| holding a temperature setpoint | `idle` — a maintained condition, not an operation in progress | no |

Shaking is observed rather than bracketed for a concrete reason: the shake
command returns as soon as motion starts and a background task keeps the plate
moving, so a span opened and closed around the request would report
milliseconds of `running` for minutes of motion.

`activity` is derived from `_busy_state` (set around each call into the
driver) and that shaking flag — never from `equipment_status`, which §2.3
forbids because it would add no information. `activity_since` is stamped only
when the value *changes*, so it is the start of the current span, not the poll
instant.

Use `cycles_total` for usage accounting, not sampled `activity`: a read can
start and finish between two dashboard polls, so a sampled series does not
undercount reads, it misses them entirely. `read_count` is this repo's older
measurement-only counter and excludes image captures; `cycles_total` includes
them.

## Health vs. activity (STATUS_SPEC §2.2 / §2.3)

`equipment_status` answers "is it fit for a run"; `activity` answers "is it
running". They are computed independently and reported side by side.

| `equipment_status` | meaning here |
|---|---|
| `dry_run` | simulation against the stub reader; no hardware. Checked first, so a dry-run service never reports any other state. |
| `busy` | an operation owns the driver right now |
| `error` (link desync) | a shake abort left the serial request/response stream off by one and the resync probes could not recover it. Everything times out, so there is no useful subset left to call `degraded`. `required_actions: ["shutdown", "startup"]`. |
| `error` (recent failure) | an operational `/control/*` action failed within the last 60 s; `message` is `last_error.message` |
| `degraded` | connected, but the cached temperature readback failed; `message` lists the readback errors |
| `ready` | connected, idle, readbacks healthy |
| `requires_init` | the service is up but **not connected to the reader**; `required_actions: ["startup"]` |

The link-desync state is a *live* condition, not a timestamped error: a desync
does not heal on a timer, so an `error` that aged out of the 60 s window would
report `ready` on a link where every command still times out. The driver
clears the flag itself on the next coherent reply, so recovery needs no
operator action beyond the reconnect.

`GET /status` never issues hardware I/O on the request path. It serves a
temperature readback cached for 3 s and will wait at most 50 ms for the driver
lock to refresh it; `details.readback_age_s` says how stale the number is. A
poll that blocked for the duration of a plate read could not observe the read
happening, which is the whole point of `activity`.

### Components

`optics`, `incubator`, `plate_stage`, `shaker`, and `imaging` when
`[imaging].enabled` is true. `imaging.connected` tracks the **camera**, not
the config flag — a missing PySpin or Blackfly reports `connected: false` with
the reason in `message`, because §2.2 forbids hiding a subsystem fault.

`incubator.state` is keyed on the setpoint this service commanded, not on a
bare temperature threshold: `off` with no setpoint, `at_setpoint` within
0.5 °C, otherwise `heating` / `cooling`, or `unknown` when the readback is
unavailable. The instrument does not answer the temperature query while
shaking, and the `message` says so rather than leaving a gap that looks like a
fault.

## Claims (STATUS_SPEC §5)

Every `/control/*` request needs a valid `X-Claim-Token` when
`[service].enforce_claims` is true (the default). Acquire with
`POST /control/claim` (`{owner, session_id, ttl_s}`), heartbeat at the
returned `heartbeat_interval_s` (one third of the TTL, which gives roughly a
two-missed-heartbeat budget), release when done. `ttl_s` is clamped to
1–600 s.

- No token, or a token that does not match the active claim → **423**, body
  `{detail, claimed_by}`.
- `POST /control/claim` while a *different* session holds the claim → **409**
  with `claimed_by` and `retry_after_s`, plus a `Retry-After` header.
- The same `session_id` re-claiming is idempotent: it rotates the token and
  extends the expiry.
- `details.claimed_by` on `/status` shows the current holder;
  `details.claims_enforced` says whether the gate is armed at all.

The `lab_skills.ClaimManager` does all of this for you.

## Startup and shutdown

- The process **auto-connects at start** (FastAPI lifespan), bounded by
  `[service].startup_connect_timeout_s` (default 30 s). Failure is logged and
  the service lands in `requires_init` rather than refusing to boot.
- `POST /control/startup` connects (no-op if already connected), reads the
  firmware revision into `equipment_version`, and re-assigns the plate that
  `state.json` remembers to the freshly-connected driver.
- `POST /control/shutdown` disconnects. It is best-effort and never raises;
  the service then reports `requires_init` and will **not** reconnect on its
  own while the process keeps running. Do not end a session with the device
  shut down unless you were asked to — release your claim and leave it
  connected.
- A failed `startup` discards the half-open driver handle. Leaving it in place
  used to keep the USB handle for the life of the process, so one transient
  non-response made every later `startup` fail identically (2026-08-25).

### The restored plate is a claim, not an observation

Nothing on this instrument reports whether a plate is physically present. On
connect, the service re-assigns whatever plate `state.json` names, so if
someone lifted the plate out while the service was down the driver will now
insist one is there. `details.plate_restored_at_startup` marks exactly that
case. Treat a restored plate as an assertion and confirm it physically before
reading.

Related: `details.loaded_plate` is what the persisted store remembers, while
`details.plate_in_reader` is whether the driver's resource tree actually holds
a plate. Only the latter permits a read, and the two disagree after a restart
where restoration failed.

## Preconditions (STATUS_SPEC §6)

Read `allowed_actions` first. An action listed there will not be refused with
a 412 if invoked immediately — §6.2 — because `allowed_actions` and the
`/control/*` gates are computed from the same helpers (`_plate_loaded`,
`_drawer_is_open`, `_camera_ready`), so the two surfaces cannot drift.

A 412 body is `{detail, precondition, ...}` and is branched on by
`precondition`, never by prose:

| `precondition` | withholds | extra fields |
|---|---|---|
| `plate_not_loaded` | all three reads, `imaging.capture` | `required_action: "plate.load"` |
| `drawer_open` | all three reads, `imaging.capture` | `drawer_state`, `required: "in"`, `required_action: "drawer.close"` |
| `camera_not_ready` | `imaging.capture` | `camera_error` |

None of these carry a `retry_after_s`: each clears by an action, not by
waiting, so §6.1 wants the field omitted rather than guessed.

A 412 is not a failure. The equipment is healthy and declining an
inapplicable request, so a refusal neither populates `last_error` (§6.3) nor
clears it (§6.4).

### The drawer interlock is dead reckoning

There is no carrier-position query in the driver's command set — `J` opens,
`A` closes, and nothing reports where it is. `details.drawer` is seeded `"in"`
at connect and moved only when this service moves it, so the interlock blocks
**only on a known-open drawer**, never on `"unknown"`. Failing open on
uncertainty is deliberate: a stale `"in"` costs the same driver assertion we
would get anyway, while a stale `"out"` would refuse every read on a perfectly
loaded instrument with no way for the operator to argue with it.

The consequence worth stating plainly: this does **not** detect someone
pressing the front-panel eject button. It catches the case the software knows
about — a `drawer.open` that was never followed by a `drawer.close`.

## Refusal codes at a glance

| code | means | retry unchanged? |
|---|---|---|
| **401** | unknown or stale token on `/control/heartbeat` | no — re-claim |
| **409** | `/control/claim` held by another session; or `well.update` addressing no-plate-loaded / a well not on the loaded plate | no |
| **412** | precondition unmet; branch on `precondition` | after the named action |
| **422** | the request itself is wrong — schema violation, unknown plate model, well not on the plate, unknown imaging channel, a fluorescence channel whose filter cube is not fitted, unknown objective, unknown shake pattern | never |
| **423** | missing or invalid `X-Claim-Token` | no — claim first |
| **503** | genuine execution failure in the driver or instrument | maybe |

Read bodies reject unknown fields (`extra: "forbid"`) rather than ignoring
them. This matters more than tidiness: passing `gain` to a read would
otherwise be silently dropped and return a plausible number measured at some
other gain — a wrong result that looks right. The driver exposes no gain
control on any read, so the only safe answer is to refuse.

## `last_error.code` (branch on the code, never the message)

`last_error` means "the most recent operational failure since the last
successful action". A successful operation clears it. Severity is always
`error`.

| code | source |
|---|---|
| `startup` | connect failed |
| `drawer.open`, `drawer.close` | carrier move failed |
| `read.absorbance`, `read.fluorescence`, `read.luminescence` | the read failed |
| `incubator.set_temperature`, `incubator.stop` | temperature command failed |
| `shake.start`, `shake.stop` | shaker command failed |
| `imaging.capture` | capture failed |
| `link_desync` | `shake.stop` succeeded but left the serial link out of sync; the condition, not the action, is named |

Precondition codes (`drawer_open`, `plate_not_loaded`, `camera_not_ready`)
never appear here. They ride the 412 body's `precondition` field.

If the driver rejects a command it may surface as a bare `AssertionError` with
an empty message; the service substitutes a hint rather than putting
`{"detail": ""}` on the wire.

## Reads: `null` always means over-range

`ReadResponse` is `{wells: {well: float|null}, over_range: [well]}`. A well
whose sample exceeds the detector range comes back `null` **and** is named in
`over_range`. A well the instrument returned nothing for at all raises before
a response exists, so `null` has no other meaning.

Never read `null` as zero. On a serial dilution the saturated wells are the
most concentrated points, so a caller that treats them as missing quietly fits
its curve to the tail and reports a confident wrong slope.

## Reads: what cannot be measured

Three limits that are refusals, not clamps — the device will not quietly give
you a nearby value instead. Full detail and the measurements behind them are in
`/agent-docs/api-reference`.

**There is no spectrum or scan verb.** `read.absorbance` takes ONE
`wavelength_nm`; `read.fluorescence` takes ONE `excitation_nm` and ONE
`emission_nm`. Both bodies reject unknown fields, so asking for a range is a
**422**, not a sweep. A spectrum is one call per wavelength — and since a claim
`ttl_s` caps at 600 s while a 46-point sweep runs ~11 minutes, a sweep **must**
heartbeat between reads.

**Emission stops at 700 nm.** Excitation and emission are both bounded
250-700 nm. Emission above 700 nm is not measurable on this path at any
setting. Emission must also sit ~20-30 nm redder than excitation, or the
monochromator passes scattered excitation light and the read measures the lamp.

**Luminescence cannot read the H12 corner.** Any region whose maximum corner is
exactly H12 fails with 503 — including the whole plate, so there is no
full-plate luminescence read. `H11`, `G12`, `A1..H11` and `A1..G12` all read
fine. Read around the corner (`A1..H11` plus `A1..G12` covers 95 of 96 wells),
or take H12 by absorbance or fluorescence, which both return it normally.

Absorbance (350-800 nm) and fluorescence (400-700 nm at 360 nm excitation) are
bench-verified on a 19 mm plate as of 2026-09-22. Luminescence is not verified
on this instrument at all.

On a 19 mm plate keep `focal_height_mm` at the 7.0 default: below ~5.7 mm the
read is refused with `5B00`.

## Shaking

`shake.start` takes `{pattern, displacement_mm}`. `displacement_mm` is the
orbit displacement and runs **inversely** to speed — 6 mm is about 360 CPM,
1 mm about 1096 CPM — which is why it is not called a frequency or a speed.

`shake.start` returns once motion has begun, not when it ends. There is no
duration argument and no server-side timer: the plate keeps moving until
`shake.stop`. While shaking, `allowed_actions` carries `shake.stop` and
nothing else beyond the claim verbs, and the temperature readback is
unavailable.

`shake.stop` drains and re-probes the link on the way out. If that probe
fails, the shaker did genuinely stop but the link is desynchronised:
`last_error.code` becomes `link_desync` and the device goes to `error` until
a `shutdown` / `startup` pair.

## Imaging

`imaging.capture` needs `[imaging].enabled`, an initialised camera, a plate in
the driver, and the drawer in. The response echoes the **resolved** values —
channel, focal height, exposure, objective — not the ones you asked for, so
that a caller who requested autofocus or auto-exposure learns what the
instrument actually used rather than the values the search discarded.

Fluorescence channels need the matching filter cube physically fitted;
`details.imaging.installed_filters` answers that from `/status` instead of by
opening the box. `details.imaging.installed_objectives` is derived from
condenser annuli rather than the objective turret, which is why
`objectives_source` and `objectives_verified` ride alongside it — treat the
list as a hint, not an inventory.

Per `GET /docs/agent`, brightfield 4x and fluorescence imaging are marked
`pending_installation_and_validation`. A camera that connects and a command
that succeeds do not prove installed, focused optics.

## Discovery

`/llms.txt` (this index), `/agent-docs` (this guide),
`/agent-docs/api-reference`, `/docs/agent` (the versioned JSON guide with
per-capability validation status and the `/control/*` action schemas),
`/openapi.json`, `/docs` (Swagger UI).
