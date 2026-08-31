"""The serial link after a shake abort, and how a dead one reaches /status.

PyLabRobot aborts a shake with ``send_command("x", wait_for_response=False)``.
The instrument answers anyway, so the reply is left in the receive buffer and
every subsequent request reads the *previous* command's response — the link
stays physically alive while answering the wrong questions. Observed twice on
2026-08-25; once it cost a run and a PnP re-enumeration to clear.

What is worth pinning here is not the happy path but the shape of the
failure: a desynced link is not silent, so "something came back" is not proof
of recovery, and the condition does not heal on a timer, so it cannot live in
the recent-error window.
"""

from __future__ import annotations

import asyncio

import pytest

from agilent_cytation_server.models import TEMPERATURE_MAX_C
from agilent_cytation_server.plate_state import PlateStateStore
from agilent_cytation_server.reader import CytationReader, StubCytationReader
from agilent_cytation_server.service import CytationService


def _temp_reply(celsius: float) -> bytes:
    """The instrument's framing: STX + hundred-thousandths + ETX."""
    return b"\x06" + f"{int(celsius * 100000):07d}".encode() + b"\x03"


class _FakeIO:
    def __init__(self) -> None:
        self.purges = 0

    async def usb_purge_rx_buffer(self) -> None:
        self.purges += 1


class _FakeBackend:
    """Replays a scripted sequence of replies to ``send_command``.

    An entry may be ``bytes`` (a reply) or an ``Exception`` (raised), which
    is how a timeout on a wedged link is expressed.
    """

    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.io = _FakeIO()
        self.commands: list[str] = []

    async def send_command(self, command, *args, **kwargs):
        self.commands.append(command)
        if not self.replies:
            raise TimeoutError("no scripted reply left")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def stop_shaking(self) -> None:
        return None


def _reader(replies: list[object]) -> tuple[CytationReader, _FakeBackend]:
    reader = CytationReader(imaging_enabled=False)
    backend = _FakeBackend(replies)
    reader._backend = backend
    reader._connected = True
    return reader, backend


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_succeeds_on_a_coherent_reply() -> None:
    reader, backend = _reader([_temp_reply(24.1)])

    assert await reader._resync_link() is True
    assert reader.link_healthy() is True
    # Draining first is the point: a stale reply left in the buffer is what
    # desynced the link, and probing before purging would just read it.
    assert backend.io.purges == 1


@pytest.mark.asyncio
async def test_an_incoherent_reply_is_a_failed_probe_not_a_passed_one() -> None:
    """The regression this whole change exists for.

    A desynced link answers — with the previous command's reply. Parsed as a
    temperature that lands outside the instrument's declared range, which
    `_read_temperature` reports as ``None`` rather than by raising. Treating
    only exceptions as failure would therefore pass on exactly the fault
    being probed for.
    """

    out_of_range = _temp_reply(TEMPERATURE_MAX_C + 40)
    reader, _ = _reader([out_of_range] * 6)

    assert await reader._resync_link(attempts=3) is False
    assert reader.link_healthy() is False


@pytest.mark.asyncio
async def test_resync_retries_and_recovers() -> None:
    reader, _ = _reader([TimeoutError("wedged"), _temp_reply(23.6)])

    assert await reader._resync_link(attempts=3) is True
    assert reader.link_healthy() is True


@pytest.mark.asyncio
async def test_a_later_coherent_reply_clears_the_desync() -> None:
    """Recovery must not require another shake abort to be noticed.

    `_read_temperature` runs on the ordinary status poll, so clearing the
    flag there is what makes a link that catches up on its own visible
    without operator action.
    """

    reader, backend = _reader([_temp_reply(TEMPERATURE_MAX_C + 40)] * 6)
    await reader._resync_link(attempts=3)
    assert reader.link_healthy() is False

    backend.replies = [_temp_reply(24.0)]
    assert await reader._read_temperature(timeout=1.0) == pytest.approx(24.0)
    assert reader.link_healthy() is True


@pytest.mark.asyncio
async def test_stop_shaking_probes_the_link_and_does_not_raise_on_a_dead_one() -> None:
    """The shaker did stop. Turning that into an exception would lose it."""

    reader, _ = _reader([TimeoutError("wedged")] * 6)

    await reader.stop_shaking()  # must not raise

    assert reader.link_healthy() is False


# ---------------------------------------------------------------------------
# How it surfaces
# ---------------------------------------------------------------------------


def _service(tmp_path) -> CytationService:
    """A connected service over the stub reader.

    Deliberately not the ``advisory_client`` fixture: that runs
    ``dry_run=True``, and `dry_run` outranks every health check in the state
    chain — correctly, since a simulated device has no link to lose. The
    condition under test only exists on a real one.
    """

    svc = CytationService(
        dry_run=False,
        reader_factory=StubCytationReader,
        plate_state=PlateStateStore(state_path=tmp_path / "state.json"),
    )
    asyncio.run(svc.startup())
    return svc


def test_status_reports_error_while_the_link_is_desynced(tmp_path) -> None:
    """§2.2: never `ready` with a known run-blocking fault.

    A desync blocks reads, captures and the temperature query alike, so
    there is no useful subset left to call `degraded`.
    """

    svc = _service(tmp_path)
    svc._reader.link_healthy = lambda: False

    s = asyncio.run(svc.get_status())

    assert s.equipment_status == "error"
    assert "desynchronised" in (s.message or "")
    assert s.required_actions == ["shutdown", "startup"]


def test_a_desynced_link_withholds_the_optical_actions(tmp_path) -> None:
    svc = _service(tmp_path)
    asyncio.run(svc.load_plate(plate_id="P-1"))
    assert "read.absorbance" in asyncio.run(svc.get_status()).allowed_actions

    svc._reader.link_healthy = lambda: False
    allowed = asyncio.run(svc.get_status()).allowed_actions

    assert "read.absorbance" not in allowed
    assert "imaging.capture" not in allowed
    # The recovery path stays reachable — §2.2 permits it, and withholding
    # it is what left the operator with no way out of the 2026-07-15
    # plateloc failure.
    assert "shutdown" in allowed


def test_the_desync_outlives_the_recent_error_window(tmp_path) -> None:
    """Why this is a live condition and not a timestamped `last_error`.

    `last_error` ages out after 60 s. A desync does not heal on a timer, so
    an implementation that only recorded one would report `ready` on a link
    where every command still times out.
    """

    svc = _service(tmp_path)
    svc._reader.link_healthy = lambda: False
    svc._last_error = None  # nothing recent to prop the state up

    assert asyncio.run(svc.get_status()).equipment_status == "error"


def test_status_is_ready_again_once_the_link_recovers(tmp_path) -> None:
    svc = _service(tmp_path)
    svc._reader.link_healthy = lambda: False
    assert asyncio.run(svc.get_status()).equipment_status == "error"

    svc._reader.link_healthy = lambda: True

    s = asyncio.run(svc.get_status())
    assert s.equipment_status == "ready"
    assert s.required_actions == []


def test_stop_shaking_records_a_coded_last_error_on_a_dead_link(tmp_path) -> None:
    """The service picks the failure up from the flag, not from an exception.

    A stable `code` is what lets a client offer the reconnect hint without
    string-matching the message (best practice #6).
    """

    svc = _service(tmp_path)
    asyncio.run(svc.shake())
    svc._reader.link_healthy = lambda: False

    asyncio.run(svc.stop_shaking())

    assert svc._last_error is not None
    assert svc._last_error.code == "link_desync"
