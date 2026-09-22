"""Agent discovery must work without startup or hardware access.

Covers both documentation surfaces: the versioned JSON guide at
``/docs/agent`` and the lab-standard Markdown surface (``/agent-docs``,
``/agent-docs/api-reference``, ``/llms.txt``).
"""

import re
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agilent_cytation_server.api import create_app
from agilent_cytation_server.documentation import router
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
    assert guide["documentation_version"] == "1.1.0"
    assert guide["protocol_version"] == "1.2"
    schema = client.get("/openapi.json").json()
    assert guide["action_schemas"]["paths"] == {
        path: operations for path, operations in schema["paths"].items()
        if path.startswith("/control/")
    }
    assert guide["action_schemas"]["components"] == schema["components"]
    for key in ("brightfield_imaging_4x", "fluorescence_imaging"):
        assert guide["capabilities"][key]["validation_status"] == "pending_installation_and_validation"


def test_agent_docs_routes(client: TestClient) -> None:
    """The Markdown surface needs no claim, even with claims enforced."""

    r = client.get("/agent-docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "allowed_actions" in r.text and "shutdown" in r.text
    r = client.get("/agent-docs/api-reference")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "/control/read/absorbance" in r.text
    assert "/control/imaging/capture" in r.text
    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in r.text and "(openapi.json)" in r.text


@pytest.mark.parametrize("prefix", ["", "/cytation", "/api/equipment/test/documentation"])
def test_index_links_without_hardware(prefix: str) -> None:
    """`llms.txt` links must resolve under any mount prefix, so they stay relative."""

    service = FastAPI()
    service.include_router(router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as http:
        response = http.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == {"agent-docs", "agent-docs/api-reference", "openapi.json"}
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
            assert http.get(resolved).status_code == 200


def test_openapi_lists_documentation_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt", "/docs/agent"} <= set(paths)


def test_api_reference_documents_every_route(client: TestClient) -> None:
    """The reference is only useful if it is exhaustive — so assert that it is."""

    reference = client.get("/agent-docs/api-reference").text
    paths = client.get("/openapi.json").json()["paths"]
    missing = [path for path in paths if path not in reference]
    assert not missing, f"undocumented routes: {missing}"
