"""Channel and objective resolution for the imaging path.

These exercise :class:`CytationReader`'s pure resolution helpers directly
rather than through the API, because both bugs they pin were found on live
hardware and neither is reachable through the dry-run stub (which reports a
fully-populated filter wheel).

Both were real, and both produced *misleading* errors:

* the objective request was upper-cased, so the instrument's own
  ``O_4X_PL_FL_Phase`` was reported "unknown" while simultaneously being
  listed as installed;
* the "cube not fitted" guard was skipped when *no* cubes were fitted, so a
  DAPI request reached the backend and died inside an internal ``.index()``
  with ``<ImagingMode.DAPI: 16> is not in list``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pylabrobot", reason="objective/channel enums come from pylabrobot")

from pylabrobot.plate_reading.standard import ImagingMode, Objective  # noqa: E402

from agilent_cytation_server.reader import CytationReader  # noqa: E402


class _FakeBackend:
    """Stands in for CytationBackend's optics introspection.

    ``filters``/``objectives`` are fixed-length slot lists with ``None`` for
    empty positions, and raise when the instrument was never queried — the
    two behaviours the resolution logic keys on.
    """

    def __init__(self, *, filters, objectives):
        self._filters = filters
        self._objectives = objectives

    @property
    def filters(self):
        if self._filters is None:
            raise RuntimeError("Filters are not set")
        return self._filters

    @property
    def objectives(self):
        if self._objectives is None:
            raise RuntimeError("Objectives are not set")
        return self._objectives


def _reader(*, filters, objectives) -> CytationReader:
    r = CytationReader(imaging_enabled=True)
    r._backend = _FakeBackend(filters=filters, objectives=objectives)
    return r


# The real fit-out of sdl2-pc-03-cytation as of 2026-08-12: three phase
# objectives, four empty cube slots.
_REAL_OBJECTIVES = [
    Objective.O_4X_PL_FL_Phase,
    Objective.O_20X_PL_FL_Phase,
    Objective.O_40X_PL_FL_Phase,
    None,
    None,
    None,
]
_NO_CUBES = [None, None, None, None]


def test_objective_accepts_the_instruments_own_spelling() -> None:
    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    assert r._resolve_objective("O_4X_PL_FL_Phase") is Objective.O_4X_PL_FL_Phase
    # Case-insensitive too, so callers need not match the enum exactly.
    assert r._resolve_objective("o_20x_pl_fl_phase") is Objective.O_20X_PL_FL_Phase


def test_objective_defaults_to_widest_field() -> None:
    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    assert r._resolve_objective(None) is Objective.O_4X_PL_FL_Phase


def test_objective_not_installed_is_refused() -> None:
    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    with pytest.raises(ValueError, match="not installed"):
        r._resolve_objective("O_60X_PL_FL")


def test_brightfield_needs_no_cube() -> None:
    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    assert r._resolve_channel("brightfield") is ImagingMode.BRIGHTFIELD
    assert r._resolve_channel("phase_contrast") is ImagingMode.PHASE_CONTRAST


def test_fluorescence_refused_when_no_cubes_are_fitted() -> None:
    """An empty wheel is a known answer, so every cube channel is refused."""

    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    for channel in ("dapi", "gfp", "rfp", "cy5"):
        with pytest.raises(ValueError, match="filter cube"):
            r._resolve_channel(channel)


def test_fluorescence_allowed_when_its_cube_is_fitted() -> None:
    r = _reader(
        filters=[ImagingMode.DAPI, ImagingMode.GFP, None, None],
        objectives=_REAL_OBJECTIVES,
    )
    assert r._resolve_channel("dapi") is ImagingMode.DAPI
    assert r._resolve_channel("uv") is ImagingMode.DAPI  # documented alias
    with pytest.raises(ValueError, match="filter cube"):
        r._resolve_channel("cy5")


def test_unknown_filter_inventory_does_not_refuse() -> None:
    """A failed query must not be read as "nothing installed".

    Refusing on unknown would send an operator hunting for hardware that is
    sitting in the wheel; the instrument's own error is the better authority.
    """

    r = _reader(filters=None, objectives=_REAL_OBJECTIVES)
    assert r._resolve_channel("dapi") is ImagingMode.DAPI


def test_unknown_channel_is_refused() -> None:
    r = _reader(filters=_NO_CUBES, objectives=_REAL_OBJECTIVES)
    with pytest.raises(ValueError, match="Unknown imaging channel"):
        r._resolve_channel("ultraviolet_ish")
