"""Step 8: preparation has no request side effect; confirmation creates one pending request."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest

from src.contracts import ActionProposal, LeaveBalance
from src.repository import ConflictError


def proposal(identifier: str) -> ActionProposal:
    return ActionProposal(
        proposal_id=identifier, leave_type="annual", start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def test_prepare_then_explicit_confirmation_checkpoint(client, repository):
    auth = {"Authorization": "Bearer token-alya"}
    repository.balances["EMP-001"] = [
        LeaveBalance(leave_type="annual", available_days=8, as_of_date=date(2026, 1, 1)),
    ]
    balances = repository.get_balances("EMP-001")
    response = client.post("/api/v1/chat", headers=auth, json={
        "message": "Siapkan cuti tahunan 2026-11-02 sampai 2026-11-03",
    })
    assert response.status_code == 200
    prepared = response.json()
    assert prepared["outcome"] == "action_proposed"
    assert prepared["action_proposal"]["requires_confirmation"] is True
    assert client.get("/api/v1/leave-requests/me", headers=auth).json() == []
    assert repository.get_balances("EMP-001") == balances

    proposal_id = prepared["action_proposal"]["proposal_id"]
    path = f"/api/v1/action-proposals/{proposal_id}/confirm"
    body = {"idempotency_key": "checkpoint-key"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer token-bima"}, json=body).status_code == 404
    assert repository.list_leave_requests("EMP-001") == []
    first = client.post(path, headers=auth, json=body).json()
    replay = client.post(path, headers=auth, json=body).json()
    assert first["status"] == replay["status"] == "pending_manager_review"
    assert first["request_id"] == replay["request_id"]
    assert first["audit_reference"] == replay["audit_reference"]
    assert first["idempotent_replay"] is False and replay["idempotent_replay"] is True
    requests = client.get("/api/v1/leave-requests/me", headers=auth).json()
    assert len(requests) == 1 and requests[0]["request_id"] == first["request_id"]
    assert repository.get_balances("EMP-001") == balances


def test_same_key_competing_proposals_create_only_one_request(repository):
    for identifier in ("P1", "P2"):
        repository.save_proposal("EMP-001", proposal(identifier))
    start = Barrier(2)

    def confirm(identifier):
        start.wait(timeout=5)
        try:
            return repository.confirm_proposal("EMP-001", identifier, "shared-key")
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, ("P1", "P2")))
    assert sum(result is not None for result in results) == 1
    assert len(repository.list_leave_requests("EMP-001")) == 1


def test_idempotency_keys_are_scoped_to_owner(repository):
    repository.save_proposal("EMP-001", proposal("ALYA"))
    repository.save_proposal("EMP-002", proposal("BIMA"))
    alya = repository.confirm_proposal("EMP-001", "ALYA", "shared-key")
    bima = repository.confirm_proposal("EMP-002", "BIMA", "shared-key")
    assert alya.request_id != bima.request_id
    with pytest.raises(KeyError):
        repository.confirm_proposal("EMP-002", "ALYA", "shared-key")


def test_expiry_boundary_and_replay_do_not_mutate_original_result(repository, monkeypatch):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("src.repository.datetime", FixedDateTime)
    expired = proposal("EXPIRED").model_copy(update={"expires_at": now})
    repository.save_proposal("EMP-001", expired)
    with pytest.raises(ConflictError, match="expired"):
        repository.confirm_proposal("EMP-001", "EXPIRED", "expiry-key")
    assert repository.list_leave_requests("EMP-001") == []

    valid = proposal("VALID").model_copy(update={"expires_at": now + timedelta(seconds=1)})
    repository.save_proposal("EMP-001", valid)
    first = repository.confirm_proposal("EMP-001", "VALID", "valid-key")
    now += timedelta(seconds=2)
    replay = repository.confirm_proposal("EMP-001", "VALID", "valid-key")
    assert replay.request_id == first.request_id and replay.idempotent_replay is True
    replay.request_id = "client-side-change"
    assert repository.confirm_proposal("EMP-001", "VALID", "valid-key").request_id == first.request_id
    assert first.idempotent_replay is False
