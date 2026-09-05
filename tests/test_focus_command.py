"""The instrument's focus position field is seven digits, not PyLabRobot's six.

Measured on serial 23030927, 2026-09-04, from the D2XX trace. PyLabRobot's
`biotek_cytation_backend.set_focus` builds the parameter as
``F{mode}0{counts:05d}``, which is seven digits wide only when ``counts``
needs six of them — true at focal heights >= 9.3993 mm and nowhere else.
Below that the instrument answers a constant ``570F`` in 15-36 ms (too short
to contain any travel) and does not move; at and above it, ``0000`` after
93-204 ms that scales with distance.

`CytationReader._set_focus` pads to seven digits and checks the reply, which
PLR discards.
"""

from __future__ import annotations

import pytest

from agilent_cytation_server.reader import CytationReader


def encode(focal_height_mm: float, mode_code: int = 5) -> str:
    counts = int(
        focal_height_mm
        + CytationReader._FOCUS_INTERCEPT
        + CytationReader._FOCUS_SLOPE * focal_height_mm * 1000
    )
    return f"F{mode_code}{counts:0{CytationReader._FOCUS_FIELD_DIGITS}d}"


def plr_encode(focal_height_mm: float, mode_code: int = 5) -> str:
    """Exactly what PyLabRobot 0.2.1 emits, for the comparison below."""
    counts = int(
        focal_height_mm
        + CytationReader._FOCUS_INTERCEPT
        + CytationReader._FOCUS_SLOPE * focal_height_mm * 1000
    )
    return f"F{mode_code}0{str(counts).zfill(5)}"


# (focal height, exact parameter observed on the wire, instrument accepted it?)
OBSERVED = [
    (4.50, "F5047876", False),
    (5.50, "F5058515", False),
    (6.50, "F5069154", False),
    (7.50, "F5079793", False),
    (8.50, "F5090432", False),
    (9.00, "F5095751", False),
    (9.40, "F50100007", True),
    (10.50, "F50111710", True),
    (12.00, "F50127668", True),
    (13.50, "F50143627", True),
]


@pytest.mark.parametrize("focal_mm,wire,accepted", OBSERVED)
def test_plr_encoding_matches_what_was_traced(focal_mm, wire, accepted) -> None:
    """Pin the trace: PLR's encoding, and which half the instrument accepted."""
    assert plr_encode(focal_mm) == wire
    # The accepted ones are exactly the ones that came out seven digits wide.
    assert (len(wire) - 2 == CytationReader._FOCUS_FIELD_DIGITS) is accepted


@pytest.mark.parametrize("focal_mm,_wire,_accepted", OBSERVED)
def test_our_encoding_is_always_seven_digits(focal_mm, _wire, _accepted) -> None:
    param = encode(focal_mm)
    assert param[0] == "F"
    assert param[2:].isdigit()
    assert len(param[2:]) == CytationReader._FOCUS_FIELD_DIGITS


def test_no_change_where_plr_already_worked() -> None:
    """The fix must be a no-op above the boundary — that range reads correctly.

    Guards against 'fixing' the half of the range that was already producing
    real movement, which is the only part of this behaviour with a track
    record on hardware.
    """
    f = 9.3993
    while f <= 13.88:
        assert encode(f) == plr_encode(f), f
        f = round(f + 0.01, 4)


def test_below_the_boundary_every_value_changes() -> None:
    f = 4.5
    while f < 9.3993:
        assert encode(f) != plr_encode(f), f
        assert len(encode(f)) == len(plr_encode(f)) + 1
        f = round(f + 0.01, 4)


def test_boundary_is_where_counts_reaches_six_digits() -> None:
    """9.3993 mm is not a magic number; it is where the counts cross 100000."""
    below, above = 9.3992, 9.3994
    assert len(plr_encode(below)) == 8
    assert len(plr_encode(above)) == 9
