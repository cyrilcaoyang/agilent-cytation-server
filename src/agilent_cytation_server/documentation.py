"""Agent documentation: the versioned JSON guide plus the Markdown surface.

Two things live here.

:func:`equipment_documentation` builds the versioned JSON guide served at
``GET /docs/agent`` — capability validation status, agent boundaries, and the
``/control/*`` action schemas taken from the app's own OpenAPI document.

:data:`router` serves the lab's standard Markdown documentation surface, the
same shape as the other device services (torry-pines-shaker-server,
sense-every-zone, mt-xpr-balance-server): a Markdown agent guide, a Markdown
API reference, and a plain-text ``/llms.txt`` index, so an agent — or the
dashboard's API reference page — can discover how to drive this instrument
without reading the repo.
"""

from importlib.resources import files
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .models import PROTOCOL_VERSION

DOCUMENTATION_VERSION = "1.1.0"

router = APIRouter(tags=["documentation"])


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


def _document(name: str) -> str:
    return files("agilent_cytation_server").joinpath("docs", name).read_text(encoding="utf-8")


@router.get("/agent-docs", response_class=MarkdownResponse, summary="Agent guide (Markdown)")
async def agent_guide() -> str:
    return _document("AGENT_GUIDE.md")


@router.get(
    "/agent-docs/api-reference",
    response_class=MarkdownResponse,
    summary="API reference (Markdown)",
)
async def api_reference() -> str:
    return _document("API_REFERENCE.md")


@router.get("/llms.txt", response_class=PlainTextResponse, summary="Discovery index for agents")
async def llms_txt() -> str:
    # Links are relative on purpose: the service is mounted behind a prefix on
    # the dashboard's documentation proxy, and an absolute "/agent-docs" would
    # resolve off the mount.
    return (
        "# BioTek (Agilent) Cytation 5 plate reader (STATUS_SPEC v1.2 device service)\n\n"
        "## Documentation\n\n"
        "- [Agent guide](agent-docs): health vs activity, claims, startup/shutdown "
        "semantics, preconditions, refusal codes.\n"
        "- [API reference](agent-docs/api-reference): every route with bodies and "
        "refusal codes.\n"
        "- [OpenAPI](openapi.json): request/response schemas.\n"
        # Deliberately NOT linked: `/docs/agent` (the versioned JSON guide) is served
        # by the main app, not by this router, so it is outside the dashboard's
        # documentation proxy and a link to it would 404 there. Named in prose instead.
        "\nThe versioned JSON guide, with per-capability validation status and the "
        "measurement recipe, is served by the device itself at `/docs/agent`.\n\n"
        "## Measurement limits worth knowing before you plan a read\n\n"
        "- **No spectrum verb.** One wavelength per absorbance call, one ex/em pair "
        "per fluorescence call; a range request is refused with 422. A sweep is one "
        "call per point, and must heartbeat because the claim TTL caps at 600 s.\n"
        "- **Emission stops at 700 nm** (ex and em are both 250-700). Above it is not "
        "measurable on this path.\n"
        "- **Luminescence cannot read a region whose maximum corner is H12**, the whole "
        "plate included; absorbance and fluorescence read H12 normally.\n"
        "- Absorbance and fluorescence are bench-verified (2026-09-22); luminescence is "
        "not verified on this instrument.\n\n"
        "Full detail and the measurements behind each limit are in the API reference.\n\n"
        "## Live status\n\n"
        "Read the service's `GET /status` through the lab-skills SDK or dashboard; "
        "live status is not a documentation-proxy resource. Read allowed_actions "
        "before acting.\n"
    )


