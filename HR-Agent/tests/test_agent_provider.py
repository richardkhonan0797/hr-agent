import json

import httpx
from openai import BadRequestError, InternalServerError, OpenAI
import pytest

from src.config import Settings
from src.domain import estimate_cost
from src.execution import BudgetExceeded, RunBudget, budget_scope, call_with_retry
from src.providers import OpenAIProvider
from src.security import stable_safety_id
from src.tools import TOOL_SCHEMAS


def response_payload(arguments='{"query":"cuti tahunan","top_k":3}'):
    return {
        "id": "resp-test", "object": "response", "created_at": 0,
        "status": "completed", "model": "configured-model",
        "output": [{"type": "function_call", "id": "fc-test", "call_id": "call-test",
                    "name": "search_hr_policy", "arguments": arguments, "status": "completed"}],
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    }


def provider_for(client):
    settings = Settings(_env_file=None, embedding_model="text-embedding-3-small",
                        agent_model="configured-model", model_timeout_seconds=30)
    return OpenAIProvider(settings=settings, client=client)


def respond(provider):
    return provider.respond(input_data="cuti tahunan", instructions="Test instructions",
                            tools=TOOL_SCHEMAS, safety_identifier=stable_safety_id("EMP-001"),
                            max_output_tokens=200)


def test_responses_wire_contract_and_usage():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_payload())
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        turn = respond(provider_for(client))
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert payload["model"] == "configured-model" and payload["store"] is False
    assert payload["max_output_tokens"] == 200
    assert payload["safety_identifier"] == stable_safety_id("EMP-001")
    assert payload["tools"] == TOOL_SCHEMAS
    assert all(schema["strict"] for schema in payload["tools"])
    assert turn.tool_calls[0].call_id == "call-test"
    assert turn.tool_calls[0].arguments == {"query": "cuti tahunan", "top_k": 3}
    assert (turn.input_tokens, turn.output_tokens) == (11, 7)


@pytest.mark.parametrize("arguments", ["[]", "null", '"text"', "{broken"])
def test_invalid_tool_json_is_rejected(arguments):
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response_payload(arguments))
    ))) as client:
        with pytest.raises(ValueError):
            respond(provider_for(client))


def test_plaintext_reasoning_is_not_retained():
    payload = response_payload()
    payload["output"].insert(0, {"id": "reasoning-test", "type": "reasoning",
                                 "summary": [{"type": "summary_text", "text": "SECRET REASONING"}],
                                 "encrypted_content": "opaque-continuation"})
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)
    ))) as client:
        turn = respond(provider_for(client))
    assert "SECRET REASONING" not in turn.model_dump_json()
    assert turn.output_items[0]["summary"] == []
    assert turn.output_items[0]["encrypted_content"] == "opaque-continuation"


def test_incomplete_output_keeps_usage_but_executes_no_calls():
    payload = response_payload()
    payload["status"] = "incomplete"
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)
    ))) as client:
        turn = respond(provider_for(client))
    assert turn.tool_calls == [] and turn.text == ""
    assert turn.output_tokens == 7


@pytest.mark.parametrize("status,expected_attempts,error", [
    (500, 3, InternalServerError), (400, 1, BadRequestError),
])
def test_retry_is_capped_without_sdk_nesting(monkeypatch, status, expected_attempts, error):
    monkeypatch.setattr("src.execution.time.sleep", lambda _: None)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "test"}})
    with OpenAI(api_key="test", max_retries=9,
                http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with budget_scope(RunBudget(30)), pytest.raises(error):
            call_with_retry(lambda: respond(provider_for(client)))
    assert len(requests) == expected_attempts


def test_retries_share_deadline_and_shrinking_http_timeout(monkeypatch):
    now = [0.0]
    timeouts = []
    monkeypatch.setattr("src.execution.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        now[0] += 1
        if len(timeouts) < 3:
            return httpx.Response(500, json={"error": {"message": "test"}})
        return httpx.Response(200, json=response_payload())
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with budget_scope(RunBudget(30, clock=lambda: now[0])):
            call_with_retry(lambda: respond(provider_for(client)))
    assert len(timeouts) == 3
    assert timeouts[0] > timeouts[1] > timeouts[2]
    assert timeouts[0] <= 30


def test_expired_budget_does_not_start_provider_request():
    calls = []
    now = [0.0]
    budget = RunBudget(30, clock=lambda: now[0])
    now[0] = 30
    with budget_scope(budget), pytest.raises(BudgetExceeded):
        call_with_retry(lambda: calls.append(True))
    assert calls == []


def test_optional_cost_and_safety_identifier():
    assert estimate_cost(100, 50, None, 2) is None
    assert estimate_cost(100, 50, 1, None) is None
    assert estimate_cost(1_000_000, 500_000, 2, 4) == 4
    assert stable_safety_id("EMP-001") == stable_safety_id("EMP-001")
    assert stable_safety_id("EMP-001") != stable_safety_id("EMP-002")
    assert len(stable_safety_id("EMP-001")) == 64


def test_final_text_is_parsed_and_missing_usage_is_rejected():
    payload = response_payload()
    payload["output"] = [{"type": "message", "id": "msg-test", "role": "assistant",
                          "status": "completed", "content": [
                              {"type": "output_text", "text": "Jawaban [HR-LEAVE-001].", "annotations": []},
                          ]}]
    with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)
    ))) as client:
        provider = provider_for(client)
        turn = respond(provider)
        assert turn.text == "Jawaban [HR-LEAVE-001]." and turn.tool_calls == []
        payload["usage"] = None
        with pytest.raises(ValueError, match="usage"):
            respond(provider)


def test_cache_is_skipped_when_its_timeout_would_exceed_budget():
    from unittest.mock import Mock
    from src.cache import ResilientCache

    cache = ResilientCache("redis://localhost:6379/0")
    cache.client = Mock()
    with budget_scope(RunBudget(0.2)):
        assert cache.get("key") is None
        cache.set("key", {"value": 1}, 30)
    assert cache.client.mock_calls == []
