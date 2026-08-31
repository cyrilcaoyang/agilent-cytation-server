"""Where captures are written.

`_save_capture` used to write straight into the captures root, which left
1142 loose PNGs whose plate could no longer be recovered from anything but a
timestamp in the filename. Images are now filed under
`captures/<plate_id>/<YYYYMMDD>/`, and the root is kept clear.

These tests exercise `CytationReader`'s path logic directly rather than
through a capture, because saving needs pillow, a real frame, and the camera.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agilent_cytation_server.reader import CytationReader

WHEN = datetime(2026, 8, 31, 11, 4, 8)


def _reader(tmp_path: Path, plate_id: str | None) -> CytationReader:
    r = CytationReader(imaging_enabled=False, captures_dir=tmp_path)
    r._plate_id = plate_id
    return r


def test_captures_are_filed_under_plate_then_date(tmp_path: Path) -> None:
    d = _reader(tmp_path, "crystallization_20260828")._capture_dir(WHEN)
    assert d == tmp_path / "crystallization_20260828" / "20260831"


def test_the_captures_root_is_never_the_target(tmp_path: Path) -> None:
    """The actual regression. Any plate id, including hostile ones, must
    produce a directory strictly below the root."""

    for plate_id in [
        "plate-1",
        "crystallization_20260828",
        None,
        "",
        "   ",
        "..",
        "../../etc",
        "C:\\Windows\\System32",
        "a/b/c",
        "....",
        "___",
        "x" * 300,
        "plate 1 (60 mg KNO3)",
        "påte-ünïcode",
    ]:
        d = _reader(tmp_path, plate_id)._capture_dir(WHEN)
        assert d != tmp_path, f"{plate_id!r} landed in the root"
        assert tmp_path in d.parents, f"{plate_id!r} escaped the root: {d}"
        # plate + date, no deeper and no shallower
        assert len(d.relative_to(tmp_path).parts) == 2, f"{plate_id!r} -> {d}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("crystallization_20260828", "crystallization_20260828"),
        ("plate-1.a", "plate-1.a"),
        ("plate 1 (60 mg)", "plate_1__60_mg"),
        ("a/b/c", "a_b_c"),
        ("..", "_unfiled"),
        ("../../etc", "etc"),
        ("", "_unfiled"),
        ("___", "_unfiled"),
    ],
)
def test_plate_ids_are_sanitised(raw: str, expected: str) -> None:
    assert CytationReader._safe_dir_name(raw) == expected


def test_a_long_plate_id_is_truncated_not_rejected() -> None:
    """Losing the tail of an over-long id is better than failing a capture
    that has already cost the sample an exposure."""

    assert CytationReader._safe_dir_name("x" * 300) == "x" * 64


def test_unloading_a_plate_forgets_its_id(tmp_path: Path) -> None:
    """Otherwise the next capture files itself under the previous plate."""

    r = _reader(tmp_path, "plate-1")
    r.unload_plate()  # no reader attached: takes the early-return path
    assert r._plate_id is None
    assert r._capture_dir(WHEN) == tmp_path / "_unfiled" / "20260831"
