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

DOCUMENTATION_VERSION = "1.0.0"

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
        "- [OpenAPI](openapi.json): request/response schemas.\n\n"
        "## Live status\n\n"
        "Read the service's `GET /status` through the lab-skills SDK or dashboard; "
        "live status is not a documentation-proxy resource. Read allowed_actions "
        "before acting.\n"
    )


def equipment_documentation(openapi: dict[str, Any]) -> dict[str, Any]:
    """Use the app's schemas so published arguments track the implemented API."""
    return {
        "documentation_version": DOCUMENTATION_VERSION,
        "updated_at": "2026-09-09",
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
            ]},
            "fluorescence_plate_read": {"validation_status": "partially_validated", "limitations": [
                "Plate-reader fluorescence commands have run; quantitative calibration remains unverified.",
                "This is distinct from fluorescence microscopy.",
            ]},
            "luminescence": {"validation_status": "partially_validated", "limitations": [
                "Regions ending at H12, including full-plate reads, have a known unresolved failure.",
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
