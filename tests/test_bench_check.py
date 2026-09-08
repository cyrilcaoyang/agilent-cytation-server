"""Supervised bench helper: plan mode, state gates, journals and claim cleanup."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import bench_check as bench


def arguments(**changes):
    values = dict(mode='luminescence', plate_id='test', confirm_test_plate=True,
                  case='H12', objective=None, focus_mm=None, well='A1', exposure_ms=8, gain=1, led_intensity=10)
    values.update(changes)
    return SimpleNamespace(**values)


def status():
    return dict(activity='idle', equipment_status='ready', details=dict(
        drawer='in', plate_in_reader=True, plate_restored_at_startup=True,
        loaded_plate=dict(plate_id='test', model='custom_96', wells=[
            dict(well='A1', sample_id='retain', volume_ul=50, notes='retain')]),
    ))


class FakeClient:
    def __init__(self, response=None, fail=None):
        self.calls, self.token = [], None
        self.response = response or status()
        self.fail = fail
    def call(self, path, body=None):
        self.calls.append((path, deepcopy(body)))
        if path == self.fail:
            raise RuntimeError('injected measurement failure')
        if path == '/status':
            return self.response
        if path == '/control/claim':
            return dict(claim_token='secret', heartbeat_interval_s=10)
        return {}


def test_plan_mode_never_connects(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Plan mode attempted a connection')
    monkeypatch.setattr(bench, 'urlopen', forbidden)
    assert bench.main(['luminescence', '--case', 'full']) == 0
    plan = json.loads(capsys.readouterr().out)
    wells = plan['measurement_requests'][0][1]['wells']
    assert len(wells) == 96 and wells[0] == 'A1' and wells[-1] == 'H12'


@pytest.mark.parametrize('change', [
    dict(activity='running'), dict(equipment_status='error'),
    dict(drawer='out'), dict(plate_in_reader=False), dict(plate_id='wrong'),
])
def test_mismatched_or_busy_state_refuses_before_claim(change):
    current = status()
    for key, value in change.items():
        if key in current:
            current[key] = value
        elif key == 'plate_id':
            current['details']['loaded_plate']['plate_id'] = value
        else:
            current['details'][key] = value
    client = FakeClient(current)
    with pytest.raises(ValueError):
        bench.execute(arguments(), client, bench.requests_for(arguments()))
    assert client.calls == [('/status', None)]


def test_reassertion_keeps_complete_well_map_and_releases_claim():
    client = FakeClient()
    args = arguments()
    bench.execute(args, client, bench.requests_for(args))
    load = next(body for path, body in client.calls if path == '/control/plate/load')
    assert load == status()['details']['loaded_plate']
    assert client.calls[-1][0] == '/control/release'
    assert client.token is None


def test_measurement_failure_stops_series_and_releases_claim():
    client = FakeClient(fail='/control/imaging/capture')
    args = arguments(mode='imaging', objective='4x', focus_mm=[9.8, 10, 10.2])
    with pytest.raises(RuntimeError, match='injected measurement failure'):
        bench.execute(args, client, bench.requests_for(args))
    assert sum(path == '/control/imaging/capture' for path, body in client.calls) == 1
    assert client.calls[-1][0] == '/control/release'
    assert client.token is None


@pytest.mark.parametrize('positions', [[float('nan')], [4.0], [14.0], [10.0] * 6])
def test_invalid_focus_plan_is_rejected(positions):
    with pytest.raises(ValueError):
        bench.requests_for(arguments(mode='imaging', objective='4x', focus_mm=positions))


def test_journal_redacts_claim_token(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr(bench, 'urlopen', lambda *a, **k: Response(b'{"claim_token":"secret","heartbeat_interval_s":10}'))
    client = bench.BenchClient('http://unused', tmp_path)
    assert client.call('/control/claim', {})['claim_token'] == 'secret'
    text = (tmp_path / '001.json').read_text()
    assert 'secret' not in text and 'claim_token' not in text


def test_journal_keeps_http_failure_body(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError('http://unused', 503, 'failure', None, io.BytesIO(b'{"detail":"instrument refused"}'))
    monkeypatch.setattr(bench, 'urlopen', fail)
    with pytest.raises(RuntimeError, match='instrument refused'):
        bench.BenchClient('http://unused', tmp_path).call('/control/read/luminescence', {'wells': ['H12']})
    entry = json.loads((tmp_path / '001.json').read_text())
    assert entry['status'] == 503 and entry['response']['detail'] == 'instrument refused'
