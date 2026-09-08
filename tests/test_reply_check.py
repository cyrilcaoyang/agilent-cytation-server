"""A status word meaning "I did not do that" must not be discardable.

PyLabRobot's `send_command` returns the instrument's reply and 21 of its 22
call sites ignore it. That is how `set_focus` passed for working code for four
months while the instrument answered `570F` — refused — to every command it
sent below 9.3993 mm, and why three bench sessions then measured the
consequences and drew three wrong conclusions.

`reply_check` wraps `send_command` so the codes with bench evidence behind them
raise. It is deliberately a deny-list: `0000` is not universally "accepted"
(`L0510` answers `7A8C` and `A` answers `5A00`, both while working), so an
allow-list would break working paths on its first day.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agilent_cytation_server.reply_check import (
    KNOWN_BENIGN,
    KNOWN_REFUSALS,
    CommandRefused,
    install,
)


class _Backend:
    """Records what was sent and replays a scripted reply per command."""

    def __init__(self, replies: dict[str, bytes]) -> None:
        self._replies = replies
        self.sent: list[tuple[str, object]] = []

    async def send_command(self, command, parameter=None, *args, **kwargs):
        self.sent.append((command, parameter))
        return self._replies.get(command, b"\x060000\x03")


def _run(coro):
    return asyncio.run(coro)


def test_a_known_refusal_raises_with_the_command_attached() -> None:
    b = _Backend({"i": b"570F\x03"})
    install(b)
    with pytest.raises(CommandRefused) as ei:
        _run(b.send_command("i", "F50047876"))
    assert ei.value.status == "570F"
    assert ei.value.command == "i"
    assert ei.value.parameter == "F50047876"
    assert "F50047876" in str(ei.value)


def test_the_checksum_refusal_is_caught_too() -> None:
    """`2D06` is what PyLabRobot turns into a bare AssertionError."""
    b = _Backend({"O": b"\x062D06\x03"})
    install(b)
    with pytest.raises(CommandRefused) as ei:
        _run(b.send_command("O"))
    assert ei.value.status == "2D06"
    assert "checksum" in str(ei.value)


@pytest.mark.parametrize("status", sorted(KNOWN_BENIGN))
def test_benign_status_words_pass_through(status: str) -> None:
    """`0000` is not the only success — an allow-list would break these."""
    b = _Backend({"Y": status.encode() + b"\x03"})
    install(b)
    assert _run(b.send_command("Y", "P0e01")) == status.encode() + b"\x03"


def test_an_unknown_status_is_recorded_not_raised(caplog) -> None:
    """Refusing to guess: log it, return it, let the caller proceed."""
    b = _Backend({"Y": b"ABCD\x03"})
    install(b)
    with caplog.at_level(logging.INFO, logger="agilent_cytation_server.reply_check"):
        assert _run(b.send_command("Y", "P0z99")) == b"ABCD\x03"
    assert "ABCD" in caplog.text


def test_an_unknown_status_is_logged_once_per_command() -> None:
    """Novel-status logging must not become per-call noise."""
    b = _Backend({"Y": b"ABCD\x03"})
    install(b)
    logger = logging.getLogger("agilent_cytation_server.reply_check")

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for _ in range(5):
            _run(b.send_command("Y", "P0z99"))
    finally:
        logger.removeHandler(handler)
    assert len([r for r in records if "ABCD" in r.getMessage()]) == 1


def test_a_data_payload_is_never_treated_as_a_status() -> None:
    """A temperature reply is seven digits; only 4-char replies are statuses."""
    b = _Backend({"h": b"\x062450000\x03"})
    install(b)
    assert _run(b.send_command("h")) == b"\x062450000\x03"


def test_a_read_body_passes_through_untouched() -> None:
    body = b"\x0601,1,\r000:00:00.0,244,01,01,+0.2395,01,02,*******\x03"
    b = _Backend({"D": body})
    install(b)
    assert _run(b.send_command("D", "0047...")) == body


def test_an_empty_reply_is_no_opinion_not_success() -> None:
    b = _Backend({"x": b""})
    install(b)
    assert _run(b.send_command("x")) == b""


def test_install_is_idempotent() -> None:
    b = _Backend({"i": b"570F\x03"})
    install(b)
    first = b.send_command
    install(b)
    assert b.send_command is first


def test_every_known_refusal_carries_an_explanation() -> None:
    """A code with no stated evidence should not be here — see the module doc."""
    for status, meaning in KNOWN_REFUSALS.items():
        assert len(status) == 4, status
        assert len(meaning) > 30, status
    assert not set(KNOWN_REFUSALS) & set(KNOWN_BENIGN)
