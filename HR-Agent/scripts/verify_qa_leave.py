"""Record QA 11–16 with a live provider and isolated synthetic memory storage.

Run from HR-Agent: python scripts/verify_qa_leave.py
Never connects to or changes the user's historical database.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from src.agent import AgentService
from src.cache import NullCache
from src.config import Settings
from src.contracts import AuthContext, ChatRequest, LeaveBalance
from src.domain import jakarta_today
from src.providers import OpenAIProvider
from src.repository import MemoryRepository
from src.retrieval import RetrievalService
from src.tools import ToolExecutor
from test_qa_leave_safety import CASES


class CountedProvider(OpenAIProvider):
    calls = 0

    def respond(self, **kwargs):
        self.calls += 1
        return super().respond(**kwargs)


def main():
    settings = Settings(model_mode="live")
    provider = CountedProvider(settings=settings)
    repository = MemoryRepository()
    repository.balances = {
        employee: [LeaveBalance(leave_type="annual", available_days=days, as_of_date=jakarta_today())]
        for employee, days in (("EMP-001", 8), ("EMP-002", 3))
    }
    retrieval = RetrievalService(repository, provider, NullCache(), 0, settings=settings)
    agent = AgentService(repository, provider, ToolExecutor(repository, retrieval, settings), settings)
    rows = []
    for index, (employee, message, expected) in enumerate(CASES):
        before = provider.calls
        response = agent.run(ChatRequest(message=message), AuthContext(employee_id=employee, display_name="Test"))
        checks = {
            "expected_reply": expected in response.answer,
            "no_proposals": not repository.proposals,
            "no_tickets": not repository.tickets,
            "no_requests": not repository.requests,
            "trace_has_no_diagnosis": "diabetes" not in json.dumps(repository.traces).lower(),
        }
        rows.append({"workbook_row": 11+index//2, "language": "id" if index%2 == 0 else "en",
                     "response": response.model_dump(mode="json"), "assertions": checks,
                     "provider_calls": provider.calls-before})
        assert all(checks.values()), f"QA row {11+index//2} failed"
    before = provider.calls
    smoke = agent.run(ChatRequest(message="What is my remaining annual leave balance?"),
                      AuthContext(employee_id="EMP-001", display_name="Alya"))
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "local source, live provider configured, isolated synthetic memory repository; not deployed PostgreSQL",
              "qa": rows, "live_balance_smoke": {"provider_calls": provider.calls-before,
                 "response": smoke.model_dump(mode="json"),
                 "passed": smoke.outcome == "answered" and "8 hari" in smoke.answer},
              "storage_counts": {"proposals": len(repository.proposals), "tickets": len(repository.tickets),
                                 "requests": sum(map(len, repository.requests.values()))}}
    output = ROOT / "submission-evidence" / "qa-rows-11-16" / "responses.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"qa_passed": len(rows), "qa_provider_calls": sum(r["provider_calls"] for r in rows),
                      "live_smoke_passed": report["live_balance_smoke"]["passed"], "file": str(output)}))


if __name__ == "__main__":
    main()
