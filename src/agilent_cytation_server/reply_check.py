r"""Refuse to ignore the instrument when it says no.

The problem
-----------
Every BioTek command answers with a short status word, and PyLabRobot
discards almost all of them — ``send_command`` returns the bytes, and of the
22 call sites in ``biotek_backend.py`` only the ``"O"`` start-read checks
what came back. That is how the focus bug survived four months: ``set_focus``
sent a command the instrument rejected outright, got ``570F`` back, threw it
away, and reported success. Three separate bench sessions then measured the
*consequences* — "the focus does not move", "the 40X returns the 20X image",
"autofocus is quantized to a grid" — and all three conclusions were wrong,
because the instrument had been saying so plainly the whole time.

The fix is not to check one command. It is to make a silent refusal
structurally impossible, so the next command with this failure mode announces
itself on its first use rather than after a month of misattributed symptoms.

Why a deny-list, not an allow-list
----------------------------------
``0000`` is not universally "accepted": ``L0510`` (LED on) answers ``7A8C``
and ``A`` (carrier close) answers ``5A00``, both while working perfectly. The
status vocabulary is per-command and mostly undocumented, so treating anything
non-``0000`` as failure would break working paths on day one.

So this raises **only** for codes bench work has actually tied to a refusal,
and logs anything it has not seen before at INFO — which costs nothing and
accumulates the vocabulary during ordinary operation instead of requiring
another trace session to discover it.

Scope
-----
Only replies that are exactly four characters after framing is stripped are
treated as status words. A temperature reply (``2450000``) and a read body are
longer, so they pass through untouched.

Installed as a monkeypatch alongside :mod:`link_lock`, and idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: Status words with bench evidence that the instrument did not act.
#:
#: Keep this conservative. A code earns a place here only once a session has
#: tied it to an observable no-op — the cost of a wrong entry is a working
#: path that starts raising, which is worse than the silence it replaces.
KNOWN_REFUSALS: dict[str, str] = {
    # Measured 2026-09-04. Returned in ~15 ms with no travel time, for both a
    # position field of the wrong width and a position outside the legal
    # window for the objective in the light path.
    "570F": (
        "the instrument refused the requested position and did not move — "
        "either it is outside the legal window for the current objective, or "
        "the command field is the wrong width"
    ),
    # Measured 2026-08-23. The `O` start-read answers this instead of `0000`
    # when the command checksum lands in 94-99; `_pad_for_checksum` exists to
    # avoid it, and PyLabRobot's bare `assert` turns it into an unexplained
    # AssertionError. See tests/test_absorbance_checksum.py.
    "2D06": (
        "the instrument refused the read command — its checksum landed in the "
        "rejected 94-99 band, which _pad_for_checksum is supposed to avoid"
    ),
}

#: Status words seen and understood to be benign, so they are not logged as
#: novel every time. Purely to keep the log quiet; nothing branches on it.
KNOWN_BENIGN: dict[str, str] = {
    "0000": "accepted",
    "5A00": "carrier stage motion complete",
    "7A8C": "LED on",
}


class CommandRefused(RuntimeError):
    """The instrument answered a command with a status meaning "I did not".

    Carries the command and the raw status so a caller can add context
    (which objective, which well) without re-parsing a message string.
    """

    def __init__(self, command: str, parameter: Any, status: str, meaning: str) -> None:
        self.command = command
        self.parameter = parameter
        self.status = status
        self.meaning = meaning
        shown = f"{command}{parameter}" if parameter is not None else command
        super().__init__(f"Cytation refused {shown!r}: status {status!r} — {meaning}")


def _status_of(response: Any) -> str | None:
    """Return the four-character status word in a reply, or ``None``.

    ``None`` means "not a status reply" — a data payload, an empty response, or
    anything whose length says it is carrying a measurement rather than an
    acknowledgement. Callers must treat that as "no opinion", never as success.
    """

    if not response:
        return None
    try:
        text = bytes(response).strip(b"\x06\x03\r\n").decode("latin")
    except (TypeError, ValueError):
        return None
    return text if len(text) == 4 else None


def install(backend: Any) -> None:
    """Wrap ``backend.send_command`` so a refusal raises instead of vanishing."""

    if getattr(backend, "_reply_check_installed", False):
        return
    original = backend.send_command
    seen: set[tuple[str, str]] = set()

    async def send_command(
        command: str, parameter: Any = None, *args: Any, **kwargs: Any
    ) -> Any:
        response = await original(command, parameter, *args, **kwargs)
        status = _status_of(response)
        if status is None:
            return response

        if status in KNOWN_REFUSALS:
            raise CommandRefused(command, parameter, status, KNOWN_REFUSALS[status])

        key = (command, status)
        if status not in KNOWN_BENIGN and key not in seen:
            seen.add(key)
            # Not a warning: an unrecognised status is far more likely to be a
            # normal reply we have not catalogued than a fault, and crying wolf
            # here would train the next person to ignore this log line.
            logger.info(
                "Cytation status %r from command %r (parameter %r) is not in "
                "the known vocabulary — recording it, not acting on it. Add it "
                "to reply_check.KNOWN_BENIGN or KNOWN_REFUSALS once a bench "
                "session establishes what it means.",
                status,
                command,
                parameter,
            )
        return response

    backend.send_command = send_command  # type: ignore[method-assign]
    backend._reply_check_installed = True  # type: ignore[attr-defined]
    logger.info(
        "Reply-status check installed; refusals (%s) now raise instead of "
        "being discarded",
        ", ".join(sorted(KNOWN_REFUSALS)),
    )
