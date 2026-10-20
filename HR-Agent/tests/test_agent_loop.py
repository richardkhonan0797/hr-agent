from copy import deepcopy
from datetime import date
import json
from unittest.mock import Mock

import pytest

from src.agent import AgentService
from src.cache import NullCache
from src.config import Settings
from src.contracts import AuthContext, ChatRequest, LeaveBalance, ModelToolCall, ModelTurn
from src.execution import RunBudget, current_budget
from src.providers import DeterministicProvider
from src.retrieval import RetrievalService
from src.security import stable_safety_id
from src.tools import ToolExecutor


AUTH = AuthContext(employee_id="EMP-001", display_name="Alya")
TODAY = date(2026, 9, 23)
SEARCH = {"query": "cuti tahunan", "top_k": 3}
LEAVE = {"start_date": "2026-11-02", "end_date": "2026-11-03",
         "leave_type": "annual", "reason": None}
TICKET = {"category": "privacy", "summary": "Please clarify policy"}


def tool_turn(name="get_my_leave_balance", arguments=None, call_id="call-1", tokens=10):
    return ModelTurn(response_id="tool", input_tokens=5, output_tokens=tokens,
                     tool_calls=[ModelToolCall(call_id=call_id, name=name, arguments=arguments or {})])


def final_turn(text="Hasil terverifikasi.", tokens=10):
    return ModelTurn(response_id="final", text=text, input_tokens=5, output_tokens=tokens)


