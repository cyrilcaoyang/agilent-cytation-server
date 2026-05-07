"""Plate-geometry tests.

These tests intentionally do **not** import PyLabRobot — only the
:class:`PlateGeometry` config loader is exercised. Building a real
:class:`pylabrobot.resources.Plate` is gated behind ``--extra plr`` and
covered by the Phase 2 sample-tracker tests.
"""

from __future__ import annotations

import pytest

from agilent_cytation_server.plates import (
    PLATE_FACTORIES,
    PlateGeometry,
    known_models,
)


def test_known_models_are_registered() -> None:
    assert set(known_models()) == {"custom_96", "agilent_shallow_96"}
    for model_id, factory in PLATE_FACTORIES.items():
        assert callable(factory), f"{model_id} factory is not callable"


@pytest.mark.parametrize("model", ["custom_96", "agilent_shallow_96"])
def test_geometry_loads_from_config(model: str) -> None:
    geom = PlateGeometry.from_config(model)
    # Footprint is the SBS standard; values come from config.example.toml
    # defaults baked into agilent_cytation_server.config._DEFAULTS.
    assert geom.size_x == pytest.approx(127.76)
    assert geom.size_y == pytest.approx(85.48)
    # Wells are positive volumes; the actual upper bound is plate-specific.
    assert geom.well_max_volume_ul > 0
    # 8x12 wells must fit inside the plate footprint.
    assert geom.well_dx * 12 <= geom.size_x + 1e-3
    assert geom.well_dy * 8 <= geom.size_y + 1e-3


def test_geometry_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        PlateGeometry.from_config("does_not_exist")


def test_factories_lazy_import_pylabrobot() -> None:
    """Building a Plate without pylabrobot installed must surface a
    clear error message that points at the right uv extras, not a
    bare ImportError from somewhere deep in the call stack.

    We only run this when pylabrobot is *not* importable. When it is,
    the assertion is skipped — the Phase 2 tests cover the
    pylabrobot-installed path directly.
    """
    try:
        import pylabrobot  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError, match="pylabrobot"):
            PLATE_FACTORIES["custom_96"]("test")
    else:
        pytest.skip("pylabrobot is installed; covered by Phase 2 tests")
