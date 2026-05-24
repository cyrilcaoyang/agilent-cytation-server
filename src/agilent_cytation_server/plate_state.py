"""Persistent per-well sample tracking for the Cytation 5 (Phase 2).

The Cytation service is a long-running process; restarts (lab PC
reboot, ``nssm restart cytation``, hardware swap) must not lose the
orchestrator's view of "which plate is currently loaded and what is
in each well". This module serialises :class:`LoadedPlate` to a JSON
file (``[plates].state_path``, default ``./state.json``) on every
mutation and re-loads it on service startup.

Volume tracking lives here, not in the PyLabRobot ``Plate`` resource
tree: the orchestrator owns ``sample_id``, the device owns
``volume_ul``, and the JSON file is the durable record of both. The
PyLabRobot ``Plate`` is rebuilt from this state when a workflow needs
geometry-aware planning (Phase 3+ skill catalog).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import LoadedPlate, WellSample
from .plates import known_models

logger = logging.getLogger(__name__)


_ROW_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]


def well_ids_96() -> list[str]:
    """Standard 96-well labels in row-major order: A1..A12, B1..B12, ..., H12."""
    return [f"{row}{col}" for row in _ROW_LETTERS for col in range(1, 13)]


def _resolve_state_path(state_path: str | Path) -> Path:
    p = Path(state_path)
    if not p.is_absolute():
        # Anchor relative paths to the project root (parent of src/).
        p = (Path(__file__).resolve().parent.parent.parent / p).resolve()
    return p


class PlateStateStore:
    """Thread-safe JSON-backed store for the currently-loaded plate.

    Single-plate model: the Cytation has one stage, so at most one
    :class:`LoadedPlate` is "in" at any time. ``plate.load`` overwrites
    any existing plate (the orchestrator is expected to call
    ``plate.unload`` first; the model does not enforce it because the
    plate may have been physically removed during a hardware reset).
    """

    def __init__(self, *, state_path: str | Path) -> None:
        self._path = _resolve_state_path(state_path)
        self._lock = threading.Lock()
        self._plate: LoadedPlate | None = None
        self._load_from_disk()

    @property
    def state_path(self) -> Path:
        return self._path

    def get(self) -> LoadedPlate | None:
        with self._lock:
            return self._plate.model_copy(deep=True) if self._plate else None

    def load_plate(
        self,
        *,
        plate_id: str,
        model: str,
        wells: list[WellSample] | None = None,
    ) -> LoadedPlate:
        if model not in known_models():
            raise ValueError(
                f"Unknown plate model {model!r}. Known: {known_models()}"
            )
        wells = wells if wells is not None else self._empty_wells_96()
        self._validate_wells(wells)
        plate = LoadedPlate(
            plate_id=plate_id,
            model=model,
            loaded_at=datetime.now(timezone.utc),
            wells=wells,
        )
        with self._lock:
            self._plate = plate
            self._persist_locked()
        return plate.model_copy(deep=True)

    def unload_plate(self) -> LoadedPlate | None:
        with self._lock:
            previous = self._plate
            self._plate = None
            self._persist_locked()
        return previous.model_copy(deep=True) if previous else None

    def update_well(
        self,
        well: str,
        *,
        sample_id: str | None = None,
        volume_ul: float | None = None,
        notes: str | None = None,
        clear_sample_id: bool = False,
        clear_notes: bool = False,
    ) -> WellSample:
        with self._lock:
            if self._plate is None:
                raise LookupError("No plate is currently loaded")
            for i, w in enumerate(self._plate.wells):
                if w.well == well:
                    new_sample = (
                        None if clear_sample_id else (sample_id if sample_id is not None else w.sample_id)
                    )
                    new_notes = (
                        None if clear_notes else (notes if notes is not None else w.notes)
                    )
                    new_volume = volume_ul if volume_ul is not None else w.volume_ul
                    if new_volume is not None and new_volume < 0:
                        raise ValueError(f"Negative volume {new_volume} for {well}")
                    updated = WellSample(
                        well=well,
                        sample_id=new_sample,
                        volume_ul=new_volume,
                        notes=new_notes,
                    )
                    self._plate.wells[i] = updated
                    self._persist_locked()
                    return updated.model_copy(deep=True)
            raise LookupError(f"Well {well!r} not in loaded plate")

    # ---- internals ----------------------------------------------------

    def _empty_wells_96(self) -> list[WellSample]:
        return [WellSample(well=w) for w in well_ids_96()]

    def _validate_wells(self, wells: list[WellSample]) -> None:
        valid = set(well_ids_96())
        seen: set[str] = set()
        for w in wells:
            if w.well not in valid:
                raise ValueError(f"Invalid well id {w.well!r}; expected A1..H12")
            if w.well in seen:
                raise ValueError(f"Duplicate well id {w.well!r}")
            seen.add(w.well)

    def _load_from_disk(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("state.json at %s is unreadable; ignoring", self._path)
            return
        plate_raw = raw.get("plate")
        if not plate_raw:
            return
        try:
            self._plate = LoadedPlate.model_validate(plate_raw)
        except Exception:
            logger.exception("state.json at %s has malformed plate; ignoring", self._path)

    def _persist_locked(self) -> None:
        body: dict[str, Any] = {
            "plate": self._plate.model_dump(mode="json") if self._plate else None,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception("Failed to persist plate state to %s", self._path)


__all__ = ["PlateStateStore", "well_ids_96"]
