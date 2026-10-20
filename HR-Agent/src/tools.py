"""Strict tool schemas and identity-bound execution."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .config import Settings
from .contracts import (
    ActionProposal, AuthContext, PrepareLeaveArgs, SearchPolicyArgs, TicketArgs,
)
from .domain import validate_leave_request
from .repository import Repository
from .retrieval import RetrievalService


class SearchToolArgs(SearchPolicyArgs):
    model_config = ConfigDict(extra="forbid")


class PrepareToolArgs(PrepareLeaveArgs):
    model_config = ConfigDict(extra="forbid")


class TicketToolArgs(TicketArgs):
    model_config = ConfigDict(extra="forbid")


class BalanceToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _function_schema(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    parameters = model.model_json_schema()
    # Provider strict schemas require every field, even nullable/defaulted fields.
    # Derive constraints from the contracts so model-facing validation stays aligned.
    parameters["required"] = list(parameters["properties"])
    for field in parameters["properties"].values():
        field.pop("default", None)
    return {
        "type": "function", "name": name, "description": description,
        "strict": True, "parameters": parameters,
    }

# TODO(M2): Define all four Responses API function schemas with strict=True,
# additionalProperties=False, and every property listed in required.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    _function_schema("search_hr_policy", "Search HR policies.", SearchToolArgs),
    _function_schema("get_my_leave_balance", "Read your own leave balances.", BalanceToolArgs),
    _function_schema("prepare_leave_request", "Prepare leave for explicit confirmation.", PrepareToolArgs),
    _function_schema("create_hr_ticket", "Create an HR consultation ticket.", TicketToolArgs),
]


class ToolExecutor:
    def __init__(self, repository: Repository, retrieval: RetrievalService, settings: Settings,
                 today_factory: Callable[[], date] | None = None) -> None:
        self.repository = repository
        self.retrieval = retrieval
        self.settings = settings
        self.today_factory = today_factory or date.today

    def execute(self, name: str, arguments: dict[str, Any], auth: AuthContext) -> tuple[Any, ActionProposal | None]:
        # TODO(M2): Validate arguments with Pydantic and bind all private-data tools to auth.employee_id.
        
        # Identity comes only from the authenticated caller, never tool arguments.
        if name == "search_hr_policy":
            return self._search(arguments), None
        if name == "get_my_leave_balance":
            return self._get_balance(arguments, auth), None
        if name == "prepare_leave_request":
            proposal = self._prepare_leave(arguments, auth)
            return proposal.model_dump(mode="json"), proposal
        if name == "create_hr_ticket":
            return self._create_ticket(arguments, auth), None
        raise ValueError(f"Unknown tool: {name}")

    def _search(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        args = SearchToolArgs.model_validate(arguments)
        chunks = self.retrieval.search(args.query, args.top_k)
        return [chunk.model_dump(mode="json") for chunk in chunks]

    def _get_balance(self, arguments: dict[str, Any], auth: AuthContext) -> list[dict[str, Any]]:
        BalanceToolArgs.model_validate(arguments)
        balances = self.repository.get_balances(auth.employee_id)
        return [balance.model_dump(mode="json") for balance in balances]

    def _prepare_leave(self, arguments: dict[str, Any], auth: AuthContext) -> ActionProposal:
        args = PrepareToolArgs.model_validate(arguments)
        balances = self.repository.get_balances(auth.employee_id)
        overlaps = self.repository.has_overlap(auth.employee_id, args.start_date, args.end_date)
        days, summary = validate_leave_request(args, balances, overlaps, self.today_factory())
        proposal = ActionProposal(
            proposal_id="AP-" + uuid4().hex,
            leave_type=args.leave_type,
            start_date=args.start_date,
            end_date=args.end_date,
            business_days=days,
            reason=args.reason,
            validation_summary=summary,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=self.settings.proposal_ttl_minutes),
        )
        # A proposal records intent only. Confirmation owns request creation and
        # its pending status; preparing leave must not reserve or deduct balance.
        self.repository.save_proposal(auth.employee_id, proposal)
        return proposal

    def _create_ticket(self, arguments: dict[str, Any], auth: AuthContext) -> dict[str, str]:
        args = TicketToolArgs.model_validate(arguments)
        ticket_id = self.repository.create_ticket(auth.employee_id, args.category, args.summary)
        return {"ticket_id": ticket_id}

