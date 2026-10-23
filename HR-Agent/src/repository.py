"""Repository contracts. Implement MemoryRepository first, then PostgreSQL/pgvector."""
from __future__ import annotations

from datetime import date, datetime, timezone
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from copy import deepcopy
import json
import math
from typing import Any, Protocol
from uuid import uuid4
from pathlib import Path
from threading import RLock

from psycopg.rows import dict_row
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import get_settings
from .providers import validate_embeddings
from .search_text import deterministic_embedding, query_terms
from .execution import check_deadline, current_budget, remaining_timeout
from .telemetry import safe_trace
from .security import token_hash, reject_medical_details
from .domain import JAKARTA, DomainError, jakarta_today, validate_leave_request
from pydantic import ValidationError
from .monitoring import summarize_traces

from .contracts import ActionProposal, ActionResult, AuthContext, LeaveBalance, LeaveRequestView, PolicyChunk, PrepareLeaveArgs


class Repository(Protocol):
    def reserve_demo_chat(self, daily_limit: int, minute_limit: int) -> int: ...
    def health(self) -> bool: ...
    def authenticate(self, token: str) -> AuthContext | None: ...
    def lexical_search(self, query: str, limit: int) -> list[PolicyChunk]: ...
    def vector_search(self, embedding: list[float], limit: int) -> list[PolicyChunk]: ...
    def get_balances(self, employee_id: str) -> list[LeaveBalance]: ...
    def has_overlap(self, employee_id: str, start: date, end: date) -> bool: ...
    def save_proposal(self, employee_id: str, proposal: ActionProposal) -> None: ...
    def get_proposal(self, proposal_id: str) -> tuple[str, ActionProposal] | None: ...
    def confirm_proposal(self, employee_id: str, proposal_id: str, idempotency_key: str) -> ActionResult: ...
    def list_leave_requests(self, employee_id: str) -> list[LeaveRequestView]: ...
    def create_ticket(self, employee_id: str, category: str, summary: str) -> str: ...
    def save_trace(self, trace: dict[str, Any]) -> None: ...
    def get_trace(self, trace_id: str) -> dict[str, Any] | None: ...
    def monitoring_summary(self) -> dict[str, Any]: ...


class ConflictError(RuntimeError):
    pass


def validate_confirmation(proposal: ActionProposal, balances: list[LeaveBalance],
                          overlaps: bool, today: date) -> None:
    try:
        reject_medical_details(proposal.reason)
        args = PrepareLeaveArgs(start_date=proposal.start_date, end_date=proposal.end_date,
                                leave_type=proposal.leave_type, reason=proposal.reason)
        days, _ = validate_leave_request(args, balances, overlaps, today)
        if days != proposal.business_days:
            raise DomainError("Working-day count changed")
    except (DomainError, ValidationError) as error:
        raise ConflictError("Proposal no longer valid; prepare a new proposal") from error


