from __future__ import annotations

from typing import Any, Protocol
import math
import json
import re

from openai import OpenAI

from .config import Settings, get_settings

from .contracts import ModelToolCall, ModelTurn
from .search_text import deterministic_embedding
from .execution import call_with_retry, current_budget, remaining_timeout


class ModelProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def respond(
        self, *, input_data: str | list[Any], instructions: str,
        tools: list[dict[str, Any]],
        safety_identifier: str, max_output_tokens: int,
    ) -> ModelTurn: ...


def validate_embeddings(vectors: list[list[float]], count: int, dimensions: int) -> None:
    # Check shape before values so each vector can safely fit the configured column.
    if len(vectors) != count:
        raise ValueError("Embedding count does not match input count")
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError(f"Expected embedding dimension {dimensions}, got {len(vector)}")
        for value in vector:
            if not _is_valid_embedding_value(value):
                raise ValueError("Embeddings must contain finite float32 numbers")


def _is_valid_embedding_value(value: float) -> bool:
    # Python treats booleans as integers, but they are not embedding coordinates.
    # Reject non-numbers before arithmetic, then enforce pgvector's float32 range.
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    maximum_float32 = 3.4028234663852886e38
    return abs(value) <= maximum_float32


class OpenAIProvider:
    """Responses function calling and batched embeddings with bounded requests."""

    def __init__(self, *, settings: Settings | None = None, client: Any = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.embedding_model.strip():
            raise ValueError("EMBEDDING_MODEL is required")
        if client is None and not self.settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required")
        # An injected client supports network-free tests and needs no local API key.
        # Only construct a client when none was supplied, retaining bounded retries.
        if client is not None:
            self.client = client
        else:
            self.client = OpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                timeout=self.settings.model_timeout_seconds,
                max_retries=2,
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Validate the entire input before making requests, so a later blank text
        # cannot cause an avoidable partial sequence of provider calls.
        for text in texts:
            if not text.strip():
                raise ValueError("Embedding input must not be blank")

        vectors: list[list[float]] = []
        batch_size = 32
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]
            batch_vectors = self._embed_batch(batch)
            vectors.extend(batch_vectors)
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        request_arguments = self._embedding_request_arguments(batch)
        if current_budget.get() is None:
            response = self.client.embeddings.create(**request_arguments)
        else:
            # Agent embeddings share its deadline. Disable SDK retries here so
            # retry attempts recompute the remaining time instead of resetting it.
            def embed_once():
                client = self.client.with_options(
                    timeout=remaining_timeout(self.settings.model_timeout_seconds), max_retries=0,
                )
                return client.embeddings.create(**request_arguments)
            response = call_with_retry(embed_once)
        ordered_vectors = self._order_embedding_response(response, len(batch))
        validate_embeddings(ordered_vectors, len(batch), self.settings.embedding_dimensions)
        return ordered_vectors

    def _embedding_request_arguments(self, batch: list[str]) -> dict[str, Any]:
        request_arguments: dict[str, Any] = {
            "model": self.settings.embedding_model,
            "input": batch,
            "encoding_format": "float",
        }
        # Older models do not support a dimensions parameter; preserve their request shape.
        if self.settings.embedding_model.startswith("text-embedding-3-"):
            request_arguments["dimensions"] = self.settings.embedding_dimensions
        return request_arguments

    def _order_embedding_response(self, response: Any, input_count: int) -> list[list[float]]:
        # The provider may return items out of order. Require exactly one item per
        # input position before sorting, avoiding silent omissions or duplicates.
        response_indices = [item.index for item in response.data]
        expected_indices = list(range(input_count))
        if sorted(response_indices) != expected_indices:
            raise ValueError("Embedding response has missing or duplicate indices")
        ordered_items = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered_items]

    def respond(
        self, *, input_data: str | list[Any], instructions: str,
        tools: list[dict[str, Any]], safety_identifier: str, max_output_tokens: int,
    ) -> ModelTurn:
        if not self.settings.agent_model.strip():
            raise ValueError("AGENT_MODEL is required")
        client = self.client.with_options(
            timeout=remaining_timeout(min(30, self.settings.model_timeout_seconds)),
            max_retries=0,
        )
        response = client.responses.create(
            model=self.settings.agent_model, input=input_data, instructions=instructions,
            tools=tools, safety_identifier=safety_identifier, store=False,
            max_output_tokens=min(700, max_output_tokens),
            include=["reasoning.encrypted_content"],
        )
        return self._parse_turn(response)

    def _parse_turn(self, response: Any) -> ModelTurn:
        calls = []
        output_items = []
        for item in response.output:
            if item.type == "function_call":
                arguments = json.loads(item.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
                calls.append(ModelToolCall(
                    call_id=item.call_id, name=item.name, arguments=arguments,
                ))
                output_items.append(item.model_dump(exclude_none=True))
            elif item.type == "reasoning":
                # Only opaque encrypted continuation is retained in run memory.
                # Do not request, retain, log, or return reasoning summaries.
                encrypted = getattr(item, "encrypted_content", None)
                if encrypted:
                    output_items.append({"type": "reasoning", "id": item.id,
                                         "encrypted_content": encrypted, "summary": []})
            elif item.type == "message":
                output_items.append(item.model_dump(exclude_none=True))
        usage = response.usage
        if usage is None:
            raise ValueError("Provider usage is required for the token budget")
        completed = response.status == "completed"
        return ModelTurn(
            response_id=response.id,
            text=response.output_text if completed else "",
            tool_calls=calls if completed else [], output_items=output_items,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        )


class DeterministicProvider:
    """A network-free fake for tests; never the production default."""

    def __init__(self, embedding_dimensions: int = 1536) -> None:
        # Hash-based embeddings need at least one coordinate to place each token.
        if embedding_dimensions < 1:
            raise ValueError("Embedding dimensions must be positive")
        self.embedding_dimensions = embedding_dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = deterministic_embedding(text, self.embedding_dimensions)
            vectors.append(vector)
        return vectors

    def respond(self, **kwargs: Any) -> ModelTurn:
        history = kwargs["input_data"]
        if isinstance(history, str):
            message = history
            outputs = []
        else:
            message = str(history[0]["content"])
            outputs = [item for item in history if item.get("type") == "function_call_output"]
        if outputs:
            payload = json.loads(outputs[-1]["output"])
            text = "Hasil tool telah diterima."
            if isinstance(payload, list) and payload and "document_id" in payload[0]:
                chunk = payload[0]
                text = f"{chunk['content']} [{chunk['document_id']}]"
            return ModelTurn(response_id="fake-final", text=text, input_tokens=1, output_tokens=1)

        lowered = message.lower()
        if re.search(r"EMP-\d+", message, re.IGNORECASE) or any(
            word in lowered for word in ("token", "diagnosis", "setujui", "abaikan")
        ):
            return ModelTurn(response_id="fake-refusal", text="", output_tokens=1)
        if "saldo" in lowered:
            name, arguments = "get_my_leave_balance", {}
        elif "cuti" in lowered and re.search(r"\d{4}-\d{2}-\d{2}", message):
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", message)
            leave_type = "sick" if "sakit" in lowered else "annual"
            if "orang tua" in lowered:
                leave_type = "parental"
            name = "prepare_leave_request"
            arguments = {"start_date": dates[0], "end_date": dates[-1],
                         "leave_type": leave_type, "reason": None}
        elif "tiket" in lowered:
            name = "create_hr_ticket"
            arguments = {"category": "policy_question", "summary": message}
        else:
            name, arguments = "search_hr_policy", {"query": message, "top_k": 3}
        return ModelTurn(
            response_id="fake-tool", input_tokens=1, output_tokens=1,
            tool_calls=[ModelToolCall(call_id="fake-call", name=name, arguments=arguments)],
        )
