"""Conformance tests for the lab equipment status spec v1.0 endpoints.

These tests run with the dry-run stub reader so they require no Windows
/ PyLabRobot / pyusb dependencies and can be executed in CI on any
platform. The reference patterns are
``agilent_plateloc/tests/test_api.py`` (v1.1) and
``xarm-translocation/test/test_status_envelope.py`` (v1.0); this file
follows the v1.0 read-only shape because Phase 1 ships no /control/*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agilent_cytation_server.api import create_app
from agilent_cytation_server.models import PROTOCOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# GET /  --  Probe
# ---------------------------------------------------------------------------


def test_probe(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_id"] == "cytation_5"
    assert body["equipment_name"] == "BioTek Cytation 5"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["protocol_version"] == "1.0"


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


# ---------------------------------------------------------------------------
# GET /openapi.json
# ---------------------------------------------------------------------------


def test_openapi_doc(client: TestClient) -> None:
    """FastAPI auto-publishes /openapi.json — the spec requires it."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schemas = r.json()["components"]["schemas"]
    for required in [
        "EquipmentStatus",
        "ProbeResponse",
        "HealthResponse",
        "ComponentStatus",
        "MetricValue",
        "ErrorInfo",
    ]:
        assert required in schemas, f"OpenAPI doc is missing {required}"


# ---------------------------------------------------------------------------
# GET /status  --  envelope shape & semantics
# ---------------------------------------------------------------------------


def test_status_envelope(client: TestClient) -> None:
    """Spec-required fields exist and have the correct types/shape."""
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()

    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["equipment_id"] == "cytation_5"
    assert body["equipment_kind"] == "plate_reader"
    # In dry_run the lifespan startup completes synchronously and the
    # service settles into "dry_run" before the first /status call.
    assert body["equipment_status"] == "dry_run"
    assert isinstance(body["device_time"], str)
    assert isinstance(body["uptime_seconds"], (int, float))
    assert isinstance(body["allowed_actions"], list)
    assert body["allowed_actions"] == []  # v1.0 — no /control/* yet

    # Components: optics / incubator / plate_stage always; imaging when enabled.
    components = body["components"]
    for required in ["optics", "incubator", "plate_stage", "imaging"]:
        assert required in components, f"missing component: {required}"

    # Metrics: temperature in C, read counter set.
    metrics = body["metrics"]
    assert metrics["actual_temperature"]["unit"] == "C"
    assert metrics["read_count"]["unit"] == "count"

    # Details: drawer / backend / imaging_enabled / loaded_plate.
    details = body["details"]
    for required in ["drawer", "backend", "imaging_enabled", "loaded_plate"]:
        assert required in details
    assert details["loaded_plate"] is None  # Phase 2 fills this in
    assert details["dry_run"] is True


def test_status_is_side_effect_free(client: TestClient) -> None:
    """Spec rule #1: GET /status MUST be side-effect-free.

    Polling repeatedly must not bump the read counter or otherwise
    mutate state.
    """
    r1 = client.get("/status")
    rc1 = r1.json()["metrics"]["read_count"]["value"]
    for _ in range(10):
        client.get("/status")
    r2 = client.get("/status")
    rc2 = r2.json()["metrics"]["read_count"]["value"]
    assert rc1 == rc2 == 0


def test_status_always_200_when_disconnected() -> None:
    """Spec rule #2: /status returns 200 even if hardware isn't ready.

    We force the service into ``requires_init`` by detaching the stub
    reader directly (the v1.1 graduation will replace this with a
    POST /control/shutdown call). The response must still be HTTP 200
    with ``equipment_status: requires_init``.

    NB: we avoid calling ``await service.shutdown()`` from the test
    thread because the service's ``asyncio.Lock`` is bound to the
    TestClient's anyio loop. Mutating ``_reader`` directly is the
    minimum-risk way to model "hardware went away" from a sync test.
    """
    app = create_app(dry_run=True)
    with TestClient(app) as alt:
        app.state.service._reader = None
        r = alt.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert body["equipment_status"] == "requires_init"
        assert "startup" in body["required_actions"]


# ---------------------------------------------------------------------------
# Snapshot fixtures (saved for regression review)
# ---------------------------------------------------------------------------


