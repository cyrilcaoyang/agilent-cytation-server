"""Agent discovery must work without startup or hardware access."""

from fastapi.testclient import TestClient

from agilent_cytation_server.api import create_app
from agilent_cytation_server.service import CytationService


def test_agent_docs_without_startup_or_hardware(monkeypatch, plate_state):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Documentation must not contact hardware")

    monkeypatch.setattr(CytationService, "startup", forbidden)
    monkeypatch.setattr(CytationService, "get_status", forbidden)
    app = create_app(dry_run=True, plate_state=plate_state)
    # No lifespan: discovery is available even before device startup.
    client = TestClient(app)
    response = client.get("/docs/agent")
    assert response.status_code == 200
    guide = response.json()
    assert guide["documentation_version"] == "1.0.0"
    assert guide["protocol_version"] == "1.2"
    schema = client.get("/openapi.json").json()
    assert guide["action_schemas"]["paths"] == {
        path: operations for path, operations in schema["paths"].items()
        if path.startswith("/control/")
    }
    assert guide["action_schemas"]["components"] == schema["components"]
    for key in ("brightfield_imaging_4x", "fluorescence_imaging"):
        assert guide["capabilities"][key]["validation_status"] == "pending_installation_and_validation"
