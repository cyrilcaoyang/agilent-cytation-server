"""Incubator and shaker control, and how they show up on /status.

These two subsystems were supported by the driver but entirely absent from
the service until 2026-08-12. The behaviours worth pinning are less about
the happy path than about how they interact with the v1.2 activity model:
shaking outlives the request that starts it, while holding a temperature
setpoint is a maintained condition rather than an operation.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Incubator
# ---------------------------------------------------------------------------


def test_set_temperature_shows_setpoint_and_state(advisory_client) -> None:
    r = advisory_client.post(
        "/control/incubator/set_temperature", json={"celsius": 37.0}
    )
    assert r.status_code == 204

    s = advisory_client.get("/status").json()
    assert s["metrics"]["setpoint_temperature"]["value"] == 37.0
    assert s["components"]["incubator"]["state"] == "at_setpoint"


def test_incubator_is_off_not_at_setpoint_when_never_commanded(advisory_client) -> None:
    """A warm room is not a running incubator.

    The previous heuristic called any reading >= 30 C `at_setpoint`, which
    reported a heating incubator on a device whose heater was never switched
    on — and could never distinguish "ramping" from "arrived".
    """

    s = advisory_client.get("/status").json()
    assert s["components"]["incubator"]["state"] == "off"
    assert "setpoint_temperature" not in s["metrics"]


def test_stop_temperature_control_clears_the_setpoint(advisory_client) -> None:
    advisory_client.post("/control/incubator/set_temperature", json={"celsius": 37.0})
    assert advisory_client.post("/control/incubator/stop").status_code == 204

    s = advisory_client.get("/status").json()
    assert "setpoint_temperature" not in s["metrics"]
    assert s["components"]["incubator"]["state"] == "off"


def test_temperature_out_of_range_is_422(advisory_client) -> None:
    assert (
        advisory_client.post(
            "/control/incubator/set_temperature", json={"celsius": 90.0}
        ).status_code
        == 422
    )


def test_temperature_does_not_make_the_device_busy(advisory_client) -> None:
    """Holding a setpoint is a condition, not a primary operation (§2.3)."""

    advisory_client.post("/control/incubator/set_temperature", json={"celsius": 37.0})
    s = advisory_client.get("/status").json()
    assert s["activity"] == "idle"
    assert s["equipment_status"] != "busy"


# ---------------------------------------------------------------------------
# Shaker
# ---------------------------------------------------------------------------


def test_shake_reports_running_after_the_request_returns(advisory_client) -> None:
    """The shake command returns as soon as motion starts, and a background
    task keeps the plate moving. Activity must reflect the *plate*, not the
    lifetime of the HTTP call — otherwise minutes of motion read as idle."""

    assert (
        advisory_client.post(
            "/control/shake/start", json={"pattern": "orbital", "displacement_mm": 3}
        ).status_code
        == 204
    )

    s = advisory_client.get("/status").json()
    assert s["activity"] == "running"
    assert s["components"]["shaker"]["state"] == "shaking"

    assert advisory_client.post("/control/shake/stop").status_code == 204
    s = advisory_client.get("/status").json()
    assert s["activity"] == "idle"
    assert s["components"]["shaker"]["state"] == "idle"


def test_shake_stop_stays_available_while_shaking(advisory_client) -> None:
    """§2.3 keeps abort/stop reachable while running.

    This is not academic for the shaker: motion outlives the request, so
    without `shake.stop` advertised the only documented way to stop the plate
    would be shutting the device down.
    """

    advisory_client.post("/control/shake/start", json={})
    s = advisory_client.get("/status").json()
    assert s["activity"] == "running"
    assert "shake.stop" in s["allowed_actions"]
    # ...and nothing that would start a second concurrent operation.
    assert "shake.start" not in s["allowed_actions"]
    assert "read.absorbance" not in s["allowed_actions"]
    assert "imaging.capture" not in s["allowed_actions"]

    advisory_client.post("/control/shake/stop")


def test_shaking_blocks_reads(loaded_client) -> None:
    """Reads are withheld while shaking for two independent reasons: you
    should not read a moving plate, and PyLabRobot's `send_command` has no
    internal lock — the shake task talks to the instrument on its own, so a
    concurrent read would interleave writes on the serial link."""

    loaded_client.post("/control/shake/start", json={})
    s = loaded_client.get("/status").json()
    assert "read.absorbance" not in s["allowed_actions"]
    loaded_client.post("/control/shake/stop")

    s = loaded_client.get("/status").json()
    assert "read.absorbance" in s["allowed_actions"]


def test_shake_rejects_out_of_range_displacement(advisory_client) -> None:
    assert (
        advisory_client.post(
            "/control/shake/start", json={"displacement_mm": 9}
        ).status_code
        == 422
    )


def test_shake_rejects_unknown_pattern(advisory_client) -> None:
    assert (
        advisory_client.post(
            "/control/shake/start", json={"pattern": "vortex"}
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_firmware_version_populates_equipment_version(advisory_client) -> None:
    """`equipment_version` was null on every envelope because nothing filled
    it; the instrument knows its own firmware revision."""

    s = advisory_client.get("/status").json()
    assert s["equipment_version"] == "3.10-stub"
    assert s["details"]["instrument_serial"] == "STUB0000"


def test_phase_contrast_availability_is_reported(advisory_client) -> None:
    """The driver refuses phase contrast on Cytation1 firmware, so whether
    the channel exists is a property of the unit, not of the request."""

    s = advisory_client.get("/status").json()
    assert s["details"]["imaging"]["phase_contrast_available"] is True
