"""Phase 3 tests for the v1.1 claim protocol + ClaimManager unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agilent_cytation_server.claims import ClaimManager, ClaimRejectedError


# ---------------------------------------------------------------------------
# ClaimManager (direct)
# ---------------------------------------------------------------------------


def test_claim_returns_token_and_heartbeat() -> None:
    cm = ClaimManager()
    token, hb, expires = cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    assert token
    assert hb == pytest.approx(10.0)  # 30 / 3
    assert (expires - datetime.now(timezone.utc)).total_seconds() <= 30.0
    holder = cm.current()
    assert holder is not None and holder.owner == "alice"


def test_claim_same_session_is_idempotent_and_rotates() -> None:
    cm = ClaimManager()
    t1, _, _ = cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    t2, _, _ = cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    assert t2  # rotates is fine; the SDK accepts either
    # Old token may or may not still be valid; current API rotates always.
    assert cm.matches(t2)


def test_claim_rejects_different_session() -> None:
    cm = ClaimManager()
    cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    with pytest.raises(ClaimRejectedError) as info:
        cm.claim(owner="bob", session_id="s2", ttl_s=30.0)
    assert info.value.claimed_by is not None
    assert info.value.claimed_by.owner == "alice"
    assert info.value.retry_after_s is not None
    assert info.value.retry_after_s >= 0.0


def test_heartbeat_extends_expiry() -> None:
    cm = ClaimManager()
    token, _, expires1 = cm.claim(owner="alice", session_id="s1", ttl_s=5.0)
    expires2 = cm.heartbeat(token, extend_s=60.0)
    assert expires2 > expires1


def test_heartbeat_wrong_token_raises() -> None:
    cm = ClaimManager()
    cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    with pytest.raises(ClaimRejectedError):
        cm.heartbeat("nope")


def test_release_clears_claim() -> None:
    cm = ClaimManager()
    token, _, _ = cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    cm.release(token)
    assert cm.current() is None


def test_release_wrong_token_is_idempotent() -> None:
    cm = ClaimManager()
    cm.claim(owner="alice", session_id="s1", ttl_s=30.0)
    # Should not raise; should not release another session's claim.
    cm.release("not-the-token")
    assert cm.current() is not None


def test_expired_claim_is_swept() -> None:
    cm = ClaimManager()
    token, _, _ = cm.claim(owner="alice", session_id="s1", ttl_s=1.0)
    # Force expiry by reaching past private state -- safer than time.sleep.
    cm._claim = cm._claim.model_copy(  # type: ignore[union-attr]
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    assert cm.current() is None
    assert not cm.matches(token)


def test_advisory_mode_skips_token_check() -> None:
    cm = ClaimManager(enforce=False)
    # In advisory mode matches() returns True regardless of token.
    assert cm.matches(None)
    assert cm.matches("anything")


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_claim_flow_via_http(client) -> None:
    r = client.post("/control/claim", json={"owner": "alice", "session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    token = body["claim_token"]
    assert body["heartbeat_interval_s"] > 0
    assert body["expires_at"]

    # /status shows claim holder.
    s = client.get("/status").json()
    assert s["details"]["claimed_by"]["owner"] == "alice"
    assert s["details"]["claims_enforced"] is True

    # Heartbeat extends.
    r = client.post("/control/heartbeat", headers={"X-Claim-Token": token})
    assert r.status_code == 204

    # Release clears.
    r = client.post("/control/release", headers={"X-Claim-Token": token})
    assert r.status_code == 204
    s = client.get("/status").json()
    assert s["details"]["claimed_by"] is None


def test_claim_conflict(client) -> None:
    r1 = client.post("/control/claim", json={"owner": "alice", "session_id": "s1"})
    assert r1.status_code == 200
    r2 = client.post("/control/claim", json={"owner": "bob", "session_id": "s2"})
    assert r2.status_code == 409
    body = r2.json()["detail"]
    assert body["claimed_by"]["owner"] == "alice"


def test_heartbeat_unknown_token_401(client) -> None:
    r = client.post("/control/heartbeat", headers={"X-Claim-Token": "garbage"})
    assert r.status_code == 401


def test_release_unknown_token_is_204(client) -> None:
    # Spec: release MUST be idempotent.
    r = client.post("/control/release", headers={"X-Claim-Token": "garbage"})
    assert r.status_code == 204


def test_control_without_token_is_423(client) -> None:
    # No claim header, claims enforced -> HTTP 423 Locked.
    r = client.post("/control/drawer/open")
    assert r.status_code == 423
    assert "X-Claim-Token" in r.json()["detail"]["detail"]


def test_control_with_token_succeeds(client) -> None:
    token = client.post(
        "/control/claim", json={"owner": "alice", "session_id": "s1"}
    ).json()["claim_token"]
    r = client.post(
        "/control/drawer/open", headers={"X-Claim-Token": token}, json={}
    )
    # 204 in dry_run mode -- the stub opens its virtual drawer.
    assert r.status_code == 204


def test_advisory_mode_allows_control_without_token(advisory_client) -> None:
    r = advisory_client.post("/control/drawer/open", json={})
    assert r.status_code == 204