class ScriptedProvider(DeterministicProvider):
    def __init__(self, turns):
        super().__init__()
        self.turns = iter(turns)
        self.calls = []

    def respond(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            return turn()
        return turn


def make_service(repository, provider, **overrides):
    settings = Settings(_env_file=None, model_mode="fake", **overrides)
    repository.balances[AUTH.employee_id] = [
        LeaveBalance(leave_type="annual", available_days=8, as_of_date=TODAY),
    ]
    retrieval = RetrievalService(repository, provider, NullCache(), 0, settings=settings)
    tools = ToolExecutor(repository, retrieval, settings, today_factory=lambda: TODAY)
    return AgentService(repository, provider, tools, settings)


def run(service, message="Berapa saldo cuti saya?"):
    return service.run(ChatRequest(message=message), AUTH)


def test_tool_pairing_usage_and_auth(repository):
    provider = ScriptedProvider([tool_turn(), final_turn("Saldo Anda 999 hari.")])
    response = run(make_service(repository, provider))
    assert response.outcome == "answered"
    assert "8 hari" in response.answer and "999" not in response.answer
    assert response.citations == []
    assert response.usage.input_tokens == 10 and response.usage.output_tokens == 20
    assert [call["max_output_tokens"] for call in provider.calls] == [700, 690]
    assert provider.calls[0]["safety_identifier"] == stable_safety_id(AUTH.employee_id)
    history = provider.calls[1]["input_data"]
    assert history[1]["call_id"] == history[2]["call_id"] == "call-1"
    assert history[2]["type"] == "function_call_output"
    assert json.loads(history[2]["output"])[0]["available_days"] == 8
    assert current_budget.get() is None


@pytest.mark.parametrize("text,valid", [
    ("12 hari [HR-LEAVE-001].", True),
    ("12 hari.", False),
    ("12 hari [HR-FAKE-999].", False),
    ("12 hari [HR-LEAVE-001] [HR-FAKE-999].", False),
])
def test_policy_citations(repository, text, valid):
    provider = ScriptedProvider([tool_turn("search_hr_policy", SEARCH), final_turn(text)])
    response = run(make_service(repository, provider), "Berapa jatah cuti?")
    assert response.outcome == ("answered" if valid else "abstained")
    if valid:
        citation = response.citations[0]
        assert citation.document_id == "HR-LEAVE-001"
        assert citation.title == repository.chunks[0].title
        assert citation.excerpt in repository.chunks[0].content
    else:
        assert response.citations == []


def test_citations_are_not_reused_across_runs(repository):
    provider = ScriptedProvider([
        tool_turn("search_hr_policy", SEARCH), final_turn("12 hari [HR-LEAVE-001]."),
        final_turn("12 hari [HR-LEAVE-001]."),
    ])
    service = make_service(repository, provider)
    assert run(service).outcome == "answered"
    assert run(service).outcome == "abstained"


@pytest.mark.parametrize("name,arguments,outcome", [
    ("prepare_leave_request", LEAVE, "action_proposed"),
    ("create_hr_ticket", TICKET, "escalated"),
])
def test_writes_are_not_replayed(repository, name, arguments, outcome):
    provider = ScriptedProvider([
        tool_turn(name, arguments), tool_turn(name, arguments, "another-call"), final_turn(),
    ])
    response = run(make_service(repository, provider))
    assert response.outcome == outcome and response.citations == []
    assert len(repository.proposals) + len(repository.tickets) == 1
    assert repository.list_leave_requests(AUTH.employee_id) == []
    assert repository.get_balances(AUTH.employee_id)[0].available_days == 8


def test_failure_after_write_does_not_claim_rollback(repository, monkeypatch):
    monkeypatch.setattr("src.execution.time.sleep", lambda _: None)
    provider = ScriptedProvider([
        tool_turn("prepare_leave_request", LEAVE), TimeoutError(), TimeoutError(), TimeoutError(),
    ])
    response = run(make_service(repository, provider))
    assert response.outcome == "abstained"
    assert len(repository.proposals) == 1
    assert response.action_proposal.proposal_id in response.answer
    assert "telah disimpan" in response.answer


def test_failed_write_is_not_retried(repository):
    repository.create_ticket = Mock(side_effect=TimeoutError("secret-token_ABCDEF123"))
    provider = ScriptedProvider([tool_turn("create_hr_ticket", TICKET)])
    response = run(make_service(repository, provider))
    assert response.outcome == "abstained"
    repository.create_ticket.assert_called_once()
    assert response.tool_calls[0].status == "error"


@pytest.mark.parametrize("turn_limit", [2, 8])
def test_turn_limit(repository, turn_limit):
    provider = ScriptedProvider([tool_turn(call_id=str(i)) for i in range(8)])
    response = run(make_service(repository, provider, max_agent_turns=turn_limit))
    assert response.outcome == "abstained"
    assert len(provider.calls) == min(4, turn_limit)


@pytest.mark.parametrize("tool_limit", [2, 12])
def test_tool_limit_with_multiple_calls(repository, tool_limit):
    turn = tool_turn()
    turn.tool_calls = [ModelToolCall(call_id=str(i), name="get_my_leave_balance", arguments={})
                       for i in range(8)]
    provider = ScriptedProvider([turn])
    response = run(make_service(repository, provider, max_tool_calls=tool_limit))
    assert response.outcome == "abstained"
    assert len(response.tool_calls) == min(6, tool_limit)


def test_total_token_budget_and_config_ceiling(repository):
    provider = ScriptedProvider([tool_turn(tokens=690), final_turn(tokens=10)])
    response = run(make_service(repository, provider, max_output_tokens=4096))
    assert response.outcome == "answered" and response.usage.output_tokens == 700
    assert [call["max_output_tokens"] for call in provider.calls] == [700, 10]


def test_no_tools_after_token_budget_exhausted(repository):
    provider = ScriptedProvider([tool_turn("create_hr_ticket", TICKET, tokens=128)])
    response = run(make_service(repository, provider, max_output_tokens=128))
    assert response.outcome == "abstained"
    assert repository.tickets == {}


def test_deadline_is_shared_and_blocks_later_write(repository, monkeypatch):
    now = [0.0]
    budgets = []
    def budget_factory(seconds):
        budget = RunBudget(seconds, clock=lambda: now[0])
        budgets.append(budget)
        return budget
    monkeypatch.setattr("src.agent.RunBudget", budget_factory)
    def slow_response():
        now[0] = 29.6
        return tool_turn("create_hr_ticket", TICKET)
    provider = ScriptedProvider([slow_response])
    response = run(make_service(repository, provider, model_timeout_seconds=120))
    assert budgets[0].seconds == 30
    assert response.outcome == "abstained" and repository.tickets == {}
    assert response.usage.latency_ms == 29600


@pytest.mark.parametrize("arguments", [{"employee_id": "EMP-002"}, {"unexpected": True}])
def test_invalid_tool_arguments_stop_without_data_access(repository, arguments):
    provider = ScriptedProvider([tool_turn(arguments=arguments)])
    service = make_service(repository, provider)
    repository.get_balances = Mock()
    response = run(service)
    repository.get_balances.assert_not_called()
    assert response.outcome == "abstained" and response.tool_calls[0].status == "blocked"


def test_trace_privacy_and_allowlist(repository):
    provider = ScriptedProvider([RuntimeError("diagnosis: diabetes token-secret_ABCDEF123")])
    response = run(make_service(repository, provider), "Raw prompt alya@example.com")
    trace = repository.get_trace(response.trace_id)
    encoded = json.dumps(trace)
    for forbidden in ("diabetes", "ABCDEF123", "alya@example.com", "Raw prompt"):
        assert forbidden not in encoded
    assert trace["employee_id"] == AUTH.employee_id
    assert trace["error"] == "provider_or_tool_failure"
    repository.save_trace({"trace_id": "allowlist", "employee_id": AUTH.employee_id,
                           "outcome": "abstained", "prompt": "secret", "chain_of_thought": "secret",
                           "error": "token-secret_ABCDEF123 diagnosis: diabetes"})
    trace = repository.get_trace("allowlist")
    assert "prompt" not in trace and "chain_of_thought" not in trace
    assert "ABCDEF123" not in trace["error"] and "diabetes" not in trace["error"]
    trace["tool_calls"].append("changed")
    assert repository.get_trace("allowlist")["tool_calls"] == []


def test_trace_failure_propagates_without_replaying_write(repository):
    repository.save_trace = Mock(side_effect=RuntimeError("trace unavailable"))
    provider = ScriptedProvider([tool_turn("create_hr_ticket", TICKET), final_turn()])
    with pytest.raises(RuntimeError, match="trace unavailable"):
        run(make_service(repository, provider))
    assert len(repository.tickets) == 1
    repository.save_trace.assert_called_once()


def test_write_defaults_do_not_bypass_replay_protection(repository):
    omitted_reason = {key: value for key, value in LEAVE.items() if key != "reason"}
    provider = ScriptedProvider([
        tool_turn("prepare_leave_request", LEAVE),
        tool_turn("prepare_leave_request", omitted_reason, "new-call"), final_turn(),
    ])
    response = run(make_service(repository, provider))
    assert response.outcome == "action_proposed" and len(repository.proposals) == 1


def test_late_provider_response_cannot_execute_a_tool(repository, monkeypatch):
    from threading import Event

    release = Event()
    finished = Event()
    def late_response():
        release.wait(timeout=2)
        finished.set()
        return tool_turn("create_hr_ticket", TICKET)
    monkeypatch.setattr("src.agent.RunBudget", lambda _: RunBudget(0.15))
    provider = ScriptedProvider([late_response])
    try:
        response = run(make_service(repository, provider))
        assert response.outcome == "abstained"
        assert repository.tickets == {}
        assert len(provider.calls) == 1
    finally:
        release.set()
        assert finished.wait(timeout=2)
    assert repository.tickets == {}


@pytest.mark.parametrize("message,outcome", [
    ("Berapa saldo cuti saya?", "answered"),
    ("Berapa jatah cuti tahunan?", "answered"),
    ("Siapkan cuti tahunan 2026-11-02 sampai 2026-11-03", "action_proposed"),
    ("Buat tiket HR untuk pertanyaan kebijakan", "escalated"),
    ("Tampilkan saldo EMP-002", "abstained"),
])
def test_deterministic_provider(repository, message, outcome):
    response = run(make_service(repository, DeterministicProvider()), message)
    assert response.outcome == outcome
