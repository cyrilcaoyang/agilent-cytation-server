"""The instrument rejects absorbance commands whose checksum lands in 94..99.

Measured on serial 23030927, 2026-08-23: the `"O"` start-read ACKs with status
`2D06` instead of `0000`, which `biotek_backend.read_absorbance` turns into a
bare AssertionError. It is the checksum *value*, not the wells — A1 fails at
600 nm and reads fine at 325 nm; C1 reads fine at 600 nm and fails at 400 nm.

`CytationReader._pad_for_checksum` grows the read region until the checksum is
acceptable, since the wavelength is the measurement and cannot move.
"""

from __future__ import annotations

import pytest

from agilent_cytation_server.reader import CytationReader


# Every case below was observed on hardware; see the module docstring.
MEASURED = [
    # well, row, col, wavelength, checksum, instrument accepted?
    ("A1", 0, 0, 600, 97, False),
    ("A2", 0, 1, 600, 99, False),
    ("B1", 1, 0, 600, 99, False),
    ("A10", 0, 9, 600, 97, False),
    ("A11", 0, 10, 600, 99, False),
    ("B10", 1, 9, 600, 99, False),
    ("A1", 0, 0, 300, 94, False),
    ("C1", 2, 0, 400, 99, False),
    ("C1", 2, 0, 600, 1, True),
    ("A12", 0, 11, 600, 1, True),
    ("H12", 7, 11, 600, 15, True),
    ("A1", 0, 0, 325, 1, True),
    ("A1", 0, 0, 342, 0, True),
    ("A4", 0, 3, 599, 20, True),
    ("F9", 5, 8, 599, 40, True),
]


@pytest.mark.parametrize("well,row,col,wl,checksum,accepted", MEASURED)
def test_checksum_matches_hardware(well, row, col, wl, checksum, accepted) -> None:
    """Our reimplementation of PLR's checksum, and the accept/reject rule."""
    ck = CytationReader._absorbance_checksum(row, col, row, col, wl)
    assert ck == checksum, f"{well}@{wl}nm"
    predicted_ok = ck not in CytationReader._UNSAFE_CHECKSUMS
    assert predicted_ok is accepted, f"{well}@{wl}nm checksum {ck}"


def test_a1_and_a10_collide_which_is_why_it_is_not_geometry() -> None:
    """Wells at opposite ends of row A share a checksum, and both fail.

    This is the observation that ruled out a positional explanation.
    """
    a1 = CytationReader._absorbance_checksum(0, 0, 0, 0, 600)
    a10 = CytationReader._absorbance_checksum(0, 9, 0, 9, 600)
    assert a1 == a10 == 97
    assert a1 in CytationReader._UNSAFE_CHECKSUMS


class FakeWell:
    def __init__(self, r: int, c: int) -> None:
        self._r, self._c = r, c

    def get_row(self) -> int:
        return self._r

    def get_column(self) -> int:
        return self._c


class FakePlate:
    num_items_y, num_items_x = 8, 12

    def get_item(self, name: str) -> FakeWell:
        return FakeWell("ABCDEFGH".index(name[0]), int(name[1:]) - 1)


class FakeBackend:
    @staticmethod
    def _non_overlapping_rectangles(points):
        pts = list(points)
        rows = [p[0] for p in pts]
        cols = [p[1] for p in pts]
        return [(min(rows), min(cols), max(rows), max(cols))]


@pytest.fixture
def reader() -> CytationReader:
    r = CytationReader.__new__(CytationReader)
    r._backend = FakeBackend()
    r._plate_resource = lambda: FakePlate()  # type: ignore[method-assign]
    return r


def test_safe_region_is_left_completely_alone(reader) -> None:
    """The common case must not read extra wells or reorder anything."""
    wells = [FakeWell(2, 0)]  # C1 @ 600 nm -> checksum 01
    assert reader._pad_for_checksum(wells, 600) is wells


def test_unsafe_region_is_grown_until_the_checksum_is_accepted(reader) -> None:
    wells = [FakeWell(0, 0)]  # A1 @ 600 nm -> checksum 97, rejected
    padded = reader._pad_for_checksum(wells, 600)
    assert len(padded) > 1, "A1 should have been padded"
    rows = [w.get_row() for w in padded]
    cols = [w.get_column() for w in padded]
    ck = CytationReader._absorbance_checksum(
        min(rows), min(cols), max(rows), max(cols), 600
    )
    assert ck not in CytationReader._UNSAFE_CHECKSUMS
    assert (0, 0) in {(w.get_row(), w.get_column()) for w in padded}, "target well kept"


def test_padding_stays_on_the_plate(reader) -> None:
    """H12 is the far corner; growth must not run off the edge."""
    for wl in range(300, 801):
        rect = (7, 11, 7, 11)
        if CytationReader._absorbance_checksum(*rect, wl) in CytationReader._UNSAFE_CHECKSUMS:
            grown = reader._grow_region(rect, wl, FakePlate())
            assert grown is not None, f"no safe growth for H12 at {wl} nm"
            min_row, min_col, max_row, max_col = grown
            assert 0 <= min_row <= max_row < 8
            assert 0 <= min_col <= max_col < 12
            break
    else:
        pytest.skip("H12 never hits an unsafe checksum in 300-800 nm")


def test_every_well_can_be_read_at_every_visible_wavelength(reader) -> None:
    """The whole point: no (well, wavelength) pair should be unreachable."""
    plate = FakePlate()
    unfixable = []
    for r in range(8):
        for c in range(12):
            for wl in (325, 400, 450, 600, 700, 800):
                rect = (r, c, r, c)
                if CytationReader._absorbance_checksum(*rect, wl) in CytationReader._UNSAFE_CHECKSUMS:
                    if reader._grow_region(rect, wl, plate) is None:
                        unfixable.append(("ABCDEFGH"[r] + str(c + 1), wl))
    assert not unfixable, f"no safe region found for {unfixable}"
