"""Plate factories for the Cytation 5 — `custom_96` and `agilent_shallow_96`.

Each factory builds a PyLabRobot ``Plate`` (containing 96 ``Well``
``Container``s, with volume tracking) from the geometry declared in
``[plates.<model>]`` in ``config.toml``. PyLabRobot is imported lazily
because Phase 0+1 only uses these factories under Phase 2 sample
tracking; the read-only service does not depend on pylabrobot at all.

Adding a new plate model: register a ``PlateGeometry`` and a factory
function under :data:`PLATE_FACTORIES`. The schema is intentionally
flat so configuration can be tuned in TOML without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import config as _config


@dataclass(frozen=True)
class PlateGeometry:
    """Flat geometry record for a 96-well plate.

    Numbers are millimetres / microlitres, snake_case keys mirror the
    ``[plates.<model>]`` TOML section.
    """

    size_x: float
    size_y: float
    size_z: float
    well_dx: float
    well_dy: float
    well_size_x: float
    well_size_y: float
    well_size_z: float
    well_max_volume_ul: float

    @classmethod
    def from_config(cls, model: str) -> "PlateGeometry":
        section = _config.get_section("plates").get(model)
        if not isinstance(section, dict):
            raise KeyError(
                f"No [plates.{model}] section in config.toml. "
                f"Known models: {sorted(PLATE_FACTORIES)}"
            )
        # Allow either snake_case fields (preferred) or fallback defaults.
        return cls(
            size_x=float(section["size_x"]),
            size_y=float(section["size_y"]),
            size_z=float(section["size_z"]),
            well_dx=float(section["well_dx"]),
            well_dy=float(section["well_dy"]),
            well_size_x=float(section["well_size_x"]),
            well_size_y=float(section["well_size_y"]),
            well_size_z=float(section["well_size_z"]),
            well_max_volume_ul=float(section["well_max_volume_ul"]),
        )


# ---------------------------------------------------------------------------
# Public factories — keyed by short string id (matches `default_model` in
# config.toml and the `model` field accepted by future POST /control/plate/load).
# ---------------------------------------------------------------------------


def custom_96(name: str, *, geometry: PlateGeometry | None = None) -> Any:
    """Build the shop-built 96-well plate as a PyLabRobot ``Plate``.

    PyLabRobot is imported lazily so this module can be unit-tested on
    a dev box that does not have pylabrobot installed; geometry-only
    tests live in ``tests/test_plates.py`` and exercise
    :meth:`PlateGeometry.from_config`.
    """

    return _build_plate(name, "custom_96", geometry)


def agilent_shallow_96(name: str, *, geometry: PlateGeometry | None = None) -> Any:
    """Build the Agilent shallow-well 96 plate as a PyLabRobot ``Plate``."""

    return _build_plate(name, "agilent_shallow_96", geometry)


def _build_plate(name: str, model: str, geometry: PlateGeometry | None) -> Any:
    """Lazy-import PyLabRobot and assemble a 96-well :class:`Plate`."""

    if geometry is None:
        geometry = PlateGeometry.from_config(model)

    try:  # noqa: SIM105 - we want a clear error message
        from pylabrobot.resources import Plate, Well, create_equally_spaced_2d
    except ImportError as exc:
        raise ImportError(
            "pylabrobot is required to build a real Plate resource. "
            "Install with `uv sync --extra plr` (and `--extra windows` on "
            "the lab PC)."
        ) from exc

    items = create_equally_spaced_2d(
        Well,
        num_items_x=12,
        num_items_y=8,
        dx=geometry.well_dx,
        dy=geometry.well_dy,
        dz=0.0,
        item_dx=geometry.well_size_x,
        item_dy=geometry.well_size_y,
        size_x=geometry.well_size_x,
        size_y=geometry.well_size_y,
        size_z=geometry.well_size_z,
        max_volume=geometry.well_max_volume_ul,
    )
    return Plate(
        name=name,
        size_x=geometry.size_x,
        size_y=geometry.size_y,
        size_z=geometry.size_z,
        items=items,
    )


PLATE_FACTORIES: dict[str, Callable[..., Any]] = {
    "custom_96": custom_96,
    "agilent_shallow_96": agilent_shallow_96,
}


def known_models() -> list[str]:
    """Return the registered plate model identifiers."""

    return sorted(PLATE_FACTORIES)


__all__ = [
    "PlateGeometry",
    "PLATE_FACTORIES",
    "agilent_shallow_96",
    "custom_96",
    "known_models",
]