def _scrub_for_diff(body: dict) -> dict:
    """Replace runtime-volatile fields with stable placeholders so the
    saved fixtures only diff when schema or value semantics change."""
    body["device_time"] = "2026-04-29T22:50:01Z"
    body["uptime_seconds"] = 0.0
    body["host"] = "cytation-pc"
    for metric in body.get("metrics", {}).values():
        if metric.get("timestamp"):
            metric["timestamp"] = "2026-04-29T22:50:01Z"
        # Coerce volatile numeric metrics to deterministic baselines so
        # the fixture only diffs on schema changes.
        if "value" in metric:
            label_metrics_volatile = {"last_read_seconds_ago"}
        # (no-op placeholder — keep structure for readability)
    if "last_read_seconds_ago" in body.get("metrics", {}):
        body["metrics"]["last_read_seconds_ago"]["value"] = 0.0
    return body


def test_save_status_fixtures() -> None:
    """Re-generate ``tests/fixtures/status_*.json``.

    Fixtures are checked into git so reviewers can eyeball schema
    changes. After intentional schema changes, re-run pytest and commit
    the diffs as part of the PR.

    Coverage:
      - status_dry_run.json         — dry_run mode (default)
      - status_requires_init.json   — reader stopped post-startup
      - status_ready.json           — stub reader, dry_run=False
      - status_busy.json            — stub reader with _busy_state=True
    """
    FIXTURES.mkdir(exist_ok=True)

    # ---- dry_run ----------------------------------------------------------
    app = create_app(dry_run=True)
    with TestClient(app) as alt:
        body = alt.get("/status").json()
        assert body["equipment_status"] == "dry_run"
        (FIXTURES / "status_dry_run.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # ---- requires_init (detach reader directly) ----------------------
        # See test_status_always_200_when_disconnected for why we don't
        # call await service.shutdown() from the sync test thread.
        app.state.service._reader = None
        body = alt.get("/status").json()
        assert body["equipment_status"] == "requires_init"
        (FIXTURES / "status_requires_init.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

    # ---- ready / busy: stub reader with dry_run=False so the operational
    # state machine (ready / busy) is exercised --------------------------
    from agilent_cytation_server.reader import StubCytationReader

    app = create_app(dry_run=False)
    # Inject the stub via reader_factory so `is_connected()` flips to True
    # under the real state machine.
    app.state.service._reader_factory = StubCytationReader
    with TestClient(app) as alt:
        body = alt.get("/status").json()
        assert body["equipment_status"] == "ready"
        (FIXTURES / "status_ready.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )

        # Flip the busy flag and snapshot.
        app.state.service._busy_state = True
        body = alt.get("/status").json()
        assert body["equipment_status"] == "busy"
        (FIXTURES / "status_busy.json").write_text(
            json.dumps(_scrub_for_diff(body), indent=2, sort_keys=True) + "\n"
        )


# ---------------------------------------------------------------------------
# Defensive: never advertise a kind / id that mismatches equipment.yaml
# ---------------------------------------------------------------------------


def test_equipment_id_matches_registry(client: TestClient) -> None:
    """The dashboard's `equipment.yaml` joins on `equipment_id`. If we
    silently rename it the tile goes orphaned. This test is the local
    guardrail; the registry-side guard lives in
    ac-organic-lab/skills/tests/test_registry.py.
    """
    body = client.get("/status").json()
    assert body["equipment_id"] == "cytation_5"
    assert body["equipment_kind"] == "plate_reader"


@pytest.mark.parametrize(
    "imaging_enabled, expect_imaging",
    [(True, True), (False, False)],
)
def test_imaging_component_visibility(imaging_enabled: bool, expect_imaging: bool) -> None:
    """The `imaging` component appears iff `[imaging].enabled = true`.

    Renaming components is documented as a breaking change in
    STATUS_SPEC §"Best Practices" #14, so we want a test that protects
    the visibility rule.
    """
    app = create_app(dry_run=True)
    app.state.service.imaging_enabled = imaging_enabled
    with TestClient(app) as alt:
        body = alt.get("/status").json()
        assert ("imaging" in body["components"]) is expect_imaging
        assert body["details"]["imaging_enabled"] is imaging_enabled
