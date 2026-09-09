"""Versioned, hardware-independent guidance for agent clients."""

from typing import Any

from .models import PROTOCOL_VERSION

DOCUMENTATION_VERSION = "1.0.0"


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
