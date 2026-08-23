# Camera driver migration — Spinnaker/PySpin → Aravis

**Status:** plan only. Nothing here has been executed. Researched 2026-08-23.

This covers the **camera** (microscopy) driver. It is unrelated to the
FTDI ↔ libusbK swap on the **reader** link documented in [`RUNBOOK.md`](../RUNBOOK.md)
§4/§5 — but §"The Windows problem" below explains why the two end up entangled
on this PC, which is the main reason this document exists.

---

## 1. What upstream actually did

| | |
|---|---|
| PR | [#985](https://github.com/PyLabRobot/pylabrobot/pull/985), authored by `vcjdeboer`, hardware-tested on a Cytation 1 with a BlackFly BFLY-U3-13S2M |
| Merged | into branch `v1b1` on 2026-04-11 |
| Reached `main` | via PR #1000 ("v1b1 changes"), 2026-08-01 |
| Released? | **No.** PyPI latest is 0.2.2 (2026-07-30), which predates the merge. We pin 0.2.1. |

Stated motivation, which matches what the developer told us directly: Aravis is
open source, so there is no Spinnaker account and no SDK download; it also drops
the Python 3.10 cap and the `numpy<2` pin that PySpin's C ABI forces on us.

### The module tree moved

```
before (what we import today)          after (main)
pylabrobot/plate_reading/agilent/      pylabrobot/agilent/biotek/cytation/
  biotek_cytation_backend.py             base.py            _CytationBase
                                         cytation1.py
                                         cytation5.py       Cytation5(_CytationBase)
                                         microscope/
                                           aravis_camera.py AravisCamera
                                           microscope.py    CytationMicroscope
                                           models.py
```

Our current import path still exists on `main` — as
`pylabrobot/legacy/plate_reading/agilent/biotek_cytation_backend.py`. **PySpin is
referenced in exactly one file on `main`, and that is it.** The camera path we
use is now the legacy path by upstream's own naming.

---

## 2. This is three coupled changes, not one

**a. Version.** Aravis is unreleased. Adopting it means pinning a git SHA of
`main` rather than a PyPI version, and living with `main`'s churn until a
release ships.

**b. API restructure.** PR #1000 removed the frontend/backend split we are
built on. `reader.py` currently does:

```python
backend = CytationBackend(device_id=...)
self._reader = PlateReader(name="cytation_5", size_x=..., backend=backend)
await self._reader.setup(use_cam=True)
...
await backend._acquire_image()
```

The v1 shape is one device object with the camera hanging off it:

```python
class Cytation5(_CytationBase):
    def __init__(self, name, camera_serial=None, device_id=None,
                 imaging_config=None, use_cam=True):
        self.microscope = CytationMicroscope(driver=self, ...)
```

So `CytationReader.setup()`, the `_acquire_image()` reach-through at
`reader.py:879`, and the camera-failure handling around `reader.py:249-282` all
have to be rewritten. **Our own coupling to PySpin is nil** — we never import
it; every Spinnaker specific lives inside PyLabRobot. That is the one part of
this migration that is genuinely easy.

**c. Aravis itself**, which on Windows is the hard part.

---

## 3. The Windows problem

Upstream's install instructions cover macOS and Linux only:

```
macOS: brew install aravis
Linux: sudo apt-get install libaravis-dev gobject-introspection
then:  pip install "pylabrobot[cytation-microscopy]"
```

There is no Windows path documented, and the PR discussion contains an explicit
"I read some Linux over Windows preference though, which might be a worry."

Two concrete consequences for `sdl2-pc-03-cytation`:

1. **Aravis reaches USB3 Vision cameras through libusb.** On Windows that means
   the BlackFly must be bound to WinUSB (or libusbK / libusb-win32) with Zadig —
   which **displaces FLIR's filter driver**. SpinView stops seeing the camera,
   and so does anything else built on Spinnaker. This is the same
   one-driver-at-a-time trap as the FTDI chip, on a second device, and it would
   make **two** independent Zadig-bound bindings on this PC. Note RUNBOOK §3.1
   uses SpinView as the camera verification tool; that step would need replacing.
2. **PyGObject is not a pip install on Windows.** It needs the MSYS2 GTK stack
   plus gobject-introspection and a matching `gi.require_version("Aravis", "0.8")`
   typelib. That is a substantially heavier host dependency than the Spinnaker
   installer it replaces — the "easier to install" argument holds on macOS and
   Linux and inverts on Windows.

**Nothing in the upstream code prevents Windows** — `aravis_camera.py` guards the
import with a `HAS_ARAVIS` flag and is otherwise platform-neutral. It is simply
unproven there, and we would be the ones proving it.

---

## 4. What we gain and lose

**Gain**

- No Spinnaker account, no SDK download, no per-CPython-minor PySpin wheel.
- Kills the trap in RUNBOOK §3.1: PySpin sits outside the lockfile because FLIR
  does not publish to PyPI, so a plain `uv run` prunes it and silently disarms
  the camera. That is why the service runs `uv run --no-sync` today. An Aravis
  path is fully declarable in `pyproject.toml`, so `--no-sync` could go.
- Drops the `numpy<2` pin and the Python 3.10 cap. This repo is pinned to 3.10
  *solely* for PySpin.

**Lose**

- A supported release; we would be on a `main` SHA.
- SpinView as a diagnostic.
- A second Zadig-bound USB device to remember during any future driver work.

---

## 5. Recommended sequencing

Do **not** start on the live PC.

**Phase 0 — decide the trigger.** The honest recommendation is to wait for a
PyPI release containing v1, unless the Spinnaker install pain becomes acute
(a new PC to provision, or a Python upgrade blocked by the 3.10 cap). Nothing
about the current imaging path is broken; the 152 frames in
`captures/solubility_monitor_20260821T2000/` were taken with it.

**Phase 1 — prove Aravis sees the BlackFly on Windows.** Timeboxed spike,
reversible, no repo changes:

1. MSYS2 + `pacman -S mingw-w64-x86_64-aravis` (+ gobject-introspection).
2. Zadig-bind the BlackFly to WinUSB. **Export the existing FLIR binding first**
   — `pnputil /export-driver <oem#>.inf <dir>` into a directory that already
   exists, and confirm the export reported `Exported driver packages: 1` before
   deleting anything. (A skipped export cost us the libusbK package on
   2026-08-23; it was only recoverable because Zadig had left its generated
   package in `C:\Users\sdl2\usb_driver\`.)
3. `arv-tool-0.8` should enumerate the camera. If it does not, stop — the rest
   of this plan is moot.
4. Revert the Zadig binding and confirm SpinView works again.

**Phase 2 — port `reader.py` to the v1 API** behind a git pin, in dry-run,
off the instrument. Tests must stay green; the stub in `reader.py` is what makes
this possible without hardware.

**Phase 3 — carry the temperature-range correction forward.** See §6.

**Phase 4 — RUNBOOK.** Add a camera-driver-binding section, revise §3.1
(SpinView, the PySpin wheel, the `--no-sync` rationale), and record that this PC
now has two Zadig-bound devices.

Each phase is independently revertible; do not compress them.

---

## 6. Do this first, regardless of Aravis

The v1 tree carries the **same wrong incubator range** we just disproved —
`pylabrobot/agilent/biotek/plate_reader_base.py:177`:

```python
@property
def temperature_range(self) -> Tuple[Optional[float], Optional[float]]:
    max_temp = 45.0 if self.supports_heating else None   # "default BioTek max"
    min_temp = 4.0 if self.supports_cooling else None    # supports_cooling is hardcoded True
    return (min_temp, max_temp)
```

Our unit reports, via Gen5 `GetReaderCharacteristics` (serial 23030927,
read 2026-08-23):

| characteristic | value |
|---|---|
| `eTemperatureControlOption` | `True` |
| `eTemperatureMin` | **18** |
| `eTemperatureMax` | **65** |
| `eTemperatureGradientMax` | 2 (spatial lid gradient, not a ramp) |

So both ends of the hardcode are wrong for at least the Cytation 5, and
`supports_cooling = True` manufactures a 4 °C floor no BioTek incubator of this
class has. We work around it locally with the `_RangeCorrectedBackend` subclass
in `reader.py::setup()`; that workaround survives the migration unchanged
because the hardcode did.

This is a small, well-evidenced upstream PR that benefits every Cytation user
and lets us delete our subclass. It is independent of the Aravis work and
should not wait for it.

---

## See also

- [`RUNBOOK.md`](../RUNBOOK.md) §1 (why driver swaps exist), §3.1 (Spinnaker /
  PySpin bring-up), §4–§5 (the reader-side FTDI ↔ libusbK swap)
- [PyLabRobot PR #985](https://github.com/PyLabRobot/pylabrobot/pull/985) —
  the Aravis camera driver
- [Cytation camera driver](https://discuss.pylabrobot.org/t/cytation-camera-driver/483)
  — the forum thread where it was designed
- [PyLabRobot installation docs](https://docs.pylabrobot.org/user_guide/_getting-started/installation.html)