def equipment_documentation(openapi: dict[str, Any]) -> dict[str, Any]:
    """Use the app's schemas so published arguments track the implemented API."""
    return {
        "documentation_version": DOCUMENTATION_VERSION,
        "updated_at": "2026-09-22",
        "equipment_id": "cytation_5",
        "protocol_version": PROTOCOL_VERSION,
        "api_version": openapi["info"]["version"],
        "scope": "Documentation baseline, not live hardware readiness or validation evidence.",
        "discovery": {
            "status": "/status", "openapi": "/openapi.json", "interactive_docs": "/docs",
        },
        "agent_boundaries": [
            "Read /status for current health, activity, claims and allowed_actions.",
            "Use the lab-skills SDK for hardware operations; do not call raw /control/* endpoints.",
            "Execution requires a validated, human-approved plan merged to main and recorded in BitacoraDB.",
            "Respect claims and interlocks. Stop and report refusals; do not adjust parameters to bypass them.",
            "Store measurements and images in BitacoraDB, not git.",
        ],
        "measurement_recipe": {
            "summary": "How to take a wavelength sweep through this API. Verified end to end "
                       "2026-09-22: 46 absorbance + 31 emission reads x 9 wells, 693 values.",
            "steps": [
                "1. GET /status. Require equipment_status ready, activity idle, and the verb in "
                "allowed_actions. `details.plate_in_reader` must be true - that, not "
                "`loaded_plate`, is what permits a read.",
                "2. POST /control/plate/load with plate_id AND an explicit model. Omitting model "
                "falls back to the last model for that plate_id, then to [plates].default_model "
                "(custom_96, 14.5 mm) - wrong for a taller plate, and wrong silently.",
                "3. POST /control/claim {owner, session_id, ttl_s}. ttl_s is clamped to 1-600, so "
                "any sweep longer than 10 minutes MUST heartbeat. The response carries "
                "heartbeat_interval_s (ttl/3).",
                "4. POST /control/heartbeat with X-Claim-Token between reads. A 46-point "
                "absorbance sweep runs ~11 min and a 31-point emission sweep ~12 min - both "
                "outrun the maximum TTL on their own.",
                "5. One POST per wavelength to /control/read/{absorbance,fluorescence}. Collect "
                "as you go; do not batch the whole sweep before persisting.",
                "6. POST /control/release when done, including on failure.",
            ],
            "costs_measured_2026_09_22": [
                "absorbance: ~14 s per read of a 9-well rectangle.",
                "fluorescence: ~20 s, rising to ~35 s when the region is padded (below).",
                "Gen5 is ~77x faster per measurement but cannot read fewer than 96 wells, so for "
                "a small region this path is the faster one.",
            ],
            "read_the_response_correctly": [
                "A null value in `wells` ALWAYS means over-range, never 'not measured', and the "
                "well is named in `over_range`. On a dilution series the saturated wells are the "
                "most concentrated points, so treating null as missing fits a curve to the tail "
                "and reports a confident wrong slope.",
                "Padding may cause extra wells to be read; the response contains only the wells "
                "you asked for.",
            ],
            "silent_pitfalls": [
                "Some commands are refused by the instrument because their checksum lands in a "
                "rejected band. The driver pads the read region to avoid it, which is why a "
                "padded read takes longer. If a read ever returns HTTP 503 naming status 2D06, "
                "that guard has been bypassed or regressed - report it, do not retry blindly.",
                "Reads are withheld while the shaker runs: they share the serial link.",
                "Reads are refused with 412 while the drawer is open. Close it first; the call is "
                "idempotent.",
            ],
        },
        "plate_requirements": [
            "Confirm the physical plate and drawer state with the operator; registered state is not a presence sensor.",
            "Register the correct plate identity and geometry before reads or imaging; close the drawer.",
            "A restored plate registration requires physical confirmation before use.",
            "Never declare a shorter plate to bypass focal-height restrictions.",
        ],
        "capabilities": {
            "absorbance": {"validation_status": "bench_verified", "limitations": [
                "Bench comparison with Gen5 does not validate every wavelength, plate or assay.",
                "Over-range wells are returned as null and listed in over_range; never interpret null as zero.",
                "ONE wavelength per call. There is no spectrum or scan verb: wavelength_nm is a "
                "single value and the body rejects unknown fields, so a start/end/step request is "
                "refused with 422. A sweep is one call per wavelength - see the sweep section of "
                "the API reference.",
                "Verified 2026-09-22: 46 wavelengths (350-800 nm, 10 nm) x 9 wells returned "
                "finite, wavelength-ordered values on a 19 mm square-well plate.",
            ]},
            "fluorescence_plate_read": {"validation_status": "bench_verified", "limitations": [
                "Verified 2026-09-22: 31 emission wavelengths (400-700 nm, 10 nm) at 360 nm "
                "excitation x 9 wells, no refusals and no saturation, on a 19 mm plate. "
                "Quantitative calibration against a reference standard remains unverified - "
                "values are raw RFU, comparable within a run, not across gains or instruments.",
                "EMISSION AND EXCITATION ARE BOTH LIMITED TO 250-700 nm. A request outside that "
                "range is refused with 422. This is a HARDWARE ceiling, not a driver choice - the "
                "Cytation 5 spec sheet gives emission as 300-700 nm - so emission above 700 nm is "
                "not measurable on this instrument by any route, Gen5 included.",
                "Emission must sit ~20-30 nm redder than excitation or the monochromator passes "
                "scattered excitation light and the read measures the lamp.",
                "ONE ex/em pair per call. No scan verb - see absorbance above and the sweep "
                "section of the API reference.",
                "This is distinct from fluorescence microscopy.",
            ]},
            "luminescence": {"validation_status": "partially_validated", "limitations": [
                "ANY region whose maximum corner is exactly H12 fails with 503, including the "
                "whole plate (A1..H12). Measured 2026-08-31: H11, G12, A1..H11 and A1..G12 all "
                "read fine; H12, H12+H11, G11..H12 and A1..H12 all fail. Read around the corner "
                "- A1..H11 plus A1..G12 covers 95 of 96 wells - or read H12 by absorbance or "
                "fluorescence, both of which return it normally.",
                "This is NOT the rejected-checksum band and is not fixed by the padding that "
                "guards absorbance and fluorescence: the band predicts the opposite of the "
                "observed failures here, so luminescence is deliberately left unpadded.",
                "Unverified since that session - no luminescence read has been taken on this "
                "instrument through the current service.",
            ]},
            "brightfield_imaging_4x": {
                "validation_status": "pending_installation_and_validation",
                "limitations": ["4x lens implementation and optical validation are pending operator confirmation.",
                                "Camera readiness and configured objective names do not prove installed, focused optics."],
            },
            "fluorescence_imaging": {
                "validation_status": "pending_installation_and_validation",
                "limitations": ["Fluorescence imaging implementation and validation are pending.",
                                "Confirm installed filter cubes, channels and objectives against live status and the physical instrument."],
            },
        },
        "maintenance": {
            "source": "src/agilent_cytation_server/documentation.py",
            "version_policy": "Semantic versioning: major for breaking guide structure; minor for capability or validation changes; patch for corrections.",
            "after_imaging_implementation": [
                "Update 4x and fluorescence imaging entries independently after installation and bench validation.",
                "Record validated objectives, filter/channel combinations and limitations, with BitacoraDB evidence references.",
                "Bump documentation_version and updated_at; update tests and deploy through the normal review process.",
                "Do not mark a capability validated solely because a camera connects or a command succeeds.",
            ],
        },
        "action_schemas": {
            "paths": {path: operations for path, operations in openapi["paths"].items()
                      if path.startswith("/control/")},
            "components": openapi.get("components", {}),
        },
    }
