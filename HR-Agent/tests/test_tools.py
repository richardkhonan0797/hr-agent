from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from src.contracts import AuthContext, LeaveBalance, LeaveRequestView, PrepareLeaveArgs
from src.domain import DomainError, business_days, validate_leave_request
from src.repository import MemoryRepository
from src.tools import TOOL_SCHEMAS, ToolExecutor


TODAY = date(2026, 9, 23)
AUTH = AuthContext(employee_id="EMP-001", display_name="Alya")
LEAVE = {"start_date": "2026-11-02", "end_date": "2026-11-03",
         "leave_type": "annual", "reason": None}
VALID_ARGUMENTS = {
    "search_hr_policy": {"query": "cuti tahunan", "top_k": 3},
    "get_my_leave_balance": {},
    "prepare_leave_request": LEAVE,
    "create_hr_ticket": {"category": "policy_question", "summary": "Please clarify leave"},
}


@pytest.fixture
def executor(repository, settings):
    repository.balances[AUTH.employee_id] = [
        LeaveBalance(leave_type="annual", available_days=8, as_of_date=TODAY),
    ]
    return ToolExecutor(repository, Mock(), settings, today_factory=lambda: TODAY)


def test_schemas_match_contracts():
    assert {schema["name"] for schema in TOOL_SCHEMAS} == set(VALID_ARGUMENTS)
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        assert schema["strict"] is True
        parameters = schema["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
        assert "employee_id" not in parameters["properties"]
    prepare = next(item for item in TOOL_SCHEMAS if item["name"] == "prepare_leave_request")
    assert {item["type"] for item in prepare["parameters"]["properties"]["reason"]["anyOf"]} == {"string", "null"}


@pytest.mark.parametrize("name", VALID_ARGUMENTS)
@pytest.mark.parametrize("extra", ["employee_id", "unexpected"])
def test_extra_arguments_rejected_before_dependencies(executor, name, extra):
    executor.repository = Mock()
    arguments = {**VALID_ARGUMENTS[name], extra: "EMP-002"}
    with pytest.raises(ValidationError):
        executor.execute(name, arguments, AUTH)
    assert executor.repository.mock_calls == []
    assert executor.retrieval.mock_calls == []


@pytest.mark.parametrize("name,arguments", [
    ("search_hr_policy", {"query": "x", "top_k": 3}),
    ("search_hr_policy", {"query": "cuti", "top_k": 6}),
    ("prepare_leave_request", {**LEAVE, "start_date": "bad"}),
    ("prepare_leave_request", {**LEAVE, "leave_type": "study"}),
    ("prepare_leave_request", {**LEAVE, "reason": "x" * 301}),
    ("create_hr_ticket", {"category": "unknown", "summary": "hello"}),
    ("create_hr_ticket", {"category": "privacy", "summary": "x"}),
])
def test_invalid_arguments_do_not_call_dependencies(executor, name, arguments):
    executor.repository = Mock()
    with pytest.raises(ValidationError):
        executor.execute(name, arguments, AUTH)
    assert executor.repository.mock_calls == []
    assert executor.retrieval.mock_calls == []


def test_unknown_tool(executor):
    with pytest.raises(ValueError, match="Unknown tool"):
        executor.execute("approve_leave", {}, AUTH)


def test_search_preserves_metadata(executor, repository):
    executor.retrieval.search.return_value = repository.chunks
    result, proposal = executor.execute("search_hr_policy", VALID_ARGUMENTS["search_hr_policy"], AUTH)
    executor.retrieval.search.assert_called_once_with("cuti tahunan", 3)
    assert result == [chunk.model_dump(mode="json") for chunk in repository.chunks]
    assert proposal is None


def test_balance_is_owned_and_detached(executor, repository):
    repository.balances["EMP-002"] = [LeaveBalance(leave_type="annual", available_days=99, as_of_date=TODAY)]
    result, proposal = executor.execute("get_my_leave_balance", {}, AUTH)
    assert result == [{"leave_type": "annual", "available_days": 8, "as_of_date": TODAY.isoformat()}]
    assert proposal is None
    repository.get_balances(AUTH.employee_id)[0].available_days = 0
    assert repository.get_balances(AUTH.employee_id)[0].available_days == 8
    assert repository.get_balances("missing") == []


def test_prepare_only_saves_owned_expiring_proposal(executor, repository, settings):
    before = datetime.now(timezone.utc)
    result, proposal = executor.execute("prepare_leave_request", LEAVE, AUTH)
    after = datetime.now(timezone.utc)
    assert result == proposal.model_dump(mode="json")
    assert proposal.business_days == 2
    assert proposal.requires_confirmation is True
    assert "status" not in result
    ttl = timedelta(minutes=settings.proposal_ttl_minutes)
    assert before + ttl <= proposal.expires_at <= after + ttl
    owner, stored = repository.proposals[proposal.proposal_id]
    assert owner == AUTH.employee_id
    assert stored == proposal
    assert repository.requests == {}
    assert repository.get_balances(AUTH.employee_id)[0].available_days == 8
    assert not repository.has_overlap(AUTH.employee_id, proposal.start_date, proposal.end_date)
    proposal.validation_summary.clear()
    assert stored.validation_summary
    _, second = executor.execute("prepare_leave_request", LEAVE, AUTH)
    assert second.proposal_id != proposal.proposal_id


def test_failed_validation_has_no_writes(executor, repository):
    with pytest.raises(DomainError, match="masa lalu"):
        executor.execute("prepare_leave_request", {**LEAVE, "start_date": "2026-09-22"}, AUTH)
    assert repository.proposals == {}
    assert repository.requests == {}
    assert repository.get_balances(AUTH.employee_id)[0].available_days == 8


def test_ticket_is_owned(executor, repository):
    arguments = VALID_ARGUMENTS["create_hr_ticket"]
    result, proposal = executor.execute("create_hr_ticket", arguments, AUTH)
    assert repository.tickets[result["ticket_id"]] == {"employee_id": AUTH.employee_id, **arguments}
    assert proposal is None
    second, _ = executor.execute("create_hr_ticket", arguments, AUTH)
    assert second != result
    assert MemoryRepository().tickets == {}


def test_storage_errors_propagate_without_retry(executor):
    executor.repository = Mock()
    error = RuntimeError("storage unavailable")
    executor.repository.create_ticket.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        executor.execute("create_hr_ticket", VALID_ARGUMENTS["create_hr_ticket"], AUTH)
    assert caught.value is error
    executor.repository.create_ticket.assert_called_once()


@pytest.mark.parametrize("status,blocks", [
    ("pending_manager_review", True), ("approved", True),
    ("rejected", False), ("cancelled", False),
])
def test_overlap_ownership_status_and_boundaries(repository, status, blocks):
    repository.requests[AUTH.employee_id] = [LeaveRequestView(
        request_id="R1", leave_type="annual", start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3), business_days=2, status=status,
        created_at=datetime.now(timezone.utc),
    )]
    assert repository.has_overlap(AUTH.employee_id, date(2026, 11, 1), date(2026, 11, 2)) is blocks
    assert repository.has_overlap(AUTH.employee_id, date(2026, 11, 3), date(2026, 11, 4)) is blocks
    assert not repository.has_overlap(AUTH.employee_id, date(2026, 11, 4), date(2026, 11, 5))
    assert not repository.has_overlap("EMP-002", date(2026, 11, 2), date(2026, 11, 3))
    assert MemoryRepository().requests == {}


def test_business_day_edges():
    assert business_days(TODAY, TODAY) == 1
    assert business_days(date.max, date.max) == 1
    with pytest.raises(DomainError, match="hari kerja"):
        business_days(date(2026, 9, 26), date(2026, 9, 27))
    with pytest.raises(DomainError, match="sebelum"):
        business_days(date(2026, 9, 24), TODAY)


def test_missing_balance_and_validation_precedence():
    args = PrepareLeaveArgs.model_validate(LEAVE)
    with pytest.raises(DomainError, match="tidak tersedia"):
        validate_leave_request(args, [], False, TODAY)
    with pytest.raises(DomainError, match="bertumpang tindih"):
        validate_leave_request(args, [], True, TODAY)
    balance = LeaveBalance(leave_type="annual", available_days=2, as_of_date=TODAY)
    days, summary = validate_leave_request(args, [balance], False, TODAY)
    assert days == 2 and summary
