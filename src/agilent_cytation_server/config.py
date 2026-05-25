"""Configuration loader for the Cytation 5 service.

Reads ``config.toml`` from the project root (next to ``pyproject.toml``)
and exposes the values via :func:`get`. Uses the standard STATUS_SPEC
device-PC config-loader pattern so deployments on the same lab PC behave
identically.

If ``config.toml`` is missing the loader falls back to the built-in
defaults under :data:`_DEFAULTS` so the test suite (and the very first
``--dry-run`` smoke check on a fresh clone) does not require it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


def _find_config_file() -> Path | None:
    """Walk up from this file to find ``config.toml``."""
    here = Path(__file__).resolve().parent
    for parent in [here, here.parent, here.parent.parent, here.parent.parent.parent]:
        candidate = parent / "config.toml"
        if candidate.is_file():
            return candidate
    cwd = Path.cwd() / "config.toml"
    if cwd.is_file():
        return cwd
    return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration from a TOML file.

    Parameters
    ----------
    path:
        Explicit path; ``None`` auto-discovers ``config.toml``.

    Returns
    -------
    dict
        Parsed TOML as a nested dictionary.

    Raises
    ------
    FileNotFoundError
        If no config file is found.
    RuntimeError
        If neither ``tomllib`` (3.11+) nor ``tomli`` is available.
    """
    if tomllib is None:
        raise RuntimeError(
            "No TOML parser available. "
            "Install tomli (`pip install tomli`) or use Python >= 3.11."
        )
    if path is None:
        found = _find_config_file()
        if found is None:
            raise FileNotFoundError(
                "config.toml not found. "
                "Place it next to pyproject.toml or pass an explicit path."
            )
        path = found
    path = Path(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


_DEFAULTS: dict[str, Any] = {
    "instrument": {
        "backend": "cytation5",
        "usb_serial": "",
    },
    "imaging": {
        "enabled": True,
    },
    "plates": {
        "default_model": "custom_96",
        "state_path": "./state.json",
        "custom_96": {
            "size_x": 127.76,
            "size_y": 85.48,
            "size_z": 14.5,
            "well_dx": 9.0,
            "well_dy": 9.0,
            "well_size_x": 6.5,
            "well_size_y": 6.5,
            "well_size_z": 11.0,
            "well_max_volume_ul": 350.0,
        },
        "agilent_shallow_96": {
            "size_x": 127.76,
            "size_y": 85.48,
            "size_z": 7.5,
            "well_dx": 9.0,
            "well_dy": 9.0,
            "well_size_x": 6.5,
            "well_size_y": 6.5,
            "well_size_z": 5.0,
            "well_max_volume_ul": 200.0,
        },
    },
    "service": {
        "host": "0.0.0.0",
        "port": 9333,
        "dry_run": False,
        "cors_origins": ["*"],
        "startup_connect_timeout_s": 30.0,
        "enforce_claims": True,
    },
    "dashboard": {
        "equipment_id": "cytation_5",
        "equipment_name": "BioTek Cytation 5",
        "equipment_version": None,
    },
}

try:
    _cfg = load_config()
except (FileNotFoundError, RuntimeError):
    _cfg = _DEFAULTS


def get(section: str, key: str, default: Any = None) -> Any:
    """Read ``[section].key`` from config.toml; fall back to built-in defaults."""
    return _cfg.get(section, {}).get(key, _DEFAULTS.get(section, {}).get(key, default))


def get_section(section: str) -> dict[str, Any]:
    """Return the merged contents of ``[section]``: config.toml on top of defaults."""
    merged: dict[str, Any] = dict(_DEFAULTS.get(section, {}))
    merged.update(_cfg.get(section, {}) or {})
    return merged


def reload(path: str | Path | None = None) -> None:
    """Re-read the config file. Useful for tests that mutate config on disk."""
    global _cfg
    try:
        _cfg = load_config(path)
    except (FileNotFoundError, RuntimeError):
        _cfg = _DEFAULTS
