"""Where captures are written.

`_save_capture` used to write straight into the captures root, which left
1142 loose PNGs whose plate could no longer be recovered from anything but a
timestamp in the filename. Images now go to `captures/<YYYYMMDD>_<plate_id>/`
— timestamp leading so a listing of the root sorts chronologically, matching
the folders `scripts/` writes — and the root itself is kept clear.

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


def test_the_folder_name_leads_with_the_date(tmp_path: Path) -> None:
    d = _reader(tmp_path, "crystallization_20260828")._capture_dir(WHEN)
    assert d == tmp_path / "20260831_crystallization_20260828"


def test_folders_sort_chronologically(tmp_path: Path) -> None:
    """The point of leading with the date: a plain listing is a timeline,
    even when the plates are unrelated and alphabetically adversarial."""

    names = [
        _reader(tmp_path, plate)._capture_dir(when).name
        for when, plate in [
            (datetime(2026, 9, 2, 9, 0), "aaa_plate"),
            (datetime(2026, 8, 31, 9, 0), "zzz_plate"),
            (datetime(2026, 9, 1, 9, 0), "mmm_plate"),
        ]
    ]
    assert sorted(names) == [
        "20260831_zzz_plate",
        "20260901_mmm_plate",
        "20260902_aaa_plate",
    ]


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
        assert d.parent == tmp_path, f"{plate_id!r} escaped the root: {d}"
        # exactly one folder below the root, and it starts with the date
        assert len(d.relative_to(tmp_path).parts) == 1, f"{plate_id!r} -> {d}"
        assert d.name.startswith("20260831_"), f"{plate_id!r} -> {d.name}"


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
    assert r._capture_dir(WHEN) == tmp_path / "20260831__unfiled"
