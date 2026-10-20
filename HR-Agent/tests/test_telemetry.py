"""Step 9: preserve useful metadata without retaining sensitive payloads."""
import json

import pytest
from pydantic import ValidationError

from src.security import redact
from src.telemetry import safe_trace


@pytest.mark.parametrize(("raw", "expected"), [
    ("alya@example.com", "[EMAIL REDACTED]"),
    ("+62812-3456-7890", "[PHONE REDACTED]"),
    ("+62 812-3456-7890", "[PHONE REDACTED]"),
    ("+62\t812-3456-7890", "[PHONE REDACTED]"),
    ("081234567890", "[PHONE REDACTED]"),
    ("+62\n812-3456-7890", "+62\n812-3456-7890"),
    ("Bearer abc123", "[TOKEN REDACTED]"),
    ("api_key=abc123", "[TOKEN REDACTED]"),
    ("eyJabc.def.ghi", "[TOKEN REDACTED]"),
    ("diagnosa: diabetes", "[MEDICAL REDACTED]"),
    ("rincian medis: diabetes\nprovider unavailable",
     "[MEDICAL REDACTED]\nprovider unavailable"),
    ("provider_or_tool_failure", "provider_or_tool_failure"),
    ("", ""),
])
def test_redaction_preserves_safe_text(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_trace_keeps_metadata_and_discards_nested_payloads() -> None:
    trace = safe_trace({
        "trace_id": "TR-privacy",
        "employee_id": "EMP-001",
        "outcome": "answered",
        "retrieved_document_ids": ["HR-LEAVE-001"],
        "tool_calls": [{
            "name": "search_hr_policy", "status": "ok", "duration_ms": 12,
            "arguments": {"query": "private query"},
            "output": "private tool result",
        }],
        "latency_ms": 100,
        "input_tokens": 20,
        "output_tokens": 10,
        "estimated_cost": 0.001,
        "error": "Contact alya@example.com or +62 812-3456-7890",
        "created_at": "2026-10-16T00:00:00Z",
        "prompt": "private prompt",
        "chain_of_thought": "private reasoning",
    })
    assert trace == {
        "trace_id": "TR-privacy", "employee_id": "EMP-001", "outcome": "answered",
        "retrieved_document_ids": ["HR-LEAVE-001"],
        "tool_calls": [{"name": "search_hr_policy", "status": "ok", "duration_ms": 12}],
        "latency_ms": 100, "input_tokens": 20, "output_tokens": 10,
        "estimated_cost": 0.001, "error": "Contact [EMAIL REDACTED] or [PHONE REDACTED]",
        "created_at": "2026-10-16T00:00:00Z",
    }


@pytest.mark.parametrize("field", ["latency_ms", "input_tokens", "output_tokens", "estimated_cost"])
def test_negative_trace_metrics_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        safe_trace({"trace_id": "TR-invalid", "employee_id": "EMP-001",
                    "outcome": "abstained", field: -1})


def test_policy_chat_persists_metadata_without_user_message(client, repository) -> None:
    message = "Berapa jatah cuti tahunan? Hubungi alya@example.com"
    response = client.post(
        "/api/v1/chat", headers={"Authorization": "Bearer token-alya"},
        json={"message": message},
    )
    assert response.status_code == 200
    body = response.json()
    trace = repository.get_trace(body["trace_id"])
    assert trace["outcome"] == body["outcome"] == "answered"
    assert "HR-LEAVE-001" in trace["retrieved_document_ids"]
    assert trace["tool_calls"] == body["tool_calls"]
    assert trace["tool_calls"][0]["name"] == "search_hr_policy"
    for field in ("latency_ms", "input_tokens", "output_tokens", "estimated_cost"):
        assert trace[field] == body["usage"][field]
    assert trace["error"] is None
    stored = json.dumps(trace)
    assert message not in stored
    assert "alya@example.com" not in stored
    assert "token-alya" not in stored
