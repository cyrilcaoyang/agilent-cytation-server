# BioTek / Agilent reference index

Catalogue of vendor documents that are useful for operating, integrating, or
debugging the Cytation 5 + BioStack 4 + Gen5 stack from this repository.

The PDFs themselves are **not committed** — they live under `docs/vendor/` on
the lab PC (gitignored). This index just tells you which doc to reach for.

> Where to find them: most are on Agilent's customer portal under the BioTek
> product family pages, or on the original BioTek support site
> (`biotek.com/support/...`, now redirected by Agilent). Some come on USB sticks
> shipped with the instrument. If you cannot locate one, contact Agilent
> support with the product number.

---

## Cytation 5 — multi-mode reader (numeric reads)

| Doc | Filename convention | Use it for |
|---|---|---|
| **Cytation 5 Operator's Manual** | `cytation5_operators_manual.pdf` | Overall instrument operation, drawer/incubator/optics layout, wavelength ranges, accuracy specs, error code list. |
| **Cytation 5 Specifications Sheet** | `cytation5_specsheet.pdf` | Quick reference: λ range (abs 230–999 nm, fluo ex 250–700 / em 300–700), shaker settings, incubator range (4 °C above ambient → 65 °C). |
| **Cytation 5 Software Installation Guide** | `cytation5_installation_guide.pdf` | Cable diagrams, USB topology, Gen5 install, driver notes. **Useful when reasoning about the dual-USB topology** — see also `RUNBOOK.md` §2. |

## Cytation 5 — Imaging module (microscopy)

| Doc | Filename convention | Use it for |
|---|---|---|
| **Cytation 5 Imaging User Guide** | `cytation5_imaging_user_guide.pdf` | LED-channel definitions (DAPI/UV, GFP, RFP, Cy5, etc.), filter-cube part numbers, focal-height ranges per objective, focusing strategies (laser autofocus vs image-based AF), brightfield/phase setup. |
| **Cytation Objective & Filter Cube Catalog** | `cytation_objective_filter_catalog.pdf` | Part numbers for installed objectives (4×, 10×, 20×, 40× PL/Apo) and filter cubes. Needed when configuring `imaging.capture(channel=..., objective=...)` payloads. |
| **FLIR/Point Grey Blackfly Camera Datasheet** | `flir_blackfly_*.pdf` | Pixel size, sensor model, max FPS, exposure range. Useful when calibrating exposure_ms / gain_db parameters. |
| **Spinnaker SDK Programmer's Guide** | `spinnaker_sdk_guide.pdf` | Camera control via PySpin (the path PyLabRobot uses). Reference when debugging "camera not found" errors. |

## Gen5 software

| Doc | Filename convention | Use it for |
|---|---|---|
| **Gen5 User Manual** | `gen5_user_manual.pdf` | Authoring `.prt` protocol files in the GUI (which we use as templates for the `biotek_driver` backend), partial-plate selection, exporting results to CSV/XML. |
| **Gen5 Liaison COM API Reference** | `gen5_liaison_api.pdf` | Programmatic Gen5 automation (the COM/.NET surface that some third-party drivers wrap). |
| **Gen5 XML Protocol Schema** | `gen5_protocol_xml_schema.pdf` (or `.xsd`) | Reverse-engineering and patching `.prt` files at runtime — wavelength substitution, partial-plate XML structure (`<PartialPlate>...</PartialPlate>`). The North-Cytation `biotek_driver.xml_builders.partial_plate_builder` constructs this XML. |

## BioStack 4 — plate stacker

| Doc | Filename convention | Use it for |
|---|---|---|
| **BioStack 4 Operator's Manual** | `biostack4_operators_manual.pdf` | Hardware operation, alignment, plate-flipper option, dimensional capacity (50 plates standard). |
| **BioStack Communications Reference** | `biostack_serial_command_reference.pdf` | **Required for `agilent-biostack-server`.** Documents the ASCII serial command set over RS-232 (baud, framing, command list — `RUN`, `IN`, `OUT`, `RD`, `RR`, `WP`, status codes, error codes). |
| **BioStack PC Software User Guide** | `biostack_pc_software.pdf` | The vendor PC client (BioStack Diagnostic Tool), useful for sniffing what the serial commands look like on the wire when reverse-engineering. |

## Reader Control / driver-level

| Doc | Filename convention | Use it for |
|---|---|---|
| **BioTek Reader Control / Liaison API** | `biotek_reader_control_api.pdf` | Lower-level reader command set — relevant if we ever switch off Gen5 and talk to the reader directly (PyLabRobot's `BioTekPlateReaderBackend` essentially does this over FTDI). |

---

## How this maps to our service code

| Doc | Touches what in this repo |
|---|---|
| Cytation 5 Operator's Manual + Specs | `src/agilent_cytation_server/reader.py` (capability discovery), `config.example.toml` (λ ranges, plate models). |
| Imaging User Guide + Objective/Filter Catalog | future `src/agilent_cytation_server/imaging.py` (channel/objective enums, exposure validation). |
| Gen5 XML Protocol Schema + Partial Plate | future `biotek_driver`-backend code path (template library, partial-plate XML rewriter). |
| BioStack Communications Reference | a separate repo `agilent-biostack-server` (planned, port 9334), not this one. |

## Adding a doc

1. Drop the PDF in `docs/vendor/<filename>.pdf`. It will be gitignored.
2. If it's a *new* type of doc, add a row to the table above so future people
   know it exists.
3. If it includes information a public API consumer needs (e.g. valid wavelength
   ranges, channel names), surface that information in `docs/notes/<topic>.md`
   (which IS tracked) — paraphrased, not pasted, so we don't redistribute
   copyrighted text.
