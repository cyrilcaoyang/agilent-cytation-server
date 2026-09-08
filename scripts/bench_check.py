"""Plan or execute one supervised API bench check; never opens USB directly.

Without --execute this prints the measurement requests and makes no connection.
See docs/IMPLEMENTATION.md for plate preparation and the matching Gen5 check.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


class BenchClient:
    def __init__(self, base_url: str, output: Path):
        self.base_url = base_url.rstrip('/')
        self.output = output
        self.token = None
        self._counter = 0
        self._log_lock = threading.Lock()

    def call(self, path, body=None):
        method = 'GET' if body is None else 'POST'
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['X-Claim-Token'] = self.token
        request = Request(self.base_url + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
        started = time.monotonic()
        result, status, failure = None, None, None
        try:
            with urlopen(request, timeout=180 if '/imaging/' in path or '/read/' in path else 10) as response:
                status = response.status
                raw = response.read().decode()
                result = json.loads(raw) if raw else None
            return result
        except HTTPError as exc:
            status = exc.code
            raw = exc.read().decode(errors='replace')
            try:
                result = json.loads(raw)
            except ValueError:
                result = raw
            failure = f'HTTP {status}: {result}'
            raise RuntimeError(f'{path}: {failure}') from exc
        except Exception as exc:
            failure = str(exc)
            raise
        finally:
            # Claims are credentials: never persist the returned token.
            public_result = ({k: v for k, v in result.items() if k != 'claim_token'}
                             if isinstance(result, dict) else result)
            entry = dict(timestamp=datetime.now(timezone.utc).isoformat(), method=method,
                         path=path, request=body, response=public_result, status=status,
                         elapsed_s=round(time.monotonic() - started, 3), error=failure)
            with self._log_lock:
                self._counter += 1
                with (self.output / f'{self._counter:03d}.json').open('x', encoding='utf-8') as stream:
                    json.dump(entry, stream, indent=2)


def requests_for(args):
    if args.mode == 'snapshot':
        return []
    if args.mode == 'imaging':
        if not args.objective or not args.focus_mm:
            raise ValueError('Imaging requires --objective and --focus-mm matched to your Gen5 check')
        if len(args.focus_mm) > 5:
            raise ValueError('Use at most five focus positions per supervised check')
        if not all(math.isfinite(f) and 4.5 <= f <= 13.88 for f in args.focus_mm):
            raise ValueError('Focus positions must be finite and within 4.5–13.88 mm')
        if not math.isfinite(args.exposure_ms) or not 0.1 <= args.exposure_ms <= 500:
            raise ValueError('Exposure must be finite and within 0.1–500 ms')
        if not math.isfinite(args.gain) or not 0 <= args.gain <= 47:
            raise ValueError('Gain must be finite and within 0–47 dB')
        return [('/control/imaging/capture', dict(
            well=args.well, channel='brightfield', objective=args.objective,
            focal_height_mm=f, exposure_ms=args.exposure_ms, gain=args.gain,
            led_intensity=args.led_intensity, autofocus=False, auto_exposure=False,
        )) for f in args.focus_mm]
    wells = ([f'{r}{c}' for r in 'ABCDEFGH' for c in range(1, 13)]
             if args.case == 'full' else [args.case])
    return [('/control/read/luminescence', dict(wells=wells, focal_height_mm=7.0, integration_time_s=1.0))]


def require_test_plate(status, plate_id):
    details = status.get('details', {})
    loaded = details.get('loaded_plate')
    if status.get('activity') != 'idle' or status.get('equipment_status') not in ('ready', 'dry_run'):
        raise ValueError('Reader must be ready and idle before this check')
    if not loaded or loaded.get('plate_id') != plate_id or not details.get('plate_in_reader'):
        raise ValueError('Recorded plate does not match --plate-id; prepare and identify the test plate first')
    if details.get('drawer') != 'in':
        raise ValueError('Drawer is not recorded as closed; close and verify it before this check')
    # Needed when reasserting on an older service: omission would erase samples.
    if not isinstance(loaded.get('wells'), list) or not loaded.get('model'):
        raise ValueError('Plate metadata is incomplete; refusing to reassert it')
    return loaded


def execute(args, client, requests):
    status = client.call('/status')
    if args.mode == 'snapshot':
        return
    if not args.confirm_test_plate or not args.plate_id:
        raise ValueError('Actuation requires --plate-id and --confirm-test-plate after physical inspection')
    require_test_plate(status, args.plate_id)
    claim = client.call('/control/claim', dict(owner='supervised-bench', session_id=uuid4().hex, ttl_s=30))
    client.token = claim['claim_token']
    stop = threading.Event()
    heartbeat_errors = []
    def heartbeat():
        while not stop.wait(5):
            try:
                client.call('/control/heartbeat', {})
            except Exception as exc:
                heartbeat_errors.append(exc)
                return
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    primary_error = False
    try:
        # Recheck after claiming to close the preparation/claim race.
        current = client.call('/status')
        loaded = require_test_plate(current, args.plate_id)
        if current['details'].get('plate_restored_at_startup'):
            client.call('/control/plate/load', {k: loaded[k] for k in ('plate_id', 'model', 'wells')})
        for path, payload in requests:
            if heartbeat_errors:
                raise RuntimeError('Claim heartbeat failed; no further measurements will be sent') from heartbeat_errors[0]
            client.call(path, payload)
        if heartbeat_errors:
            raise RuntimeError('Claim heartbeat failed during measurement; inspect the saved results') from heartbeat_errors[0]
        client.call('/status')
    except BaseException:
        primary_error = True
        raise
    finally:
        stop.set()
        worker.join(timeout=12)
        try:
            client.call('/control/release', {})
        except Exception as exc:
            if not primary_error:
                raise
            print(f'Claim release also failed: {exc}')
        finally:
            client.token = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['snapshot', 'imaging', 'luminescence'])
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--url', default='http://127.0.0.1:8040')
    parser.add_argument('--plate-id')
    parser.add_argument('--confirm-test-plate', action='store_true')
    parser.add_argument('--well', default='A1')
    parser.add_argument('--objective')
    parser.add_argument('--focus-mm', nargs='+', type=float)
    parser.add_argument('--exposure-ms', type=float, default=8.0)
    parser.add_argument('--gain', type=float, default=0.0)
    parser.add_argument('--led-intensity', type=int, choices=range(1, 11), default=10)
    parser.add_argument('--case', choices=['H11', 'G12', 'H12', 'full'], default='H11')
    parser.add_argument('--output', type=Path, default=Path('captures'))
    args = parser.parse_args(argv)
    try:
        requests = requests_for(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.execute:
        print(json.dumps({'mode': args.mode, 'measurement_requests': requests,
                          'note': 'Plan only; --execute enables the supervised check.'}, indent=2))
        return 0
    if args.mode != 'snapshot' and (not args.confirm_test_plate or not args.plate_id):
        parser.error('--execute requires --plate-id and --confirm-test-plate for measurements')
    folder = args.output / ('bench_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'manifest.json').write_text(json.dumps(vars(args), default=str, indent=2), encoding='utf-8')
    print(f'Bench evidence: {folder.resolve()}')
    try:
        execute(args, BenchClient(args.url, folder), requests)
    except Exception as exc:
        print(f'Stopped: {exc}. Inspect the journal before retrying; no automatic retry or restart.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
