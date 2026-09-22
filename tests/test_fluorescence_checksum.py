"""The rejected-checksum defect is not absorbance-only.

`_pad_for_checksum` has guarded absorbance since 2026-08-23, but it was wired
into `read_absorbance` alone — `read_fluorescence` and `read_luminescence` went
out unguarded and nobody had taken a fluorescence read on a real plate to find
out ("live-but-unverified", docs/LABSKILLS.md).

Measured 2026-09-22 on serial 23030927, during the plate-reader-uv-smoke run:
a 360/400 nm read of A1:C3 was refused with `2D06` after 46 absorbance reads on
the same region had all succeeded. Its predicted checksum is 95 — inside the
rejected band — which is what tied the refusal to this defect rather than to
the fluorescence path being broken in some new way.

The fluorescence frame is its own thing: opcode `008401`, two wavelengths, and
a `+7` offset absorbance does not have. Luminescence is a third frame again,
with `+8` and the integration time inside the checksummed body.
"""

from __future__ import annotations

import pytest

from agilent_cytation_server.reader import CytationReader

from .test_absorbance_checksum import FakeBackend, FakePlate, FakeWell


@pytest.fixture
def reader() -> CytationReader:
    r = CytationReader.__new__(CytationReader)
    r._backend = FakeBackend()
    r._plate_resource = lambda: FakePlate()  # type: ignore[method-assign]
    return r


def test_the_read_that_was_actually_refused() -> None:
    """A1:C3 at ex 360 / em 400 — the exact read the instrument rejected."""
    ck = CytationReader._fluorescence_checksum(0, 0, 2, 2, 360, 400)
    assert ck == 95
    assert ck in CytationReader._UNSAFE_CHECKSUMS


def test_fluorescence_offset_is_not_the_absorbance_one() -> None:
    """The `+7` is load-bearing: drop it and the prediction is 7 too low."""
    base = (
        f"008401{1:02}{1:02}{3:02}{3:02}"
        f"{CytationReader._FLUO_FIXED_A}{360:04d}000{400:04d}"
        f"{CytationReader._FLUO_FIXED_B}"
    )
    assert sum(base.encode()) % 100 == 88          # what absorbance's rule gives
    assert CytationReader._fluorescence_checksum(0, 0, 2, 2, 360, 400) == 95


def test_the_emission_sweep_is_nearly_half_rejected_unguarded() -> None:
    """Why this matters: it is not a rare unlucky wavelength.

    Sweeping em 400-700 nm at 10 nm over A1:C3 with ex 360, 13 of 31 points
    land in the rejected band — so an unguarded scan fails at its first read
    and would keep failing across the range.
    """
    bad = [
        em for em in range(400, 701, 10)
        if CytationReader._fluorescence_checksum(0, 0, 2, 2, 360, em)
        in CytationReader._UNSAFE_CHECKSUMS
    ]
    assert len(bad) == 13
    assert bad[0] == 400, "the sweep's first point is one of them"


def test_safe_fluorescence_region_is_left_alone(reader) -> None:
    wells = [FakeWell(r, c) for r in range(3) for c in range(3)]
    ck = CytationReader._fluorescence_checksum(0, 0, 2, 2, 360, 450)
    assert ck not in CytationReader._UNSAFE_CHECKSUMS, "precondition: 450 nm is safe"
    assert reader._pad_for_checksum(
        wells, (360, 450), checksum=CytationReader._fluorescence_checksum
    ) is wells


def test_unsafe_fluorescence_region_is_grown_until_accepted(reader) -> None:
    wells = [FakeWell(r, c) for r in range(3) for c in range(3)]
    padded = reader._pad_for_checksum(
        wells, (360, 400), checksum=CytationReader._fluorescence_checksum
    )
    assert len(padded) > len(wells), "the refused read should have been padded"
    rows = [w.get_row() for w in padded]
    cols = [w.get_column() for w in padded]
    ck = CytationReader._fluorescence_checksum(
        min(rows), min(cols), max(rows), max(cols), 360, 400
    )
    assert ck not in CytationReader._UNSAFE_CHECKSUMS


def test_every_rejected_emission_point_can_be_rescued(reader) -> None:
    """Padding is a real fix for this sweep, not a partial one."""
    wells = [FakeWell(r, c) for r in range(3) for c in range(3)]
    for em in range(400, 701, 10):
        padded = reader._pad_for_checksum(
            wells, (360, em), checksum=CytationReader._fluorescence_checksum
        )
        rows = [w.get_row() for w in padded]
        cols = [w.get_column() for w in padded]
        ck = CytationReader._fluorescence_checksum(
            min(rows), min(cols), max(rows), max(cols), 360, em
        )
        assert ck not in CytationReader._UNSAFE_CHECKSUMS, f"em {em} nm unrescued"


def test_luminescence_checksum_depends_on_integration_time() -> None:
    """The integration time is inside the checksummed body, so it moves the value.

    Derived from `biotek_backend.read_luminescence`. This function exists to keep
    the band hypothesis testable; it does NOT guard reads - see below.
    """
    a = CytationReader._luminescence_checksum(0, 0, 2, 2, 1.0)
    b = CytationReader._luminescence_checksum(0, 0, 2, 2, 2.0)
    assert a != b
    assert 0 <= a < 100 and 0 <= b < 100


def test_the_band_does_not_explain_luminescence_refusals() -> None:
    """Why luminescence is deliberately NOT padded.

    Measured 2026-08-31 (docs/BENCH_2026-08-31.md §4): single-well regions H11
    and G12 both compute a checksum inside the rejected band and READ FINE,
    while H12 computes one outside it and fails. The band predicts the opposite
    of the observed behaviour, so padding is the wrong tool for this path - and
    would be harmful, since growing a region can reach the H12 corner that
    actually fails.
    """
    h11 = CytationReader._luminescence_checksum(7, 10, 7, 10, 1.0)
    g12 = CytationReader._luminescence_checksum(6, 11, 6, 11, 1.0)
    h12 = CytationReader._luminescence_checksum(7, 11, 7, 11, 1.0)

    # The two that WORKED sit inside the band; the one that FAILED sits outside.
    assert h11 in CytationReader._UNSAFE_CHECKSUMS
    assert g12 in CytationReader._UNSAFE_CHECKSUMS
    assert h12 not in CytationReader._UNSAFE_CHECKSUMS


@pytest.mark.asyncio
async def test_luminescence_is_not_padded(reader) -> None:
    """A luminescence read must reach the driver with exactly the wells asked for.

    Padding could grow the region into H12 and break a working read.
    """
    sent: dict = {}

    async def fake_frontend(name, **kw):
        sent.update(kw)
        return [{"data": [[0.0] * 12 for _ in range(8)]}]

    reader._require_connected = lambda: None
    reader._require_plate = lambda: None
    reader._check_wells = lambda w: None
    reader._call_frontend = fake_frontend
    reader._wells_for = lambda names: [FakeWell(7, 10)]      # H11
    reader._grid_to_wells = lambda grid, objs, names: {"H11": 0.0}

    await CytationReader.read_luminescence(reader, wells=["H11"])
    assert len(sent["wells"]) == 1, "luminescence must not be padded"
