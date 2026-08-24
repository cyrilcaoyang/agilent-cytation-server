"""The serial link must serialize, and D->O must stay atomic.

Why this exists: `send_command` has no internal lock, so a temperature read
landing inside the shaker's 16-minute re-trigger would interleave writes and
steal replies. Avoiding that by refusing to read while shaking is what left the
2026-08-21 solubility run with no temperature for its entire heated phase.
"""

from __future__ import annotations

import asyncio

import pytest

from agilent_cytation_server import link_lock


class FakeBackend:
    """Records the interleaving of concurrent command sequences."""

    def __init__(self, delay: float = 0.01) -> None:
        self.log: list[str] = []
        self.delay = delay

    async def send_command(self, command, parameter=None, *a, **kw):
        self.log.append(f"{command}<")
        await asyncio.sleep(self.delay)  # a real exchange takes time
        self.log.append(f"{command}>")
        return b"\x060000\x03"


@pytest.mark.asyncio
async def test_a_shake_retrigger_and_a_temperature_read_do_not_interleave() -> None:
    """The exact race the old is_shaking() guard was avoiding."""
    b = FakeBackend()
    link_lock.install(b)

    async def shake_retrigger():
        await b.send_command("D", "0033...")   # opens the transaction
        await b.send_command("O")             # closes it

    async def temperature_poll():
        await asyncio.sleep(0.005)            # land inside the D/O pair
        await b.send_command("h")

    await asyncio.gather(shake_retrigger(), temperature_poll())

    joined = "".join(b.log)
    assert "D<D>O<O>" in joined, f"D/O was split: {b.log}"
    assert joined.index("h<") > joined.index("O>"), f"h cut in: {b.log}"


@pytest.mark.asyncio
async def test_single_commands_serialize_against_each_other() -> None:
    b = FakeBackend()
    link_lock.install(b)
    await asyncio.gather(*(b.send_command(c) for c in "htg"))
    # Every command must complete before the next begins.
    for i in range(0, len(b.log), 2):
        assert b.log[i][0] == b.log[i + 1][0], f"overlapped: {b.log}"


@pytest.mark.asyncio
async def test_a_failed_D_releases_the_link() -> None:
    """A transaction that dies before its "O" must not strand the lock."""
    b = FakeBackend()

    async def boom(command, parameter=None, *a, **kw):
        raise OSError("cable yanked")

    b.send_command = boom  # type: ignore[method-assign]
    link_lock.install(b)

    with pytest.raises(OSError):
        await b.send_command("D", "x")
    # The link must still be usable.
    b2_ok = False
    try:
        await asyncio.wait_for(b.send_command("h"), timeout=1.0)
    except OSError:
        b2_ok = True  # the fake raises, but it was *reached* — lock was free
    assert b2_ok, "link stayed locked after a failed transaction"


@pytest.mark.asyncio
async def test_a_stranded_transaction_is_reclaimed(monkeypatch) -> None:
    """If "O" never arrives, the link is reclaimed rather than lost forever."""
    monkeypatch.setattr(link_lock, "TRANSACTION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(link_lock, "ACQUIRE_TIMEOUT_S", 0.5)
    b = FakeBackend()
    link_lock.install(b)

    await b.send_command("D", "x")       # opens and never closes
    await asyncio.sleep(0.08)
    await asyncio.wait_for(b.send_command("h"), timeout=1.0)  # must not hang


@pytest.mark.asyncio
async def test_install_is_idempotent() -> None:
    b = FakeBackend()
    first = link_lock.install(b)
    assert link_lock.install(b) is first
    await b.send_command("h")
    assert b.log == ["h<", "h>"], "double-wrapped"
