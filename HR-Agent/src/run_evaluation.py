"""Run supplied cases against an API; never confirm proposals or retry chat."""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from .contracts import ChatResponse, LeaveRequestView
from .evaluation import EvaluationCase, assess_case, build_report, failed_case
from .telemetry import TraceRecord

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = TypeAdapter(list[LeaveRequestView])


def load_cases() -> list[EvaluationCase]:
    path = ROOT / "data" / "evaluation_cases.json"
    cases = TypeAdapter(list[EvaluationCase]).validate_json(path.read_text(encoding="utf-8"))
    ids = [case.case_id for case in cases]
    if len(cases) != 15 or len(set(ids)) != len(ids):
        raise ValueError("Expected 15 cases with unique IDs")
    return cases


def read_requests(client: httpx.Client) -> list[LeaveRequestView]:
    response = client.get("/api/v1/leave-requests/me")
    response.raise_for_status()
    return REQUESTS.validate_python(response.json())


def evaluate_case(client: httpx.Client, case: EvaluationCase) -> dict[str, Any]:
    # Baselines exclude historical approvals. None of the supplied cases gives
    # explicit confirmation, so chat must not create a leave request.
    # Never retry chat: even a lost response may have persisted a proposal/ticket.
    try:
        before = read_requests(client)
        response = client.post("/api/v1/chat", json={"message": case.input})
        response.raise_for_status()
        raw_response = response.json()
        chat = ChatResponse.model_validate(raw_response)
        trace_response = client.get("/api/v1/traces/" + quote(chat.trace_id, safe=""))
        trace_response.raise_for_status()
        raw_trace = trace_response.json()
        trace = TraceRecord.model_validate(raw_trace)
        after = read_requests(client)
        return assess_case(case, chat, trace, raw_response, raw_trace, before, after)
    except (httpx.HTTPError, ValueError, TypeError):
        # Bodies and exception strings can contain secrets. A fixed label keeps
        # reports safe and prevents infrastructure failures from passing gates.
        return failed_case(case, "api_or_contract_failure")


def evaluate(client: httpx.Client, cases: list[EvaluationCase],
             interval_seconds: float = 0) -> dict[str, Any]:
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise ValueError("Interval must be finite and nonnegative")
    results = []
    for index, case in enumerate(cases):
        if index and interval_seconds:
            time.sleep(interval_seconds)
        results.append(evaluate_case(client, case))
    return build_report(results)


def run(base_url: str, token: str, interval_seconds: float = 0) -> dict[str, Any]:
    cases = load_cases()
    with httpx.Client(base_url=base_url.rstrip("/"),
                      headers={"Authorization": "Bearer " + token}, timeout=60) as client:
        report = evaluate(client, cases, interval_seconds)
    output = ROOT / "outputs" / "evaluation_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--interval-seconds", type=float, default=0,
                        help="Delay between cases; use 7 for the public demo quota")
    arguments = parser.parse_args()
    try:
        report = run(arguments.base_url, arguments.token, arguments.interval_seconds)
    except (OSError, ValueError):
        print("Evaluation setup or report write failed.")
        return 2
    print(f"{report['release_decision']}: outputs/evaluation_report.json")
    return 0 if report["release_decision"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())

