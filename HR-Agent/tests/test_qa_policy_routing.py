"""Policy questions must not be treated as leave preparation or medical disclosure."""
import pytest

from src.contracts import ChatRequest, ModelTurn, ModelToolCall, PolicyChunk
from src.leave_intent import classify_request
from src.setup_lab import _load_policy_chunks
from test_agent_loop import ScriptedProvider, make_service, AUTH, TODAY


CASES = [
    ("Saya ingin membawa 8 hari cuti ke tahun depan. Bisa?", "carryover", "HR-LEAVE-001"),
    ("I want to carry over 8 days of leave to next year. Can I?", "carryover", "HR-LEAVE-001"),
    ("Saya sakit tiga hari. Boleh saya kirim diagnosis di chat ini?", "medical_channel", "HR-LEAVE-002"),
    ("I have been sick for three days. Can I send a diagnosis in this chat?", "medical_channel", "HR-LEAVE-002"),
]


@pytest.mark.parametrize("message,topic,document", CASES)
def test_policy_question_retrieves_before_answering(repository, message, topic, document):
    decision = classify_request(message, TODAY)
    assert decision.kind == "policy" and decision.policy_topic == topic
    repository.chunks = [PolicyChunk.model_validate(chunk) for chunk in _load_policy_chunks(None)]
    provider = ScriptedProvider([ModelTurn(response_id="policy", text=f"Kebijakan yang relevan [{document}].")])
    response = make_service(repository, provider).run(ChatRequest(message=message), AUTH)
    assert response.outcome == "answered"
    assert response.citations[0].document_id == document
    assert response.tool_calls[0].name == "search_hr_policy"
    history = provider.calls[0]["input_data"]
    assert history[1]["type"] == "function_call"
    assert history[2]["type"] == "function_call_output"
    assert history[1]["call_id"] == history[2]["call_id"] == "preflight-policy"
    assert repository.proposals == repository.tickets == repository.requests == {}


@pytest.mark.parametrize("tool,args", [
    ("create_hr_ticket", {"category": "policy", "summary": "Carryover exception"}),
    ("prepare_leave_request", {"leave_type": "annual", "start_date": "2026-11-23", "end_date": "2026-11-24", "reason": None}),
])
def test_policy_question_cannot_trigger_model_writes(repository, tool, args):
    provider = ScriptedProvider([ModelTurn(response_id="bad", tool_calls=[
        ModelToolCall(call_id="bad", name=tool, arguments=args)])])
    response = make_service(repository, provider).run(ChatRequest(message=CASES[0][0]), AUTH)
    assert response.outcome == "abstained"
    assert repository.proposals == repository.tickets == repository.requests == {}


def test_actual_disclosure_still_blocked():
    assert classify_request("Diagnosis saya diabetes. Boleh saya kirim di chat ini?", TODAY).kind == "medical"


def test_missing_policy_evidence_still_abstains(repository):
    repository.chunks = []
    provider = ScriptedProvider([ModelTurn(response_id="bad", text="Delapan hari boleh dibawa.")])
    response = make_service(repository, provider).run(ChatRequest(message=CASES[0][0]), AUTH)
    assert response.outcome == "abstained" and not response.citations
