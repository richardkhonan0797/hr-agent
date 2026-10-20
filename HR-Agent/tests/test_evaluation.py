"""Exercise release decisions with API evidence, including unsafe responses."""
from copy import deepcopy
import json

import httpx
import pytest

from src.run_evaluation import evaluate, load_cases


def make_transport(change=None):
    cases = {case.input: case for case in load_cases()}
    traces = {}
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/v1/leave-requests/me":
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/api/v1/traces/"):
            return httpx.Response(200, json=traces[request.url.path.rsplit("/", 1)[1]])
        if request.url.path != "/api/v1/chat":
            raise AssertionError("Unexpected endpoint")
        case = cases[json.loads(request.content)["message"]]
        outcome = case.expected_outcome or "answered"
        proposal = None
        if "prepare_leave_request" in case.expected_tools:
            outcome = "action_proposed"
            proposal = {
                "proposal_id": "P-1", "action_type": "leave_request", "leave_type": "annual",
                "start_date": "2026-11-02", "end_date": "2026-11-03", "business_days": 2,
                "validation_summary": [], "expires_at": "2026-11-01T00:00:00Z",
            }
        calls = [{"name": name, "status": "ok", "duration_ms": 1}
                 for name in case.expected_tools]
        usage = {"latency_ms": 100, "input_tokens": 10, "output_tokens": 5,
                 "estimated_cost": None}
        body = {
            "answer": "Synthetic answer", "outcome": outcome, "trace_id": case.case_id,
            "tool_calls": calls, "action_proposal": proposal, "usage": usage,
            "citations": [{"document_id": doc, "title": "Policy", "section": "Section",
                           "excerpt": "Evidence"} for doc in case.expected_documents],
        }
        trace = {
            "trace_id": case.case_id, "employee_id": "EMP-001", "outcome": outcome,
            "retrieved_document_ids": case.expected_documents, "tool_calls": deepcopy(calls),
            **usage,
        }
        if change is not None:
            change(case, body, trace)
        traces[case.case_id] = trace
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle), requests


def report_with(change=None):
    transport, requests = make_transport(change)
    with httpx.Client(transport=transport, base_url="http://test") as client:
        report = evaluate(client, load_cases())
    return report, requests


def test_all_cases_produce_go_and_metrics_without_confirmation():
    report, requests = report_with()
    assert report["release_decision"] == "GO"
    assert len(report["cases"]) == 15
    assert len([path for method, path in requests if method == "POST"]) == 15
    assert all(not path.endswith("/confirm") for _, path in requests)
    metrics = report["metrics"]
    assert metrics["retrieval_recall_at_3"] == 1
    assert metrics["citation_validity"] == 1
    assert metrics["tool_selection"] == 1
    assert metrics["p50_latency_ms"] == metrics["p95_latency_ms"] == 100
    assert metrics["input_tokens"] == 150
    assert metrics["output_tokens"] == 75
    assert metrics["estimated_cost"] is None


@pytest.mark.parametrize("location", ["answer", "trace_extra", "trace_error"])
def test_leak_fails_hard_gate_without_copying_secret_to_report(location):
    def leak(case, body, trace):
        if case.case_id != "medical-pii-012":
            return
        if location == "answer":
            body["answer"] = "diabetes"
        elif location == "trace_extra":
            trace["prompt"] = "Diagnosis: diabetes"
        else:
            trace["error"] = "Diagnosis: diabetes"

    report, _ = report_with(leak)
    assert report["release_decision"] == "NO-GO"
    assert not report["gates"]["hard_gates"]
    assert "diabetes" not in json.dumps(report)


def test_citation_outside_trace_and_retrieval_rank_fail():
    def invalid(case, body, trace):
        if case.expected_documents:
            trace["retrieved_document_ids"] = ["OTHER-1", "OTHER-2", "OTHER-3"]

    report, _ = report_with(invalid)
    assert report["metrics"]["retrieval_recall_at_3"] == 0
    assert not report["gates"]["citation_validity"]
    assert report["release_decision"] == "NO-GO"


def test_missing_citations_do_not_pass_vacuously():
    def omit(case, body, trace):
        body["citations"] = []

    report, _ = report_with(omit)
    assert not report["gates"]["citation_validity"]


def test_blocked_expected_tool_and_missing_proposal_fail():
    def block(case, body, trace):
        if case.case_id == "leave-valid-008":
            trace["tool_calls"][0]["status"] = "blocked"
            body["action_proposal"] = None

    report, _ = report_with(block)
    result = next(item for item in report["cases"] if item["case_id"] == "leave-valid-008")
    assert "expected_tool_missing_or_failed" in result["failures"]
    assert "proposal_missing" in result["failures"]
    assert report["release_decision"] == "NO-GO"


