r"""Serialize access to the Cytation's serial link.

The problem
-----------
``BioTekPlateReaderBackend.send_command`` has no internal lock, so two
concurrent callers interleave writes and steal each other's replies. Until now
this repo avoided that by simply refusing to read the incubator temperature
while the shaker ran (``CytationReader.get_temperature`` returned ``None`` when
``is_shaking()``) — which is why the 30 h solubility run of 2026-08-21 recorded
**no temperature at all for its entire six-hour heated phase**, the one span
where the number mattered.

Why a lock is enough
--------------------
PyLabRobot's ``shake()`` does not hold the link. It starts a background task
that re-sends a 16-minute shake command and then sleeps in 0.25 s ticks until
the next re-trigger, so the link is idle for ~16 minutes at a stretch and busy
for a second or two per cycle. Reading temperature during shaking is therefore
fine as long as it cannot land *inside* a re-trigger.

Two details make a ``send_command``-level lock sufficient:

* **``"D"`` then ``"O"`` is one transaction.** Every read and the shake
  re-trigger send a ``"D"`` (setup, with parameter) followed by ``"O"``
  (start). Those must not be split, so ``"D"`` takes the lock and holds it
  until its matching ``"O"`` completes.
* **The only unsynchronized actor is the shake re-trigger.** Our own reads run
  under the service's operation lock, and the status refresh skips when that
  lock is held — so reads and temperature polls already serialize with each
  other. The shake task runs outside all of it, and its burst is exactly a
  ``D``/``O`` pair with no trailing body transfer.

This is installed as a monkeypatch rather than a fork, matching the existing
patches in ``reader.py``. It is idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: How long an ordinary command waits for the link before giving up. A shake
#: re-trigger occupies it for ~1-2 s; anything beyond this means a transaction
#: leaked, and failing is better than hanging the status endpoint.
ACQUIRE_TIMEOUT_S = 10.0

#: A `"D"` with no matching `"O"` would hold the link forever. Nothing legitimate
#: takes this long between the two, so past it the lock is reclaimed.
TRANSACTION_TIMEOUT_S = 60.0


class LinkBusy(RuntimeError):
    """The serial link was held by another operation for too long."""


class LinkLock:
    """One lock over every ``send_command`` on a backend."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._txn_task: asyncio.Task[Any] | None = None
        self._txn_at: float = 0.0

    # -- transaction bookkeeping ---------------------------------------
    def _open_txn(self) -> None:
        self._txn_task = asyncio.current_task()
        self._txn_at = time.monotonic()

    def _close_txn(self) -> None:
        self._txn_task = None
        self._txn_at = 0.0
        if self._lock.locked():
            self._lock.release()

    def _reclaim_if_stale(self) -> None:
        if self._txn_task is None:
            return
        held = time.monotonic() - self._txn_at
        if held > TRANSACTION_TIMEOUT_S:
            logger.error(
                "Link transaction held %.0f s with no matching 'O'; reclaiming. "
                "This should not happen — a command sequence was interrupted.",
                held,
            )
            self._close_txn()

    async def _acquire(self) -> None:
        self._reclaim_if_stale()
        try:
            await asyncio.wait_for(self._lock.acquire(), ACQUIRE_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LinkBusy(
                f"serial link busy for more than {ACQUIRE_TIMEOUT_S:.0f}s"
            ) from exc

    # -- installation ---------------------------------------------------
    def install(self, backend: Any) -> None:
        if getattr(backend, "_link_lock_installed", False):
            return
        original = backend.send_command

        async def send_command(
            command: str, parameter: Any = None, *args: Any, **kwargs: Any
        ) -> Any:
            # "D" opens a transaction and deliberately keeps the lock.
            if command == "D":
                await self._acquire()
                self._open_txn()
                try:
                    return await original(command, parameter, *args, **kwargs)
                except BaseException:
                    self._close_txn()
                    raise

            # "O" closes the transaction this task opened.
            if command == "O" and self._txn_task is asyncio.current_task():
                try:
                    return await original(command, parameter, *args, **kwargs)
                finally:
                    self._close_txn()

            # Everything else is a single self-contained command.
            await self._acquire()
            try:
                return await original(command, parameter, *args, **kwargs)
            finally:
                self._lock.release()

        backend.send_command = send_command  # type: ignore[method-assign]
        backend._link_lock_installed = True  # type: ignore[attr-defined]
        backend._link_lock = self  # type: ignore[attr-defined]
        logger.info(
            "Serial link lock installed; temperature can now be read while shaking"
        )


def install(backend: Any) -> LinkLock:
    """Install a :class:`LinkLock` on ``backend`` and return it."""
    existing = getattr(backend, "_link_lock", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    lock = LinkLock()
    lock.install(backend)
    return lock
