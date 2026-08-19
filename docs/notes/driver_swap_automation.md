# Driver-swap automation — design note

> **Status: design, not implemented.** Implementation is deferred to a feature
> branch. This note captures the plan so it survives the session.

## Problem

Switching the Cytation 5 between PyLabRobot (libusbK) and Gen5 (FTDI VCP) is a
manual ~5-minute Zadig dance (RUNBOOK §4/§5). The manual cost makes the
"spectral scan in Gen5, imaging in PyLabRobot" workflow impractical to do
routinely. Automating the swap turns it into a one-command switch.

## Goal

A single elevated CLI tool that flips the Cytation between the two driver
modes, verifies each step, and is safe to re-run (idempotent).

```
cytation-driver-mode pylabrobot   # FTDI → libusbK, dry_run=false, service on
cytation-driver-mode gen5         # libusbK → FTDI, dry_run=true, service on (dry_run)
```

## What's automatable

| Step | Automatable | Mechanism |
|---|---|---|
| Stop/start the service | yes | `nssm stop/start cytation` |
| Flip `config.toml` `dry_run` | yes | file edit |
| Unbind libusbK from the device | yes | `pnputil` / `devcon` / Win32 `SetupDi*` |
| Rebind to FTDI vendor driver | yes | `pnputil /add-driver ftdibus.inf` (driver already in store after one-time Gen5/FTDI install) |
| Bind libusbK (reverse direction) | yes | `libwdi` (Zadig's backend — CLI + Python bindings) |
| Trigger re-enumeration without replug | likely yes | `pnputil /scan` or `SetupDiChangeState` |
| Wait for device to come back | yes | poll device node or `/status` |
| Run a Gen5 protocol | yes (via COM) | Gen5 OLE automation — the `biotek_driver` path already uses this |

## The hard part

**Driver rebind without physical replug.** RUNBOOK §5.3 says "unplug, wait 3 s,
plug back in" — that's just forcing USB re-enumeration. Windows PnP can
re-enumerate a device in software:

- `pnputil /scan` triggers a PnP rescan
- `SetupDiChangeState` (Win32) re-triggers device enumeration
- A USB port reset (via `usbip` or WinUSB) can force the FTDI chip to
  re-enumerate

The FTDI chip should respond to software re-enumeration; the replug is the
manual easy path, not a hardware requirement. Needs bench confirmation.

**Permissions.** Driver install/removal needs admin. The device PC's UAC
prompts don't render in headless sessions (DEVICE_PC_SETUP), so the tool
must run from an already-elevated context — scheduled task, `nssm`-installed
helper, or `!` in the prompt.

## Shape of the tool

Single script, run elevated, takes a mode argument. Each direction:

1. `nssm stop cytation`
2. Edit `config.toml` (`dry_run` flip)
3. Unbind current driver (`pnputil` / `libwdi`)
4. Bind new driver (`libwdi` for libusbK, `pnputil /add-driver` for FTDI)
5. Trigger re-enumeration (`pnputil /scan`)
6. Poll for device to reappear (device node or `/status`)
7. `nssm start cytation`
8. Verify (`/status` shows `ready` or `dry_run`)

Idempotent: each step verifies its precondition before acting; safe to re-run
if a previous run died mid-swap.

## Risks

- **Orphaned device**: a bug in the rebind leaves the Cytation with no driver
  bound, requiring manual Device Manager intervention. The tool must verify
  each step and bail before committing to the next.
- **Driver store state**: if the FTDI vendor driver isn't in the store
  (someone "deleted driver software" globally), the rebind fails. Pre-check
  and offer to install from `ftdichip.com` if missing.
- **Timing**: PnP re-enumeration takes 2–10 s and varies. Poll, don't
  hardcode sleeps.
- **Testing**: touches system drivers on a production machine. Test on a
  maintenance window with a documented manual rollback path.

## Open questions for the branch

- Does `pnputil /scan` reliably re-enumerate the FTDI chip without replug, or
  is a USB port reset needed? Bench-test first.
- Is `libwdi`'s CLI (`wdi-simple`) sufficient for the libusbK bind, or do we
  need the Python bindings via ctypes?
- Should the tool also orchestrate Gen5 protocol execution (via OLE
  automation), or just the driver swap and leave Gen5 to the operator?
- Should the tool emit a STATUS_SPEC `activity` event so the dashboard
  reflects "driver swap in progress" rather than just `unknown`?

## Implementation plan (for the branch)

1. Prototype in PowerShell (lowest friction for `pnputil` + service control).
2. Bench-test the re-enumeration path on a maintenance window.
3. If re-enumeration works, harden the script (idempotent, verified, logged).
4. Wire to a CLI entry point and document in RUNBOOK.
5. Optional: add a `/control/driver-mode` REST endpoint that triggers the
   swap (service stops itself, runs the tool, restarts) — but this is
   risky (the service is modifying its own driver binding) and probably
   not worth it vs. a standalone CLI.

## Non-goals

- Not automating Gen5 GUI interaction beyond OLE/COM protocol execution.
- Not auto-detecting which mode is "needed" — the operator decides.
- Not touching the camera (Spinnaker) — it's shared and not part of the
  FTDI conflict.