class MemoryRepository:
    def __init__(self, *, today_factory: Callable[[], date] | None = None) -> None:
        self.today_factory = today_factory or jakarta_today
        self.chunks: list[PolicyChunk] = []
        self.balances: dict[str, list[LeaveBalance]] = {}
        self.proposals: dict[str, tuple[str, ActionProposal]] = {}
        self.requests: dict[str, list[LeaveRequestView]] = {}
        self.tickets: dict[str, dict[str, str]] = {}
        self.traces: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, AuthContext] = {}
        employees_path = Path(__file__).resolve().parents[1] / "data" / "employees.json"
        for employee in json.loads(employees_path.read_text(encoding="utf-8")):
            self.identities[token_hash(employee["token"])] = AuthContext(
                employee_id=employee["employee_id"], display_name=employee["display_name"],
                role=employee["role"],
            )
        self._confirmation_lock = RLock()
        self._confirmed: dict[str, tuple[str, ActionResult]] = {}
        self._confirmation_keys: dict[tuple[str, str], str] = {}

    def health(self) -> bool:
        return True

    def authenticate(self, token: str) -> AuthContext | None:
        auth = self.identities.get(token_hash(token))
        return auth.model_copy() if auth is not None else None

    def monitoring_summary(self) -> dict[str, Any]:
        return summarize_traces(list(self.traces.values()))

    def get_proposal(self, proposal_id: str) -> tuple[str, ActionProposal] | None:
        return deepcopy(self.proposals.get(proposal_id))

    def confirm_proposal(self, employee_id: str, proposal_id: str,
                         idempotency_key: str) -> ActionResult:
        # The lock covers checking replay state and committing the request together.
        # Ownership is checked before replay, so keys cannot expose another user's result.
        with self._confirmation_lock:
            stored = self.proposals.get(proposal_id)
            if stored is None or stored[0] != employee_id:
                raise KeyError("Proposal not found")
            replay = self._confirmation_replay(employee_id, proposal_id, idempotency_key)
            if replay is not None:
                return replay
            proposal = stored[1]
            if proposal.expires_at <= datetime.now(timezone.utc):
                raise ConflictError("Proposal expired")
            validate_confirmation(proposal, self.get_balances(employee_id),
                                  self.has_overlap(employee_id, proposal.start_date, proposal.end_date),
                                  self.today_factory())
            return self._store_pending_request(employee_id, proposal, idempotency_key)

    def _confirmation_replay(self, employee_id: str, proposal_id: str,
                             idempotency_key: str) -> ActionResult | None:
        # A successful replay returns the original result, even after expiry;
        # only creation of a new request needs a still-valid proposal.
        if proposal_id in self._confirmed:
            original_key, result = self._confirmed[proposal_id]
            if original_key != idempotency_key:
                raise ConflictError("Proposal already confirmed with another key")
            return result.model_copy(update={"idempotent_replay": True})
        if (employee_id, idempotency_key) in self._confirmation_keys:
            raise ConflictError("Idempotency key already used")
        return None

    def _store_pending_request(self, employee_id: str, proposal: ActionProposal,
                               idempotency_key: str) -> ActionResult:
        # Called while holding the confirmation lock: publish the request and
        # both replay indexes together, without deducting balance or approving it.
        result = ActionResult(request_id="LR-" + uuid4().hex, idempotent_replay=False,
                              audit_reference="AUD-" + uuid4().hex)
        request = LeaveRequestView(
            request_id=result.request_id, leave_type=proposal.leave_type,
            start_date=proposal.start_date, end_date=proposal.end_date,
            business_days=proposal.business_days, status=result.status,
            created_at=datetime.now(timezone.utc),
        )
        self.requests.setdefault(employee_id, []).append(request)
        self._confirmed[proposal.proposal_id] = (idempotency_key, result.model_copy())
        self._confirmation_keys[(employee_id, idempotency_key)] = proposal.proposal_id
        return result

    def list_leave_requests(self, employee_id: str) -> list[LeaveRequestView]:
        requests = sorted(self.requests.get(employee_id, []),
                          key=lambda item: (item.created_at, item.request_id), reverse=True)
        return [request.model_copy(deep=True) for request in requests]

    def save_trace(self, trace: dict[str, Any]) -> None:
        record = safe_trace(trace)
        if record["trace_id"] in self.traces:
            raise ConflictError("Trace already exists")
        self.traces[record["trace_id"]] = record

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return deepcopy(self.traces.get(trace_id))

    def get_balances(self, employee_id: str) -> list[LeaveBalance]:
        balances = sorted(self.balances.get(employee_id, []), key=lambda item: item.leave_type)
        return [balance.model_copy(deep=True) for balance in balances]

    def has_overlap(self, employee_id: str, start: date, end: date) -> bool:
        # Shared endpoints overlap because both start and end dates count as leave.
        for request in self.requests.get(employee_id, []):
            if request.status not in ("pending_manager_review", "approved"):
                continue
            if request.start_date <= end and request.end_date >= start:
                return True
        return False

    def save_proposal(self, employee_id: str, proposal: ActionProposal) -> None:
        reject_medical_details(proposal.reason)
        if proposal.proposal_id in self.proposals:
            raise ConflictError("Proposal already exists")
        self.proposals[proposal.proposal_id] = (employee_id, proposal.model_copy(deep=True))

    def create_ticket(self, employee_id: str, category: str, summary: str) -> str:
        reject_medical_details(summary)
        ticket_id = "HR-" + uuid4().hex
        self.tickets[ticket_id] = {
            "employee_id": employee_id, "category": category, "summary": summary,
        }
        return ticket_id

    def lexical_search(self, query: str, limit: int) -> list[PolicyChunk]:
        terms = set(query_terms(query))
        candidates = []
        for chunk in self.chunks:
            words = set(query_terms(f"{chunk.title} {chunk.section} {chunk.content}"))
            score = len(terms & words)
            if score:
                candidates.append(chunk.model_copy(update={"score": float(score)}))
        return sorted(candidates, key=lambda chunk: (-chunk.score, chunk.chunk_id))[:limit]

    def vector_search(self, embedding: list[float], limit: int) -> list[PolicyChunk]:
        if not embedding:
            raise ValueError("Embedding must not be empty")
        validate_embeddings([embedding], 1, len(embedding))
        magnitude = math.sqrt(sum(value * value for value in embedding))
        if not magnitude:
            return []
        candidates = []
        for chunk in self.chunks:
            vector = deterministic_embedding(
                f"{chunk.title}\n{chunk.section}\n{chunk.content}", len(embedding)
            )
            score = sum(left * right for left, right in zip(embedding, vector)) / magnitude
            candidates.append(chunk.model_copy(update={"score": score}))
        return sorted(candidates, key=lambda chunk: (-chunk.score, chunk.chunk_id))[:limit]

