"""Fault injection for durable plate state and hardware cleanup; no hardware IO."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agilent_cytation_server.models import WellSample
from agilent_cytation_server.plate_state import PlateStateStore, PlateStatePersistenceError
from agilent_cytation_server.reader import CytationReader, StubCytationReader
from agilent_cytation_server.service import CytationService


def seeded(tmp_path):
    store = PlateStateStore(state_path=tmp_path / 'state.json')
    store.load_plate(plate_id='original', model='custom_96', wells=[
        WellSample(well='A1', sample_id='valuable', volume_ul=50, notes='retain'),
    ])
    return store


def fail_replace(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(Path, 'replace', fail)


def test_same_plate_reassertion_preserves_samples(tmp_path):
    store = seeded(tmp_path)
    expected = store.get().wells
    assert store.load_plate(plate_id='original', model='custom_96').wells == expected
    assert PlateStateStore(state_path=store.state_path).get().wells == expected
    # An explicit replacement list still replaces; a different plate starts empty.
    assert store.load_plate(plate_id='original', model='custom_96', wells=[]).wells == []
    new = store.load_plate(plate_id='different', model='custom_96')
    assert len(new.wells) == 96 and all(w.sample_id is None for w in new.wells)


@pytest.mark.parametrize('action', ['load', 'unload', 'update'])
def test_failed_save_leaves_memory_and_disk_unchanged(tmp_path, monkeypatch, action):
    store = seeded(tmp_path)
    before = store.get()
    disk = store.state_path.read_bytes()
    fail_replace(monkeypatch)
    with pytest.raises(PlateStatePersistenceError, match='not committed'):
        if action == 'load':
            store.load_plate(plate_id='replacement', model='custom_96')
        elif action == 'unload':
            store.unload_plate()
        else:
            store.update_well('A1', sample_id='replacement')
    assert store.get() == before
    assert store.state_path.read_bytes() == disk


@pytest.mark.parametrize('raw', ['[]', 'null', '3'])
def test_non_object_state_is_ignored(tmp_path, raw):
    path = tmp_path / 'state.json'
    path.write_text(raw)
    assert PlateStateStore(state_path=path).get() is None


async def service_with_plate(tmp_path):
    store = seeded(tmp_path)
    service = CytationService(dry_run=False, reader_factory=StubCytationReader, plate_state=store)
    await service.startup()
    return service


async def test_invalid_wells_do_not_change_reader_or_restoration_flag(tmp_path):
    service = await service_with_plate(tmp_path)
    before = service.plate_state.get()
    with pytest.raises(ValueError, match='Duplicate'):
        await service.load_plate(plate_id='replacement', wells=[WellSample(well='A1')] * 2)
    assert service.plate_state.get() == before
    assert service._reader._plate == 'original'
    assert service._plate_restored_at_startup is True


@pytest.mark.parametrize('action', ['load', 'unload'])
async def test_service_rolls_back_reader_after_save_failure(tmp_path, monkeypatch, action):
    service = await service_with_plate(tmp_path)
    before = service.plate_state.get()
    fail_replace(monkeypatch)
    with pytest.raises(PlateStatePersistenceError):
        if action == 'load':
            await service.load_plate(plate_id='replacement')
        else:
            await service.unload_plate()
    assert service.plate_state.get() == before
    assert service._reader._plate == 'original'
    assert service._plate_restored_at_startup is True


async def test_reader_assignment_failure_rolls_back(tmp_path, monkeypatch):
    service = await service_with_plate(tmp_path)
    reader = service._reader
    original_load = reader.load_plate
    def fail_replacement(*, plate_id, model):
        reader.unload_plate()
        if plate_id == 'replacement':
            raise RuntimeError('assignment failed')
        return original_load(plate_id=plate_id, model=model)
    monkeypatch.setattr(reader, 'load_plate', fail_replacement)
    with pytest.raises(RuntimeError, match='assignment failed'):
        await service.load_plate(plate_id='replacement')
    assert reader._plate == 'original'
    assert service.plate_state.get().plate_id == 'original'


async def test_failed_rollback_disconnects_reader(tmp_path, monkeypatch):
    service = await service_with_plate(tmp_path)
    monkeypatch.setattr(service._reader, 'load_plate', Mock(side_effect=RuntimeError('bad resource')))
    with pytest.raises(RuntimeError):
        await service.load_plate(plate_id='replacement')
    assert not service._reader.is_connected()
    assert service.plate_state.get().plate_id == 'original'
    assert 'read.absorbance' not in (await service.get_status()).allowed_actions


async def test_same_plate_retains_model_when_model_omitted(tmp_path):
    service = await service_with_plate(tmp_path)
    await service.load_plate(plate_id='original', model='agilent_shallow_96')
    plate = await service.load_plate(plate_id='original')
    assert plate.model == 'agilent_shallow_96'
    assert plate.wells[0].sample_id == 'valuable'


@pytest.mark.parametrize('endpoint,body', [
    ('plate/load', {'plate_id': 'replacement'}),
    ('plate/unload', {}),
    ('well/update', {'well': 'A1', 'sample_id': 'replacement'}),
])
def test_api_reports_persistence_failure(loaded_client, monkeypatch, endpoint, body):
    before = loaded_client.get('/status').json()['details']['loaded_plate']
    fail_replace(monkeypatch)
    response = loaded_client.post('/control/' + endpoint, json=body)
    assert response.status_code == 503
    assert 'not committed' in response.json()['detail']
    assert loaded_client.get('/status').json()['details']['loaded_plate'] == before


async def test_cancelled_startup_releases_handle_and_allows_retry(tmp_path):
    entered = asyncio.Event()
    class HangingReader(StubCytationReader):
        stopped = False
        async def setup(self):
            entered.set()
            await asyncio.Event().wait()
        async def stop(self):
            self.stopped = True
            await super().stop()
    reader = HangingReader()
    service = CytationService(dry_run=False, reader_factory=lambda: reader,
                              plate_state=PlateStateStore(state_path=tmp_path / 'state.json'))
    task = asyncio.create_task(service.startup())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.stopped and service._reader is None
    service._create_reader = StubCytationReader
    await service.startup()
    assert service._reader.is_connected()


def capture_reader(monkeypatch):
    r = CytationReader(imaging_enabled=True)
    backend = SimpleNamespace(**{name: AsyncMock() for name in (
        'set_plate', 'set_objective', 'set_imaging_mode', 'select', 'set_gain',
        'set_position', 'led_off',
    )}, start_acquisition=Mock(), stop_acquisition=Mock())
    r._backend, r._reader, r._camera_ready = backend, object(), True
    monkeypatch.setattr(r, '_resolve_channel', lambda _: SimpleNamespace(name='BRIGHTFIELD'))
    monkeypatch.setattr(r, '_resolve_objective', lambda _: SimpleNamespace(name='O_4X_PL_FL_Phase'))
    well = SimpleNamespace(get_row=lambda: 0, get_column=lambda: 0)
    monkeypatch.setattr(r, '_wells_for', lambda _: [well])
    monkeypatch.setattr(r, '_require_plate', lambda: object())
    monkeypatch.setattr(r, '_acquire_at', AsyncMock(return_value=object()))
    monkeypatch.setattr(r, '_save_capture', Mock(return_value={}))
    return r, backend


@pytest.mark.parametrize('failure', ['set_imaging_mode', 'select', 'set_gain', 'set_position', 'start_acquisition', 'acquire', 'stop_acquisition', 'led_off'])
async def test_capture_failure_always_attempts_led_off(monkeypatch, failure):
    reader, backend = capture_reader(monkeypatch)
    failing = reader._acquire_at if failure == 'acquire' else getattr(backend, failure)
    failing.side_effect = RuntimeError(failure)
    with pytest.raises(RuntimeError, match=failure):
        await reader.capture_image(well='A1', channel='brightfield')
    backend.led_off.assert_awaited_once()
    reader._save_capture.assert_not_called()
    if failure in {'start_acquisition', 'acquire', 'stop_acquisition', 'led_off'}:
        backend.stop_acquisition.assert_called_once()


async def test_capture_error_survives_cleanup_errors(monkeypatch):
    reader, backend = capture_reader(monkeypatch)
    reader._acquire_at.side_effect = RuntimeError('original error')
    backend.stop_acquisition.side_effect = RuntimeError('stop failed')
    backend.led_off.side_effect = RuntimeError('LED failed')
    with pytest.raises(RuntimeError, match='original error'):
        await reader.capture_image(well='A1', channel='brightfield')
    backend.led_off.assert_awaited_once()


async def test_cancelled_capture_attempts_cleanup(monkeypatch):
    reader, backend = capture_reader(monkeypatch)
    reader._acquire_at.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reader.capture_image(well='A1', channel='brightfield')
    backend.stop_acquisition.assert_called_once()
    backend.led_off.assert_awaited_once()


def test_capture_files_are_unique_and_metadata_round_trips(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    PIL = pytest.importorskip('PIL.Image')
    import agilent_cytation_server.reader as module
    from datetime import datetime, timezone
    class FrozenDatetime:
        @staticmethod
        def now(*args):
            return datetime(2026, 9, 8, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'datetime', FrozenDatetime)
    monkeypatch.setattr(module, '_software_provenance', lambda: {'git_revision': 'test-revision'})
    reader = CytationReader(imaging_enabled=False, captures_dir=tmp_path)
    reader._plate_id, reader._plate_model = 'P1', 'custom_96'
    frame = np.array([[0, 30], [128, 255]], dtype=np.uint8)
    args = dict(well='A1', channel='BRIGHTFIELD', objective='4x',
                focal_height_mm=5.12345, exposure_ms=10.12345, gain=1,
                led_intensity=3, tuning={'focus': [5.0, 5.12345]})
    first = reader._save_capture(frame, **args)
    second = reader._save_capture(frame, **args)
    assert first['image_path'] != second['image_path']
    for payload in (first, second):
        assert json.loads(Path(payload['metadata_path']).read_text()) == payload
        assert payload['plate_id'] == 'P1'
        assert payload['focal_height_mm'] == 5.12345
        assert payload['led_intensity'] == 3
        assert payload['captured_at'].endswith('+00:00')
        with PIL.open(payload['image_path']) as image:
            assert np.array_equal(np.asarray(image), frame)


def test_metadata_failure_is_visible_and_retains_image(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    pytest.importorskip('PIL.Image')
    original_open = Path.open
    def fail_metadata(path, *args, **kwargs):
        if path.suffix == '.json':
            raise OSError('metadata disk failure')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_metadata)
    reader = CytationReader(imaging_enabled=False, captures_dir=tmp_path)
    reader._plate_id, reader._plate_model = 'P1', 'custom_96'
    with pytest.raises(OSError, match='metadata disk failure'):
        reader._save_capture(np.zeros((2, 2), dtype=np.uint8), well='A1', channel='BRIGHTFIELD',
                             objective='4x', focal_height_mm=10, exposure_ms=8, gain=0)
    assert len(list(tmp_path.rglob('*.png'))) == 1


def test_unencodable_metadata_leaves_no_sidecar(tmp_path, monkeypatch):
    """A sidecar that cannot be encoded must not be left half-written.

    `json.dump` writes incrementally, so a non-finite float — which
    `allow_nan=False` correctly refuses, since NaN is not valid JSON — used to
    leave a truncated .json beside a perfectly good .png. A corrupt file that
    still looks like metadata is worse than no metadata: a consumer walking
    the capture directory cannot tell it from a complete one without parsing
    every file. Measured 2026-09-08 against the reassessment branch: a NaN in
    `tuning` left 950 bytes of partial JSON.

    The image is still retained and the operation still fails visibly, which
    is what the sidecar contract intends.
    """
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL.Image")

    reader = CytationReader.__new__(CytationReader)
    reader._captures_dir = tmp_path
    reader._plate_id = "plate"
    reader._plate_model = "square_96_19mm"

    with pytest.raises(ValueError):
        reader._save_capture(
            np.full((4, 4), 7, dtype=np.uint8),
            well="H12",
            channel="brightfield",
            objective="O_4X_PL_FL_Phase",
            focal_height_mm=7.0,
            exposure_ms=8.1,
            gain=0.0,
            led_intensity=10,
            tuning={"autofocus": {"sharpness": float("nan")}},
        )

    images = list(tmp_path.rglob("*.png"))
    sidecars = list(tmp_path.rglob("*.json"))
    assert len(images) == 1, "the image must survive for investigation"
    assert sidecars == [], f"a partial sidecar was left behind: {sidecars}"
