"""Cooperative claim manager for STATUS_SPEC v1.1 (Phase 3).

Single-claim, in-memory, TTL-based. A client POSTs ``/control/claim``
with ``{owner, session_id}``; the manager mints a token, returns the
heartbeat interval and absolute expiry, and rejects any further
``/control/claim`` from a *different* session until release/expiry.

Subsequent ``/control/<verb>`` requests must carry the matching
``X-Claim-Token`` header — otherwise the API returns HTTP 423 Locked.
``/control/heartbeat`` extends the TTL; ``/control/release`` clears
the claim.

Concurrency: methods are sync but use an internal ``threading.Lock``
because heartbeats may arrive while the event loop is executing a
control verb. The lock guards the small in-memory state only — no I/O.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel

from .models import ClaimedBy


class ClaimRejectedError(Exception):
    """Raised when a claim request loses the race to another session.

    Mapped to HTTP 409 (or 423 if X-Claim-Token-on-control violates a
    held claim). Carries the currently-active holder for diagnostics.
    """

    def __init__(
        self,
        detail: str,
        *,
        claimed_by: ClaimedBy | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.claimed_by = claimed_by
        self.retry_after_s = retry_after_s


class _ActiveClaim(BaseModel):
    token: str
    owner: str
    session_id: str
    expires_at: datetime

    def to_claimed_by(self) -> ClaimedBy:
        return ClaimedBy(
            session_id=self.session_id,
            owner=self.owner,
            expires_at=self.expires_at,
        )


class ClaimManager:
    """In-memory single-holder TTL claim store."""

    # 1/3 of TTL by default — gives the SDK ~2 missed-heartbeat budget
    # before expiry. Mirrors the agilent_plateloc / filter_every_well
    # convention.
    _HEARTBEAT_DIVISOR = 3.0

    def __init__(self, *, enforce: bool = True) -> None:
        self.enforce = enforce
        self._lock = threading.Lock()
        self._claim: _ActiveClaim | None = None

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def current(self) -> ClaimedBy | None:
        with self._lock:
            self._sweep_locked()
            return self._claim.to_claimed_by() if self._claim else None

    def is_held(self) -> bool:
        return self.current() is not None

    def matches(self, token: Optional[str]) -> bool:
        """Return True iff ``token`` matches the active claim (and we
        are still holding it)."""
        if not self.enforce:
            return True
        with self._lock:
            self._sweep_locked()
            if self._claim is None:
                return False
            return token is not None and secrets.compare_digest(
                token, self._claim.token
            )

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def claim(
        self,
        *,
        owner: str,
        session_id: str,
        ttl_s: float,
    ) -> tuple[str, float, datetime]:
        """Acquire (or refresh) a claim.

        Returns ``(token, heartbeat_interval_s, expires_at)``. Raises
        :class:`ClaimRejectedError` if a different session currently
        holds the claim.

        Idempotent for the same ``session_id``: rotates the token and
        returns a fresh expiry.
        """
        ttl_s = max(1.0, min(600.0, float(ttl_s)))
        with self._lock:
            self._sweep_locked()
            if self._claim is not None and self._claim.session_id != session_id:
                holder = self._claim.to_claimed_by()
                raise ClaimRejectedError(
                    "Device is already claimed by another session",
                    claimed_by=holder,
                    retry_after_s=max(
                        1.0,
                        (self._claim.expires_at - _now()).total_seconds(),
                    ),
                )
            token = secrets.token_urlsafe(24)
            expires_at = _now() + timedelta(seconds=ttl_s)
            self._claim = _ActiveClaim(
                token=token,
                owner=owner,
                session_id=session_id,
                expires_at=expires_at,
            )
            heartbeat = max(1.0, ttl_s / self._HEARTBEAT_DIVISOR)
            return token, heartbeat, expires_at

    def heartbeat(self, token: str, *, extend_s: float | None = None) -> datetime:
        """Refresh the claim's TTL.

        Raises :class:`ClaimRejectedError` if ``token`` is unknown /
        stale (mapped to HTTP 401 by the API layer). ``extend_s`` lets
        the caller request a specific extension; otherwise we extend by
        the original TTL window (30 s default).
        """
        with self._lock:
            self._sweep_locked()
            if self._claim is None or not secrets.compare_digest(
                token, self._claim.token
            ):
                raise ClaimRejectedError("Unknown or stale claim token")
            ttl_s = max(1.0, min(600.0, float(extend_s or 30.0)))
            new_expires = _now() + timedelta(seconds=ttl_s)
            self._claim = self._claim.model_copy(update={"expires_at": new_expires})
            return new_expires

    def release(self, token: str) -> None:
        """Release the claim if ``token`` matches. Idempotent — never raises."""
        with self._lock:
            if self._claim is not None and secrets.compare_digest(
                token, self._claim.token
            ):
                self._claim = None

    def force_release(self) -> None:
        """Clear any active claim regardless of token (test / admin only)."""
        with self._lock:
            self._claim = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sweep_locked(self) -> None:
        if self._claim is not None and self._claim.expires_at <= _now():
            self._claim = None


def _now() -> datetime:
    # Centralised so tests can monkeypatch if needed.
    return datetime.now(timezone.utc)


__all__ = ["ClaimManager", "ClaimRejectedError"]
