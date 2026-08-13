# Overnight handoff — Phases 2, 3, 4 of the Cytation roadmap

Three commits land in this batch, in order:

| Commit | Phase | What it does |
|---|---|---|
| `615e783` | **2** | Per-well sample tracking via `PlateStateStore` (JSON-backed). Surfaced under `details.loaded_plate` on `/status`. |
| `ad189a3` | **3** | STATUS_SPEC v1.1 — cooperative claim protocol + full `/control/*` write surface. `protocol_version` bumped to `1.1`. |
| *(this commit)* | **4** | Skill catalog draft + handoff doc at `docs/LABSKILLS.md`. The catalog itself cannot be applied from this PC because `ac-organic-lab/` is a read-only mirror per `~/Projects/CLAUDE.md`. |

Test suite: **68/68 passing** in `dry_run`. Hardware was **not** touched
overnight — all verification is local stub-only.

## What's safe right now

You can keep using the service exactly as it stood at commit `34c6277`
(real-hardware "ready"). The new `/control/*` endpoints are
additive — the existing read surface (`/`, `/health`, `/status`) is
backward-compatible at the JSON level apart from the `protocol_version`
field flipping from `"1.0"` to `"1.1"`.

## What needs your attention in the morning

### 1) Restore the production venv

`uv sync --extra dev` (run during testing) **removed** pylabrobot,
pyusb, libusb-package, pylibftdi, spinnaker_python from the venv. Before
the next driver swap to libusbK or any hardware boot:

```powershell
cd C:\Users\sdl2\Projects\agilent-cytation-server
C:\SDL_Tools\uv.exe sync --extra api --extra plr --extra windows
```

Then re-install spinnaker_python from the wheel per `RUNBOOK.md` §3.1
step 4 and re-pin `numpy<2`. The libftdi DLLs in `.venv\Scripts\` were
copied per §3.2 — `uv sync` does **not** touch those files, but verify
they're still present.

### 2) Hardware verification before flipping `protocol: "1.1"`

The dashboard's `equipment.yaml` still says `protocol: "1.0"` and
`do_not_call_connect: true`. **Do not flip it yet.** The order is:

1. Restore the venv (step 1 above).
2. Run the driver-swap procedure in `RUNBOOK.md` §3-§4 (FTDI → libusbK)
   so the real Cytation is reachable from PyLabRobot.
3. Restart `cytation` service: `nssm restart cytation`.
4. With the service in real-hardware mode, smoke-test the new endpoints:

   ```powershell
   # Claim the device
   $claim = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9333/control/claim `
       -ContentType application/json `
       -Body (@{owner="manual-test"; session_id=[guid]::NewGuid().ToString()} | ConvertTo-Json)

   $headers = @{ "X-Claim-Token" = $claim.claim_token }

   # Drawer open / close
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9333/control/drawer/open -Headers $headers -ContentType application/json -Body "{}"
   Start-Sleep -Seconds 5
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9333/control/drawer/close -Headers $headers -ContentType application/json -Body "{}"

   # Single-well absorbance read at 600 nm
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9333/control/read/absorbance `
       -Headers $headers -ContentType application/json `
       -Body (@{wells=@("A1"); wavelength_nm=600} | ConvertTo-Json)

   # Release
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9333/control/release -Headers $headers
   ```

5. Check `C:\SDL_Logs\cytation.err.log` for any PyLabRobot kwarg-drift
   errors. The reader uses `getattr`-style delegation in
   `src/agilent_cytation_server/reader.py`, so missing methods surface
   as clear `RuntimeError`s rather than `AttributeError` deep in PLR;
   if any read fails, the device will fall into `error` state for
   60 s (see `_RECENT_ERROR_WINDOW_S`).

6. **Only then** apply the patch in `docs/LABSKILLS.md` from the
   central server and restart the dashboard.

### 3) Phase 4 — needs to be applied on the central server

`ac-organic-lab/` is a read-only mirror on this PC, so the skill-catalog
file and `equipment.yaml` flip are **not** committed. The exact patch
to apply (one new file + one `__init__.py` import + one yaml diff)
is in `docs/LABSKILLS.md`. Push it from the central server, then
`git pull` here.

## Known caveats / things to watch

- **`protocol_version` bump is visible to the dashboard.** Until the
  monorepo flips `protocol: "1.1"` in `equipment.yaml`, the dashboard
  will log a version-mismatch warning. This is per
  `STATUS_SPEC.md` §"Best Practices" #3 (intended behaviour, but you'll
  see it in `ac-organic-lab-api` logs).
- **`enforce_claims = true` is the default.** Any client that calls
  `/control/*` without first claiming will get HTTP 423. The dashboard
  passthrough on the central server handles this transparently for
  `filter_every_well`; the cytation will follow the same pattern when
  the registry flip lands.
- **Imaging response is best-effort.** PyLabRobot does not (yet)
  expose `capture_image` on the `PlateReader` frontend; the real
  reader's `capture_image` delegates via `getattr` and will raise a
  clear `RuntimeError` if the method is absent. For full imaging
  support, the existing `scripts/capture_a1.py` remains the
  fallback path that uses the backend directly.
- **`details.dry_run` only appears in dry_run mode.** Tests that
  assert on it should not run against a real-hardware service.
- **Async / asyncio.Lock contention:** the service's single lock now
  guards *both* status polls and control verbs. Long reads
  (`integration_time_s = 60.0`) will block `/status` for the duration.
  The aggregator's `poll_timeout_seconds: 3.0` in `equipment.yaml`
  will see that as a poll failure; consider bumping it to 10 s if
  long-running luminescence reads become common.

## Where to start in the morning

1. `git log --oneline -5` — sanity-check the three new commits are local.
2. `uv sync --extra dev` and `uv run pytest -q` — confirm 68/68 still
   passes on a clean checkout. (Do this **before** restoring the
   production venv so you have a clean signal.)
3. Read `docs/LABSKILLS.md` to understand exactly what gets
   applied on the central server.
4. Restore the production venv per step 1 above.
5. Driver swap + hardware verification per step 2 above.
6. Apply Phase 4 from the central server, restart the dashboard,
   and confirm `await session.role("plate_reader").read_absorbance(...)`
   works against the real Cytation.

## File map (what's new in this branch)

```
new:
  src/agilent_cytation_server/plate_state.py     # Phase 2 store
  src/agilent_cytation_server/claims.py          # Phase 3 ClaimManager
  src/agilent_cytation_server/control_args.py    # Phase 3 request/response schemas
  tests/test_plate_state.py                      # 17 tests
  tests/test_claims.py                           # 12 tests
  tests/test_control.py                          # 17 tests
  docs/LABSKILLS.md                             # lab-skills catalog patch
  HANDOFF.md                                     # this file

modified:
  src/agilent_cytation_server/models.py          # WellSample / LoadedPlate / Claim* + v1.1 bump
  src/agilent_cytation_server/service.py         # control methods, claim wiring, allowed_actions
  src/agilent_cytation_server/api.py             # /control/* endpoints
  src/agilent_cytation_server/reader.py          # read_* / capture_image delegation
  src/agilent_cytation_server/config.py          # enforce_claims default
  config.example.toml                            # docs the new toggles
  tests/conftest.py                              # plate_state + advisory_client fixtures
  tests/test_api.py                              # v1.0 -> v1.1 assertions
  tests/fixtures/status_*.json                   # regenerated for v1.1 envelope
  README.md                                      # roadmap + REST API tables
```