class PostgresRepository:
    def __init__(self, database_url: str, embedding_dimensions: int,
                 *, policy_index_version: str | None = None,
                 today_factory: Callable[[], date] | None = None) -> None:
        self.today_factory = today_factory
        if embedding_dimensions < 1:
            raise ValueError("Embedding dimensions must be positive")
        self.embedding_dimensions = embedding_dimensions
        self.policy_index_version = (policy_index_version if policy_index_version is not None
                                     else get_settings().policy_index_version)
        if not self.policy_index_version.strip():
            raise ValueError("Policy index version is required")
        self.pool = ConnectionPool(database_url, min_size=0, max_size=4, timeout=5, open=True,
                                   kwargs={"row_factory": dict_row, "connect_timeout": 5})

    def reserve_demo_chat(self, daily_limit: int, minute_limit: int) -> int:
        from .demo_quota import reserve
        with self._connection() as connection:
            return reserve(connection, daily_limit, minute_limit)

    def close(self) -> None:
        self.pool.close()

    def __enter__(self) -> PostgresRepository:
        return self

    def __exit__(self, *arguments: Any) -> None:
        self.close()

    def health(self) -> bool:
        # Check the existing schema without returning connection strings or SQL errors.
        try:
            with self._connection() as connection:
                row = connection.execute("""
                    SELECT bool_and(to_regclass(name) IS NOT NULL) AS ready
                    FROM unnest(ARRAY['employees', 'leave_balances', 'action_proposals',
                        'leave_requests', 'hr_tickets', 'policy_chunks', 'traces']) AS name
                """).fetchone()
            return bool(row["ready"])
        except Exception:
            return False

    def authenticate(self, token: str) -> AuthContext | None:
        with self._connection() as connection:
            row = connection.execute("""
                SELECT employee_id, display_name, role FROM employees WHERE token_hash = %s
            """, (token_hash(token),)).fetchone()
        return AuthContext.model_validate(row) if row is not None else None

    def monitoring_summary(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("""
                SELECT count(*) AS run_count,
                       COALESCE(avg((error IS NOT NULL)::int), 0) AS error_rate,
                       COALESCE(percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)
                           AS p95_latency_ms,
                       COALESCE(sum(jsonb_array_length(tool_calls)), 0) AS tool_calls
                FROM traces
            """).fetchone()
        return {"run_count": row["run_count"], "error_rate": float(row["error_rate"]),
                "p95_latency_ms": row["p95_latency_ms"], "tool_calls": row["tool_calls"]}

    def get_proposal(self, proposal_id: str) -> tuple[str, ActionProposal] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM action_proposals WHERE proposal_id = %s",
                                     (proposal_id,)).fetchone()
        if row is None:
            return None
        return row["employee_id"], ActionProposal.model_validate(row)

    def confirm_proposal(self, employee_id: str, proposal_id: str,
                         idempotency_key: str) -> ActionResult:
        with self._connection() as connection:
            with connection.transaction():
                # Lock the owner first: different proposals cannot race using one
                # idempotency key. Then lock the proposal until the insert commits.
                connection.execute("SELECT employee_id FROM employees WHERE employee_id = %s FOR UPDATE",
                                   (employee_id,)).fetchone()
                proposal = connection.execute("""
                    SELECT * FROM action_proposals
                    WHERE proposal_id = %s AND employee_id = %s FOR UPDATE
                """, (proposal_id, employee_id)).fetchone()
                if proposal is None:
                    raise KeyError("Proposal not found")
                replay = self._confirmation_replay(connection, employee_id, proposal_id, idempotency_key)
                if replay is not None:
                    return replay
                # clock_timestamp is evaluated after lock waits; transaction-start
                # time could otherwise allow a proposal that expired while waiting.
                now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                if proposal["expires_at"] <= now:
                    raise ConflictError("Proposal expired")
                balances = connection.execute("""
                    SELECT leave_type, available_days, as_of_date FROM leave_balances
                    WHERE employee_id = %s FOR UPDATE
                """, (employee_id,)).fetchall()
                overlaps = connection.execute("""
                    SELECT EXISTS(SELECT 1 FROM leave_requests WHERE employee_id = %s
                        AND status IN ('pending_manager_review', 'approved')
                        AND start_date <= %s AND end_date >= %s) AS overlaps
                """, (employee_id, proposal["end_date"], proposal["start_date"])).fetchone()["overlaps"]
                validate_confirmation(
                    ActionProposal.model_validate(proposal),
                    [LeaveBalance.model_validate(row) for row in balances], overlaps,
                    self.today_factory() if self.today_factory else now.astimezone(JAKARTA).date(),
                )
                return self._insert_pending_request(connection, employee_id, proposal, idempotency_key)

    def _confirmation_replay(self, connection: Connection, employee_id: str,
                             proposal_id: str, idempotency_key: str) -> ActionResult | None:
        rows = connection.execute("""
            SELECT proposal_id, idempotency_key, request_id, status, audit_reference
            FROM leave_requests WHERE employee_id = %s
              AND (proposal_id = %s OR idempotency_key = %s)
        """, (employee_id, proposal_id, idempotency_key)).fetchall()
        if not rows:
            return None
        for row in rows:
            if row["proposal_id"] != proposal_id or row["idempotency_key"] != idempotency_key:
                raise ConflictError("Proposal or idempotency key already used")
        row = rows[0]
        return ActionResult(request_id=row["request_id"], status=row["status"],
                            audit_reference=row["audit_reference"], idempotent_replay=True)

    def _insert_pending_request(self, connection: Connection, employee_id: str,
                                proposal: dict[str, Any], idempotency_key: str) -> ActionResult:
        result = ActionResult(request_id="LR-" + uuid4().hex, idempotent_replay=False,
                              audit_reference="AUD-" + uuid4().hex)
        connection.execute("""
            INSERT INTO leave_requests (
                request_id, employee_id, proposal_id, leave_type, start_date, end_date,
                business_days, reason, status, source, idempotency_key, audit_reference
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending_manager_review',
                      'application', %s, %s)
        """, (result.request_id, employee_id, proposal["proposal_id"], proposal["leave_type"],
              proposal["start_date"], proposal["end_date"], proposal["business_days"],
              proposal["reason"], idempotency_key, result.audit_reference))
        return result

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        # Standalone retrieval keeps its original behavior. Inside an agent run,
        # bound pool waits and SQL/lock waits by the same remaining deadline.
        if current_budget.get() is None:
            with self.pool.connection() as connection:
                yield connection
            return
        with self.pool.connection(timeout=remaining_timeout(5)) as connection:
            with connection.transaction():
                milliseconds = max(1, int(remaining_timeout(30) * 1000))
                connection.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('lock_timeout', %s, true)",
                                   (str(milliseconds), str(milliseconds)))
                yield connection
                # Expired writes roll back before leaving the transaction.
                check_deadline()

    def list_leave_requests(self, employee_id: str) -> list[LeaveRequestView]:
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT request_id, leave_type, start_date, end_date, business_days,
                       status, created_at
                FROM leave_requests WHERE employee_id = %s
                ORDER BY created_at DESC, request_id DESC
            """, (employee_id,)).fetchall()
        return [LeaveRequestView.model_validate(row) for row in rows]

    def save_trace(self, trace: dict[str, Any]) -> None:
        record = safe_trace(trace)
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO traces (
                    trace_id, employee_id, outcome, retrieved_document_ids, tool_calls,
                    latency_ms, input_tokens, output_tokens, estimated_cost, error, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (record["trace_id"], record["employee_id"], record["outcome"],
                  Jsonb(record["retrieved_document_ids"]), Jsonb(record["tool_calls"]),
                  record["latency_ms"], record["input_tokens"], record["output_tokens"],
                  record["estimated_cost"], record["error"], record["created_at"]))

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM traces WHERE trace_id = %s",
                                     (trace_id,)).fetchone()
        return safe_trace(row) if row is not None else None

    def lexical_search(self, query: str, limit: int) -> list[PolicyChunk]:
        terms = sorted(set(query_terms(query)))
        if not terms or limit <= 0:
            return []
        # Only normalized alphanumeric terms enter the query expression; all SQL values
        # remain parameters. OR matching tolerates words absent from the policy text.
        expression = " | ".join(terms)
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT chunk_id, document_id, title, section, version, effective_date,
                       content, ts_rank_cd(search_vector, search_query) AS score
                FROM policy_chunks, to_tsquery('simple', %s) AS search_query
                WHERE index_version = %s AND search_vector @@ search_query
                ORDER BY score DESC, chunk_id ASC LIMIT %s
            """, (expression, self.policy_index_version, limit)).fetchall()
        return [PolicyChunk.model_validate(row) for row in rows]

    def vector_search(self, embedding: list[float], limit: int) -> list[PolicyChunk]:
        validate_embeddings([embedding], 1, self.embedding_dimensions)
        if not any(embedding) or limit <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT chunk_id, document_id, title, section, version, effective_date,
                       content, 1 - (embedding <=> %s::vector) AS score
                FROM policy_chunks WHERE index_version = %s
                ORDER BY embedding <=> %s::vector, chunk_id ASC LIMIT %s
            """, (json.dumps(embedding, allow_nan=False), self.policy_index_version,
                  json.dumps(embedding, allow_nan=False), limit)).fetchall()
        return [PolicyChunk.model_validate(row) for row in rows]

    def get_balances(self, employee_id: str) -> list[LeaveBalance]:
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT leave_type, available_days, as_of_date
                FROM leave_balances WHERE employee_id = %s ORDER BY leave_type
            """, (employee_id,)).fetchall()
        return [LeaveBalance.model_validate(row) for row in rows]

    def has_overlap(self, employee_id: str, start: date, end: date) -> bool:
        # Only active requests block dates; inclusive comparisons cover shared endpoints.
        with self._connection() as connection:
            row = connection.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM leave_requests
                    WHERE employee_id = %s
                      AND status IN ('pending_manager_review', 'approved')
                      AND start_date <= %s AND end_date >= %s
                ) AS overlaps
            """, (employee_id, end, start)).fetchone()
        return row["overlaps"]

    def save_proposal(self, employee_id: str, proposal: ActionProposal) -> None:
        reject_medical_details(proposal.reason)
        with self._connection() as connection:
            with connection.transaction():
                connection.execute("""
                    INSERT INTO action_proposals (
                        proposal_id, employee_id, action_type, leave_type, start_date,
                        end_date, business_days, reason, validation_summary, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    proposal.proposal_id, employee_id, proposal.action_type,
                    proposal.leave_type, proposal.start_date, proposal.end_date,
                    proposal.business_days, proposal.reason,
                    Jsonb(proposal.validation_summary), proposal.expires_at,
                ))

    def create_ticket(self, employee_id: str, category: str, summary: str) -> str:
        reject_medical_details(summary)
        ticket_id = "HR-" + uuid4().hex
        with self._connection() as connection:
            with connection.transaction():
                connection.execute("""
                    INSERT INTO hr_tickets (ticket_id, employee_id, category, summary)
                    VALUES (%s, %s, %s, %s)
                """, (ticket_id, employee_id, category, summary))
        return ticket_id


