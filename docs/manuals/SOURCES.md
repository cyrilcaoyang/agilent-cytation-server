# Cytation manuals & data sheets (downloaded 2026-08-23)

The PDFs in this folder are vendor-copyrighted and gitignored
(`docs/manuals/*.pdf`), same policy as `docs/vendor/`. This file records
where they came from and the load-bearing facts they establish, so the
evidence survives even where the PDFs don't.

| File | Source | Key fact |
|---|---|---|
| `cytation5_instructions_for_use.pdf` | [Brown Univ. core-facility copy](https://biomedcorefacilities.brown.edu/sites/default/files/biotekcytation5usermanual.pdf) (BioTek IFU) | Specs section: "Incubation: Temperature control ranges from 4 °C over ambient to 65 °C." No imaging-configuration caveat anywhere in the manual; the only heat-gated subsystem is the alpha laser module ("disabled above an internal instrument temperature of 35 °C"), not fitted on our unit. |
| `cytation5_datasheet.pdf` | [Agilent data sheet, Chemetrix mirror](https://chemetrix.co.za/wp-content/uploads/2024/02/Agilent-BioTek-Cytation-5-Data-Sheet.pdf) | "All configurations include incubation to 65 °C and shaking" — imaging-fitted included. |
| `cytation1_datasheet.pdf` | [Agilent data sheet, Chemetrix mirror](https://chemetrix.co.za/wp-content/uploads/2024/02/Agilent-BioTek-Cytation-1-Data-Sheet.pdf) | "4-Zone Incubation to 45 °C with Condensation Control" — the 45 °C ceiling belongs to the Cytation 1. |
| `cytationC10_datasheet.pdf` | [Agilent data sheet, Chemetrix mirror](https://chemetrix.co.za/wp-content/uploads/2024/02/Agilent-BioTek-Cytation-C10-Data-Sheet.pdf) | "4-Zone Incubation to 45 °C with Condensation Control" — likewise the C10 confocal. |

Agilent's own hosting of the Cytation 5 tech-details PDF
(`agilent.com/cs/library/specifications/public/Cytation-5-technical-details-5994-3580EN-agilent.pdf`)
403s non-browser clients; the Chemetrix mirror above is byte-for-byte the
same document series.

## Where our 45 °C API ceiling actually comes from

It is **not** a Cytation 5 hardware limit. The chain:

1. PyLabRobot's shared base class hardcodes it:
   `BioTekPlateReaderBackend.temperature_range` →
   `max_temp = 45.0 if self.supports_heating else None  # default BioTek max`
   (and `min_temp = 4.0` for cooling). Introduced in upstream PR
   [#757](https://github.com/PyLabRobot/pylabrobot/pull/757) ("Synergy H1
   backend", 2025-12-02, commit `4663d6b6`), which refactored a common
   BioTek base class. The "default BioTek max" of 45 matches the Cytation
   1 / C10 (and other small BioTek readers) — not the Cytation 5's 65 °C.
2. `CytationBackend` overrides only `supports_heating` / `supports_cooling`
   (both `True`, which also manufactures the 4 °C cooling floor our unit
   almost certainly can't reach) and never overrides `temperature_range`,
   so every Cytation inherits (4.0, 45.0).
3. Our `control_args.py::TemperatureArgs` (`ge=4.0, le=45.0`) deliberately
   mirrors "the driver's" bounds, and `reader.set_temperature` re-checks
   the driver range — so the 45 is enforced twice on our side of the wire.

Consequence: raising the API bound alone is not enough; the backend's
`temperature_range` must be overridden too (thin subclass at the
`CytationBackend(**backend_kwargs)` construction site in `reader.py`).
`docs/IMPLEMENTATION.md` §5 and `docs/INDEX.md` already flag the
range discrepancy; this folder holds the primary sources.

Note on "60 °C is bad for the imaging unit": no Agilent/BioTek document
we could find says this for the Cytation 5 — it matches the Cytation 1 /
C10 spec instead. And skipping image capture would not protect the optics
anyway: the objectives, cubes, and camera sit in the same enclosure and
heat-soak with the chamber regardless of whether the camera fires. If a
credible C5-specific warning surfaces, confirm with Agilent support
against our serial number before running above 45 °C.
