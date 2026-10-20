"""Allowlisted trace metadata; raw prompts and model/tool payloads are excluded."""
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .contracts import Outcome, ToolCallSummary
from .security import redact


class TraceRecord(BaseModel):
    # Keep operational metadata only. Pydantic ignores extra fields here and in
    # ToolCallSummary, so prompts, reasoning, and tool payloads are not persisted.
    # Both repositories validate through this model before storing a trace.
    trace_id: str
    employee_id: str
    outcome: Outcome
    retrieved_document_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("error")
    @classmethod
    def redact_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return redact(value)


def safe_trace(trace: dict[str, Any]) -> dict[str, Any]:
    # Validate and redact before serialization. JSON mode keeps enum values and
    # timestamps consistent between memory storage and PostgreSQL.
    record = TraceRecord.model_validate(trace)
    return record.model_dump(mode="json")
