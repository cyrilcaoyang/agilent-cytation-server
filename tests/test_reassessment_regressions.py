"""Regressions found while reassessing the September 8 reply checker and claims."""
import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from agilent_cytation_server.reader import CytationReader
from agilent_cytation_server import claims, link_lock, reply_check


@pytest.mark.parametrize("reply", [b"", b"\x06\x03", b"ABCD\x03", b"0000extra"] )
async def test_focus_does_not_accept_an_unconfirmed_reply(reply):
    class Backend:
        _imaging_mode = object()
        _focal_height = 10.0
        def _imaging_mode_code(self, mode):
            return 5
        async def send_command(self, *args, **kwargs):
            return reply
    backend = Backend()
    reply_check.install(backend)
    reader = CytationReader(imaging_enabled=False)
    reader._backend = backend
    with pytest.raises(RuntimeError):
        await reader._set_focus(5.0)
    assert backend._focal_height == 10.0


async def test_refused_setup_releases_transaction_lock(monkeypatch):
    class Backend:
        async def send_command(self, command, parameter=None, *args, **kwargs):
            return b'\x062D06\x03' if command == 'D' else b'\x062450000\x03'
    monkeypatch.setattr(link_lock, 'ACQUIRE_TIMEOUT_S', 0.01)
    backend = Backend()
    reply_check.install_checked_link(backend)
    with pytest.raises(reply_check.CommandRefused):
        await backend.send_command('D', 'synthetic-setup')
    assert await backend.send_command('h') == b'\x062450000\x03'


@pytest.mark.parametrize("ttl", [30, 90, 300, 600])
def test_heartbeat_holds_claim_until_next_advertised_heartbeat(monkeypatch, ttl):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(claims, '_now', lambda: now)
    manager = claims.ClaimManager()
    token, interval, expiry = manager.claim(owner='review', session_id='review', ttl_s=ttl)
    now += timedelta(seconds=interval)
    manager.heartbeat(token)
    now += timedelta(seconds=interval - 1)
    assert manager.matches(token), 'claim expired before the next advertised heartbeat'


@pytest.mark.parametrize('reply', [b'0000', b'\x060000\x03', b'\r\n0000\r\n'])
async def test_confirmed_focus_updates_cache(reply):
    class Backend:
        _imaging_mode = object()
        _focal_height = 10.0
        def _imaging_mode_code(self, mode):
            return 5
        async def send_command(self, *args, **kwargs):
            return reply
    backend = Backend()
    reply_check.install_checked_link(backend)
    reader = CytationReader(imaging_enabled=False)
    reader._backend = backend
    await reader._set_focus(5.0)
    assert backend._focal_height == 5.0


async def test_checked_link_is_idempotent_and_releases_refused_start():
    class Backend:
        sent = []
        async def send_command(self, command, parameter=None):
            self.sent.append(command)
            return b'2D06' if command == 'O' else b'0000'
    backend = Backend()
    reply_check.install_checked_link(backend)
    installed = backend.send_command
    reply_check.install_checked_link(backend)
    assert backend.send_command is installed
    await backend.send_command('D', 'setup')
    with pytest.raises(reply_check.CommandRefused):
        await backend.send_command('O')
    assert await backend.send_command('h') == b'0000'
    assert backend.sent == ['D', 'O', 'h']


def test_checked_link_rejects_unsafe_installation_order():
    class Backend:
        async def send_command(self, *args, **kwargs):
            return b'0000'
    backend = Backend()
    link_lock.install(backend)
    with pytest.raises(RuntimeError, match='before its link lock'):
        reply_check.install_checked_link(backend)
