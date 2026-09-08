"""A saturated well is reported by name, not as an indistinguishable null.

The instrument marks a well whose absorbance exceeds the detector range with
`*******` on the wire. PyLabRobot's `_parse_body` maps that to `float("nan")`
(`value = float("nan") if "*" in raw_value else float(raw_value)`), and JSON
renders NaN as `null` — which without `over_range` reads exactly like "this
well was not measured".

Measured on serial 23030927, 2026-09-07: a methylene-blue serial dilution had
A2 and H4 over range at 664 nm, both reproducibly, while the other 34
perimeter wells read normally. Those two are the *top* of the dilution series,
so a caller treating them as missing fits its curve to the tail.
"""

from __future__ import annotations

import math

import pytest

from agilent_cytation_server.control_args import ReadResponse
from agilent_cytation_server.reader import CytationReader


class _Well:
    """Minimal stand-in for a PyLabRobot ``Well`` (row/column accessors only)."""

    def __init__(self, row: int, col: int) -> None:
        self._row, self._col = row, col

    def get_row(self) -> int:
        return self._row

    def get_column(self) -> int:
        return self._col


def _grid(values: dict[tuple[int, int], float | None]):
    grid: list[list[float | None]] = [[None] * 12 for _ in range(8)]
    for (r, c), v in values.items():
        grid[r][c] = v
    return grid


# The values actually observed at 664 nm, A1-A4 (A2 over range).
OBSERVED = {(0, 0): 0.2077, (0, 1): float("nan"), (0, 2): 0.2070, (0, 3): 1.1434}


def test_nan_survives_grid_to_wells_as_nan() -> None:
    """The reader must not turn an over-range marker into a raise or a zero."""
    wells = [_Well(0, i) for i in range(4)]
    names = ["A1", "A2", "A3", "A4"]
    out = CytationReader._grid_to_wells(_grid(OBSERVED), wells, names)
    assert math.isnan(out["A2"])
    assert out["A1"] == pytest.approx(0.2077)
    assert out["A4"] == pytest.approx(1.1434)


def test_over_range_is_named_and_value_is_null() -> None:
    resp = ReadResponse.from_values(
        {"A1": 0.2077, "A2": float("nan"), "A3": 0.2070, "A4": 1.1434}
    )
    assert resp.over_range == ["A2"]
    assert resp.wells["A2"] is None
    assert resp.wells["A1"] == pytest.approx(0.2077)


def test_json_distinguishes_saturated_from_measured() -> None:
    """The wire form is what callers branch on, so pin it."""
    resp = ReadResponse.from_values({"A1": 0.2077, "A2": float("nan")})
    payload = resp.model_dump(mode="json")
    assert payload == {"wells": {"A1": 0.2077, "A2": None}, "over_range": ["A2"]}


def test_no_saturated_wells_leaves_over_range_empty() -> None:
    """The common case must stay clean — no empty-list noise to special-case."""
    resp = ReadResponse.from_values({"A1": 0.2077, "A3": 0.2070})
    assert resp.over_range == []
    assert all(v is not None for v in resp.wells.values())


def test_over_range_follows_requested_order() -> None:
    """Not sorted: 'the order you asked' beats 'A10 before A2'."""
    resp = ReadResponse.from_values(
        {"H4": float("nan"), "A2": float("nan"), "A10": 0.5}
    )
    assert resp.over_range == ["H4", "A2"]


def test_a_well_with_no_data_still_raises_rather_than_reporting_null() -> None:
    """`null` must mean over-range and nothing else.

    A grid cell PyLabRobot never filled is a different failure — the read did
    not cover the well — and it has always raised. If that ever softened into
    a null, `over_range` would stop being the only reason for one.
    """
    wells = [_Well(0, 0), _Well(0, 1)]
    with pytest.raises(RuntimeError, match="no data for well A2"):
        CytationReader._grid_to_wells(_grid({(0, 0): 0.2077}), wells, ["A1", "A2"])
