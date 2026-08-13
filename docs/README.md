# `docs/` — vendor manuals & protocol library

This directory holds reference material for operating the BioTek (Agilent)
Cytation 5 and BioStack 4. Two things live here:

| Path | Tracked in git? | Purpose |
|---|---|---|
| `docs/IMPLEMENTATION.md` | yes | Bench verification plan — what still needs testing on real hardware, what to bring, and what to record. |
| `docs/LABSKILLS.md` | yes | Drop-in `lab-skills` skill-catalog patch for the central server, so workflows can drive this device by role. |
| `docs/PLATE_STATE.md` | yes | How per-well sample tracking works, and the cross-device strategy behind it. |
| `docs/INDEX.md` | yes | Catalogue of which BioTek/Agilent manuals are useful, and where to get the PDFs from BioTek/Agilent. |
| `docs/notes/` | yes | Lab-authored notes — wavelength choices, plate-geometry calibration, Gen5 protocol-file conventions, etc. Anything we wrote ourselves. |
| `docs/vendor/` | **no** (gitignored) | The actual BioTek/Agilent PDFs. They are vendor-copyrighted; do not push them to a public GitHub repo. Drop them here on the lab PC and they stay on the lab PC. |
| `docs/protocols/` | partial | Gen5 protocol files (`.prt`, `.exp`) — gitignored by default, since BioTek's protocol XML is a proprietary format and many `.prt` files include vendor-supplied templates. Lab-authored protocols can be checked in by force-adding them, but inspect first. |

## Quick rules

- If you have a fresh PDF download from BioTek/Agilent's customer portal, put it
  under `docs/vendor/`. It will not be pushed.
- If you author a `.prt` file from scratch in Gen5 and it doesn't include any
  vendor templates, you may force-add it (`git add -f docs/protocols/<name>.prt`)
  — but consider whether the `.prt` discloses anything you don't want public.
- `docs/INDEX.md` is the canonical pointer to "which manual covers which
  question" and "where to download it from".