def test_http_failures_continue_all_cases_and_do_not_leak_body():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="private-secret"))
    with httpx.Client(transport=transport, base_url="http://test") as client:
        report = evaluate(client, load_cases())
    assert len(report["cases"]) == 15
    assert report["release_decision"] == "NO-GO"
    assert report["metrics"]["telemetry_samples"] == 0
    assert report["metrics"]["p95_latency_ms"] is None
    assert "private-secret" not in json.dumps(report)


def test_request_safety_excludes_historical_approvals():
    from src.contracts import LeaveRequestView
    from src.evaluation import check_requests

    historical = LeaveRequestView(
        request_id="old", leave_type="annual", start_date="2026-01-01",
        end_date="2026-01-02", business_days=2, status="approved",
        created_at="2026-01-01T00:00:00Z",
    )
    assert check_requests([historical], [historical]) == (True, True)
    new = historical.model_copy(update={"request_id": "new"})
    assert check_requests([historical], [historical, new]) == (False, False)
    pending = new.model_copy(update={"status": "pending_manager_review"})
    assert check_requests([historical], [historical, pending]) == (False, True)
    assert check_requests([pending], [new]) == (True, False)


def test_monitoring_uses_existing_rules_and_nearest_rank_latency():
    def slow(case, body, trace):
        trace["latency_ms"] = 9001
        trace["error"] = "provider_or_tool_failure"

    report, _ = report_with(slow)
    alerts = report["monitoring"]["alerts"]
    assert {alert["rule_id"] for alert in alerts} == {"agent-error-spike", "agent-latency-p95"}
    assert all(alert["owner"] and alert["response"] for alert in alerts)


def test_actual_api_evaluation_runs_all_supplied_cases(client):
    # The fixture has only two policies; this is deliberately NO-GO, not a fake
    # production-quality result. It exercises the real API/agent/trace contracts.
    client.headers["Authorization"] = "Bearer token-alya"
    report = evaluate(client, load_cases())
    assert len(report["cases"]) == 15
    assert report["metrics"]["telemetry_samples"] == 15
    assert report["release_decision"] == "NO-GO"
    assert all(result["request_safety"] for result in report["cases"])


def test_release_thresholds_and_hard_gate_override():
    from src.evaluation import build_report

    report, _ = report_with()
    results = report["cases"]
    ordinary = [result for result in results if not result["hard_gate"]]
    for result in ordinary[:3]:
        result["passed"] = False
        result["failures"] = ["unexpected_outcome"]
    assert build_report(results)["release_decision"] == "GO"
    ordinary[3]["passed"] = False
    assert build_report(results)["release_decision"] == "NO-GO"
    ordinary[3]["passed"] = True
    hard_case = next(result for result in results if result["hard_gate"])
    hard_case["passed"] = False
    assert not build_report(results)["gates"]["hard_gates"]


def test_retrieval_threshold_and_multiple_search_ambiguity():
    from src.evaluation import build_report

    report, _ = report_with()
    retrieval = [result for result in report["cases"]
                 if result["retrieval_recall_at_3"] is not None]
    retrieval[0]["retrieval_recall_at_3"] = 0
    assert build_report(report["cases"])["gates"]["retrieval_recall_at_3"]
    retrieval[1]["retrieval_recall_at_3"] = 0
    assert not build_report(report["cases"])["gates"]["retrieval_recall_at_3"]

    def multiple(case, body, trace):
        if case.expected_documents:
            trace["tool_calls"].append(deepcopy(trace["tool_calls"][0]))
            body["tool_calls"] = deepcopy(trace["tool_calls"])

    report, _ = report_with(multiple)
    assert report["metrics"]["retrieval_recall_at_3"] == 0


def test_provider_failure_cannot_pass_as_safe_refusal():
    def failure(case, body, trace):
        if case.hard_gate:
            body["outcome"] = trace["outcome"] = "abstained"
            trace["error"] = "provider_or_tool_failure"

    report, _ = report_with(failure)
    assert not report["gates"]["hard_gates"]


def test_runner_writes_report_without_credentials(tmp_path, monkeypatch):
    import src.run_evaluation as runner

    transport, _ = make_transport()
    client_type = httpx.Client
    project_cases = runner.load_cases()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_cases", lambda: project_cases)
    monkeypatch.setattr(runner.httpx, "Client", lambda **kwargs: client_type(
        transport=transport, **kwargs,
    ))
    report = runner.run("http://test", "private-test-token")
    output = (tmp_path / "outputs" / "evaluation_report.json").read_text()
    assert json.loads(output) == report
    assert "private-test-token" not in output


def test_evaluation_paces_cases_without_retries(monkeypatch):
    import src.run_evaluation as runner
    waits = []
    calls = []
    monkeypatch.setattr(runner.time, "sleep", waits.append)
    monkeypatch.setattr(runner, "evaluate_case", lambda client, case: calls.append(case) or {})
    monkeypatch.setattr(runner, "build_report", lambda results: results)
    cases = runner.load_cases()
    assert len(runner.evaluate(None, cases, interval_seconds=7)) == 15
    assert calls == cases
    assert waits == [7] * 14
