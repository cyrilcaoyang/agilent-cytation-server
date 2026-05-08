# Cytation 5 — Operational Runbook

Day-to-day operations for the BioTek (Agilent) Cytation 5 service running on `sdl2-pc-03-cytation`.

This runbook is the **source of truth** for procedures that touch the live instrument. The repo's [`README.md`](./README.md) covers project structure, install, and the API contract; this file covers what you actually do at the lab PC.

---

## Quick reference

| You want to… | Do this |
|---|---|
| Check service health | `sc.exe query cytation` and `curl http://127.0.0.1:9333/status` |
| Tail the logs | `Get-Content C:\SDL_Logs\cytation.out.log -Tail 30 -Wait` |
| Restart the service | `nssm restart cytation` |
| Stop the service | `nssm stop cytation` |
| Start the service | `sc.exe start cytation` (or `nssm start cytation`) |
| Edit config | edit `C:\Users\sdl2\Projects\agilent-cytation-server\config.toml`, then `nssm restart cytation` |
| Update from `git push` | see [§ Updating from a git push](#updating-from-a-git-push) |
| Switch from PyLabRobot → Gen5 | see [§ Driver swap: libusbK → FTDI (PyLabRobot off, Gen5 on)](#driver-swap-libusbk--ftdi-pylabrobot-off-gen5-on) |
| Switch from Gen5 → PyLabRobot | see [§ Driver swap: FTDI → libusbK (Gen5 off, PyLabRobot on)](#driver-swap-ftdi--libusbk-gen5-off-pylabrobot-on) |

---

## 1. Background: why driver swaps are necessary

The Cytation 5 has **two USB connections** to this PC:

| Subsystem | USB chip | Driver |
|---|---|---|
| Reader (drawer, optics, incubator, shaker) | **FTDI USB-serial bridge** | conflict zone — see below |
| Microscopy camera (Cytation 5 Imaging) | Point Grey / FLIR Blackfly | **Spinnaker SDK** — same on both stacks, no swap needed |

The FTDI chip is the conflict. It has to be bound to **exactly one** Windows driver at a time:

- **FTDI's vendor driver (`FTDIBUS`)** — what comes with Windows / Gen5 / BioTek's installer. **Gen5 needs this.** Exposes the chip as `USB Serial Converter` plus an optional `USB Serial Port (COMx)`.
- **libusbK** — generic raw-USB driver. **PyLabRobot needs this.** PyLabRobot's `pylabrobot.io.ftdi.FTDI` reaches the chip through libusb; libusb on Windows can only see the chip when libusbK (or WinUSB) is bound, not the FTDI vendor driver.

**Implication:** at any moment this PC is in **one of two modes** — *Gen5 mode* (FTDI driver bound, PyLabRobot can't reach the device, our service runs in `dry_run`) or *PyLabRobot mode* (libusbK bound, Gen5 can't open the device, our service can drive real hardware).

The two procedures below switch between modes. They take ~2 minutes each and require **Administrator** PowerShell on this PC.

---

## 2. Tools

Pre-stage these once on the lab PC. They live next to `uv.exe` and `nssm.exe` for consistency.

```powershell
# Run as Administrator
New-Item -ItemType Directory -Force C:\SDL_Tools | Out-Null

# Zadig (the libusb driver-swap utility) — single .exe, no install.
# Always grab the latest release here:
#   https://zadig.akeo.ie/      (mirror of https://github.com/pbatard/libwdi/releases)
# Pick the latest "zadig-X.Y.exe" and save as C:\SDL_Tools\zadig.exe.
# A browser download is the most reliable path; PowerShell direct downloads
# from GitHub Releases sometimes 404 on transient renames.

# Verify
Get-Item C:\SDL_Tools\zadig.exe | Select-Object Name,Length
```

`zadig.exe` is **read-only data**: not installed, not registered, no service. Run it on demand from `C:\SDL_Tools\` only.

The FTDI vendor driver (FTDI CDM) is presumed already present on this PC because Gen5 has been running here. If it ever needs to be re-installed (e.g. you "Delete the driver software" during a swap-back and Windows can't auto-recover), grab the official installer from <https://ftdichip.com/drivers/d2xx-drivers/> and run it once. The package installs into the system driver store and is then permanent.

---

## 3. Driver swap: FTDI → libusbK (Gen5 off, PyLabRobot on)

Use this when you want PyLabRobot to drive the Cytation for real (orchestrator runs, dashboard reaches `equipment_status: ready`, real `/control/*` measurements).

### 3.1 Pre-flight

```powershell
# 1) Make sure no Gen5 / BioTek session is active. Close Gen5 fully.
#    (If anyone is mid-protocol you will brick their run.)

# 2) Confirm our service is in dry_run today (it should be).
Get-Content C:\Users\sdl2\Projects\agilent-cytation-server\config.toml |
    Select-String '^\s*dry_run'
# Expected: dry_run = true
```

### 3.2 Stop the cytation service so it isn't fighting Zadig

```powershell
# Run as Administrator
nssm stop cytation
sc.exe query cytation | Select-String STATE   # expect STOPPED
```

Stopping the service is important: if the service tries to claim the FTDI handle while Zadig is rebinding, Zadig may fail with "Operation in progress".

### 3.3 Run Zadig and bind libusbK

1. **Right-click `C:\SDL_Tools\zadig.exe` → Run as Administrator.**
2. Menu **Options → List All Devices** — *required* (the FTDI device is normally hidden under composite-parent rules).
3. In the dropdown, find the Cytation FTDI device. The label is typically:
   - `USB Serial Converter` (manufacturer string `FTDI`), or
   - `BioTek-CYT5-…` if BioTek registered a custom string descriptor.
   - The USB **VID is `0403`** (hex; FTDI's vendor ID). PIDs vary by Cytation model — `6001`, `6010`, `6011`, `6014` are all possible.
4. **Verify you have the right device** by pulling the Cytation's USB cable for one second and replugging — the entry that disappears + reappears is the right one. Do this at least once before you click anything destructive.
5. In the right-hand "target driver" box, click the **green up/down arrows** until it shows **`libusbK (vX.X.X.X)`**.
6. Click **Replace Driver** (the button text may also read **Install Driver** depending on current state). Wait ~20–30 seconds. A Windows UAC prompt may appear.
7. When done, the row should read `Driver: libusbK`. Close Zadig.

### 3.4 Switch the service to real-hardware mode and start it

```powershell
# Install the PyLabRobot extras (one-time per PC; safe to re-run).
cd C:\Users\sdl2\Projects\agilent-cytation-server
C:\SDL_Tools\uv.exe sync --extra api --extra plr --extra windows

# Edit config.toml — flip dry_run = false. Optionally set [instrument].usb_serial
# if there are multiple Cytations on this Tailnet.
notepad C:\Users\sdl2\Projects\agilent-cytation-server\config.toml

# Recommended: move the service off LocalSystem so libftdi has a proper
# user environment (some FTDI / libusb interactions read HKCU). Skip this
# step if you do not have the lab user password handy — LocalSystem
# usually works for libusbK, just less reliably.
C:\SDL_Tools\nssm.exe set cytation ObjectName .\sdl2 "<labuser-password>"

# Bring it up
sc.exe start cytation
Start-Sleep -Seconds 5
```

### 3.5 Verify

```powershell
sc.exe query cytation | Select-String STATE                      # RUNNING
curl.exe -fsS http://127.0.0.1:9333/status |
    ConvertFrom-Json |
    Select-Object equipment_status, message, host
# Expected:
#   equipment_status : ready
#   message          : Idle, ready to read

# Confirm a real-hardware metric appears
curl.exe -fsS http://127.0.0.1:9333/status |
    ConvertFrom-Json |
    Select-Object -ExpandProperty metrics |
    Format-List
# 'actual_temperature' should reflect the real incubator, not 37.0 stub.

# Tail logs for any FTDI errors
Get-Content C:\SDL_Logs\cytation.err.log -Tail 30
```

If `/status` shows `requires_init` instead of `ready`, the FTDI chip didn't bind correctly or the FTDI extras didn't install. See [§5 Troubleshooting](#5-troubleshooting).

The dashboard tile (`ac-organic-lab`) will pick up the new state on the next 3 s poll.

---

## 4. Driver swap: libusbK → FTDI (PyLabRobot off, Gen5 on)

Use this when someone needs Gen5 (or any other BioTek/FTDI-vendor-driver-based software) to operate the Cytation. The cytation REST service will go back to `dry_run` so the dashboard tile stays out of `unknown` / `error`.

### 4.1 Pre-flight

```powershell
# Make sure no orchestrator job has the Cytation claimed (v1.1+ only —
# v1.0 has no claim protocol so any in-flight read will just be aborted).

# Stop the cytation service so it releases the libusbK handle.
nssm stop cytation
sc.exe query cytation | Select-String STATE   # expect STOPPED
```

### 4.2 Flip config back to dry_run

This is so when the service auto-starts (next boot or `sc.exe start`) it does **not** try to grab the libusb-bound chip — the chip won't be libusb-bound anymore.

```powershell
# Edit config.toml — set dry_run = true (and clear [instrument].usb_serial if set).
notepad C:\Users\sdl2\Projects\agilent-cytation-server\config.toml
```

### 4.3 Uninstall libusbK from the Cytation device

1. Open **Device Manager** (`devmgmt.msc`) — Run as Administrator if you are not already.
2. Expand the **`libusbK USB Devices`** branch (visible only after a libusbK-bound device is plugged in).
3. Right-click the Cytation entry → **Uninstall device**.
4. **Check the box `Delete the driver software for this device`.** This is the step that lets Windows reapply the FTDI vendor driver next.
5. Click **Uninstall**.
6. **Unplug the Cytation USB cable**, wait 3 seconds, **plug it back in.** Windows Plug-and-Play will auto-rebind it to the FTDI vendor driver from the system driver store. You should see toast notifications, then two new entries:
   - `Universal Serial Bus controllers → USB Serial Converter`  
   - `Ports (COM & LPT) → USB Serial Port (COMx)` (if the FTDI VCP layer is enabled — Gen5 typically does not need it, but it does no harm).

If Windows fails to find a driver at this step, install FTDI CDM from <https://ftdichip.com/drivers/d2xx-drivers/> (one-time) and replug.

### 4.4 Verify Gen5 sees the device

1. Open **Gen5** as the lab user.
2. Connect to the reader through Gen5's connection wizard. If it succeeds: you're done.

### 4.5 Restart our service in dry_run

```powershell
sc.exe start cytation
Start-Sleep -Seconds 5
curl.exe -fsS http://127.0.0.1:9333/status |
    ConvertFrom-Json |
    Select-Object equipment_status
# Expected: dry_run
```

The dashboard tile will return to `dry_run` (yellow/blue, depending on theme) on the next poll. Orchestrator workflows that target the Cytation will fall back to "device unavailable" until you swap back.

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Zadig dropdown is empty | `Options → List All Devices` not checked, or Cytation off | Check the option; power-cycle the Cytation; re-open Zadig |
| Multiple FTDI devices show up | Other FTDI-based instruments on this PC (xArm, etc.) | Use the unplug/replug trick to identify the Cytation |
| Zadig says "Operation in progress" | Our service still has the device handle | `nssm stop cytation`, then retry Zadig |
| `/status` reads `requires_init` after libusbK swap | `--extra plr --extra windows` not installed, or libusbK didn't actually take | `cd C:\Users\sdl2\Projects\agilent-cytation-server; C:\SDL_Tools\uv.exe sync --extra api --extra plr --extra windows`; re-check Device Manager shows `libusbK USB Devices → ...` |
| Service log says `usb.core.NoBackendError: No backend available` | `libusb-package` not installed | `C:\SDL_Tools\uv.exe sync --extra plr --extra windows`, `nssm restart cytation` |
| Service log says `pylabrobot.io.ftdi.FTDIError: device not found` | libusbK swap incomplete, or wrong VID/PID, or device powered off | Check Device Manager + power; re-run Zadig if needed |
| Gen5 cannot connect after swap-back | FTDI vendor driver missing | Install FTDI CDM from ftdichip.com; replug |
| Service status keeps flipping `requires_init ↔ ready` | The cytation is going to sleep / standby, libusbK losing the handle | Disable USB selective suspend for the cytation port, or set the Cytation's idle sleep to `Never` in its menu |
| Dashboard still shows `unknown` after swap | Aggregator cached, or yaml not reloaded | On the dashboard server: restart `ac-dashboard-api.service` per `ac-organic-lab/docs/EQUIPMENT_INTEGRATION.md` |

---

## 6. Updating from a git push

Per `ac-organic-lab/docs/DEVICE_PC_SETUP.md` §4:

```powershell
# Run as Administrator on the lab PC.
cd C:\Users\sdl2\Projects\agilent-cytation-server
git pull
C:\SDL_Tools\uv.exe sync --extra api          # add --extra plr --extra windows if running real HW
nssm restart cytation
sc.exe query cytation | Select-String STATE   # RUNNING
```

If `pyproject.toml` deps changed, `uv sync` updates `.venv` and `nssm restart` rolls the service. Total downtime: ~5 s.

---

## 7. Looking ahead

- **Phase 2** — Per-well sample tracking via PyLabRobot `Container` / `Plate` / `Well`; surfaced in `details.loaded_plate` of `/status`. Requires real-hardware mode (libusbK) for measurements; can be developed and tested in `dry_run` with stubbed plate state.
- **Phase 3** — STATUS_SPEC v1.1: `POST /control/claim`, `/heartbeat`, `/release`, full `/control/*` write surface (drawer, reads, plate load/unload, imaging capture, incubator).
- **Phase 4** — Skill catalog registration in `ac-organic-lab/lab_skills`.

If the daily Gen5 ↔ PyLabRobot toggle becomes painful, the long-term fix is to **patch PyLabRobot's `pylabrobot.io.ftdi.FTDI` to use FTDI's `ftd2xx` driver** instead of libusb. `ftd2xx` coexists with the FTDI vendor driver (it *is* part of the FTDI driver bundle), so both Gen5 and PyLabRobot could share the chip with no Zadig swaps. This is a feature-sized change that needs an upstream PR or a vendored driver shim — deferred until the daily-driver pain is real.
