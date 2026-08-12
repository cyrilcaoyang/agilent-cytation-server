"""Typed precondition failures for the ``/control/*`` surface.

STATUS_SPEC §6.1 asks that a refused-because-the-device-isn't-ready request
return HTTP 412 with a body a client can branch on **by shape**, not by
string-matching ``detail``. These exception types are how the reader and
service report such a refusal without knowing anything about HTTP, and how
:mod:`agilent_cytation_server.api` decides between 412, 422, and 503.

They are deliberately *not* used for execution failures. A driver error
mid-read is a genuine fault that belongs in ``last_error`` (§6.3); a
precondition refusal is the device declining an inapplicable request while
perfectly healthy, and must leave ``last_error`` alone.
"""

from __future__ import annotations

from typing import Any


class PreconditionNotMet(RuntimeError):
    """A control action is inapplicable in the device's current state.

    ``code`` is the stable discriminator (the field a client branches on);
    ``extra`` carries the precondition-specific fields that make the 412
    body self-describing.
    """

    #: Overridden by subclasses; part of the wire contract.
    code: str = "precondition_not_met"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, Any] = extra

    def to_body(self) -> dict[str, Any]:
        """Render the HTTP 412 body."""

        return {"detail": self.message, "precondition": self.code, **self.extra}


class PlateNotLoaded(PreconditionNotMet):
    """No plate is assigned in the reader, so no well can be addressed.

    Recovery is operator-driven (a plate has to be loaded), so no
    ``retry_after_s`` — §6.1 wants that omitted rather than guessed.
    """

    code = "plate_not_loaded"

    def __init__(
        self,
        message: str = "No plate is loaded. POST /control/plate/load before reading.",
        **extra: Any,
    ) -> None:
        super().__init__(message, required_action="plate.load", **extra)


class CameraNotReady(PreconditionNotMet):
    """The imaging camera was not initialised, so no capture can run."""

    code = "camera_not_ready"

    def __init__(self, message: str, *, camera_error: str | None = None, **extra: Any) -> None:
        super().__init__(message, camera_error=camera_error, **extra)


def describe(exc: BaseException) -> str:
    """A message that is never empty.

    PyLabRobot's BioTek backend validates instrument replies with bare
    ``assert`` statements, so a rejected command arrives as an
    ``AssertionError`` whose ``str()`` is the empty string. Passed straight
    through, that becomes ``{"detail": ""}`` on the wire and an empty
    ``last_error.message`` on ``/status`` — an operator is told only that
    something failed, which is barely better than silence. Naming the
    exception type at least says *where* it broke, and the assertion case
    gets the one hint that is almost always right.
    """

    message = str(exc).strip()
    if message:
        return message
    if isinstance(exc, AssertionError):
        return (
            "The instrument rejected the command (driver assertion failed). "
            "This usually means the reader was not in a state to run it — "
            "most often no plate is physically present, or the drawer is open."
        )
    return f"{type(exc).__name__} (no message)"


__all__ = ["CameraNotReady", "PlateNotLoaded", "PreconditionNotMet", "describe"]
