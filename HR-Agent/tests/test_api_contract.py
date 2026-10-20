from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from src.api import create_app
from src.cache import NullCache
from src.config import Settings
from src.contracts import ActionProposal
from src.monitoring import active_alerts
from src.repository import MemoryRepository
from src.runtime import assemble_runtime


ALYA = {"Authorization": "Bearer token-alya"}
BIMA = {"Authorization": "Bearer token-bima"}
ADMIN = {"Authorization": "Bearer token-admin"}


def proposal(identifier="AP-API"):
    return ActionProposal(
        proposal_id=identifier, leave_type="annual", start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def assert_problem(response, status):
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == status and body["title"] and body["detail"]
    assert set(body) == {"type", "title", "status", "detail", "trace_id"}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic token-alya", "Bearer"])
@pytest.mark.parametrize("path", ["/api/v1/leave-requests/me", "/api/v1/traces/missing",
                                  "/api/v1/monitoring/summary", "/api/v1/monitoring/alerts"])
def test_missing_or_invalid_identity(client, authorization, path):
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = client.get(path, headers=headers)
    assert_problem(response, 401)
    assert response.headers["www-authenticate"] == "Bearer"


def test_validated_chat_request_and_response(client):
    invalid = client.post("/api/v1/chat", headers=ALYA, json={"message": {"secret": "token-private"}})
    assert_problem(invalid, 422)
    assert "token-private" not in invalid.text
    malformed = client.post(
        "/api/v1/chat", headers={**ALYA, "Content-Type": "application/json"}, content='{"secret":'
    )
    assert_problem(malformed, 422)
    assert_problem(client.post("/api/v1/chat", headers=ALYA, json={"message": " "}), 422)


def test_openapi_contains_eight_endpoints_and_bearer_auth(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {
        "/healthz", "/readyz", "/api/v1/chat",
        "/api/v1/action-proposals/{proposal_id}/confirm", "/api/v1/leave-requests/me",
        "/api/v1/traces/{trace_id}", "/api/v1/monitoring/summary", "/api/v1/monitoring/alerts",
    }
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert spec["paths"]["/api/v1/chat"]["post"]["security"] == [{"HTTPBearer": []}]
    schema = spec["paths"]["/api/v1/chat"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/ChatResponse")
    errors = spec["paths"]["/api/v1/chat"]["post"]["responses"]["422"]["content"]
    assert set(errors) == {"application/problem+json"}


def test_readiness_redis_degrades_without_blocking(client, repository):
    runtime = client.app.state.runtime
    runtime.cache = Mock()
    runtime.cache.health.return_value = True
    assert client.get("/readyz").json()["status"] == "ready"
    runtime.cache.health.return_value = False
    response = client.get("/readyz")
    assert response.status_code == 200 and response.json()["status"] == "degraded"
    repository.health = Mock(return_value=False)
    assert_problem(client.get("/readyz"), 503)
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_model_configuration_exposes_health_but_blocks_chat(repository):
    settings = Settings(_env_file=None, model_mode="live", openai_api_key="",
                        agent_model="", embedding_model="")
    with TestClient(create_app(settings=settings, repository=repository, cache=NullCache())) as client:
        assert client.get("/healthz").status_code == 200
        assert_problem(client.get("/readyz"), 503)
        assert_problem(client.post("/api/v1/chat", headers=ALYA, json={"message": "saldo"}), 503)


def test_trace_owner_and_admin_only(client):
    result = client.post("/api/v1/chat", headers=ALYA, json={"message": "jatah cuti tahunan"}).json()
    path = "/api/v1/traces/" + result["trace_id"]
    assert client.get(path, headers=ALYA).status_code == 200
    assert client.get(path, headers=ADMIN).status_code == 200
    assert_problem(client.get(path, headers=BIMA), 403)
    assert_problem(client.get("/api/v1/traces/missing", headers=ALYA), 404)


def test_confirmation_owner_replay_expiry_and_listing(client, repository):
    repository.save_proposal("EMP-001", proposal())
    url = "/api/v1/action-proposals/AP-API/confirm"
    body = {"idempotency_key": "api-key-123"}
    assert_problem(client.post(url, headers=BIMA, json=body), 404)
    assert_problem(client.post(url, headers=ADMIN, json=body), 404)
    assert_problem(client.post(url, headers=ALYA, json={"idempotency_key": "short"}), 422)
    first = client.post(url, headers=ALYA, json=body)
    replay = client.post(url, headers=ALYA, json=body)
    assert first.status_code == 200 and replay.status_code == 200
    assert first.json()["status"] == "pending_manager_review"
    assert first.json()["audit_reference"]
    assert replay.json()["request_id"] == first.json()["request_id"]
    assert replay.json()["idempotent_replay"] is True
    assert_problem(client.post(url, headers=ALYA, json={"idempotency_key": "another-key"}), 409)
    assert len(client.get("/api/v1/leave-requests/me", headers=ALYA).json()) == 1
    assert client.get("/api/v1/leave-requests/me", headers=BIMA).json() == []
    expired = proposal("EXPIRED").model_copy(update={
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    repository.save_proposal("EMP-001", expired)
    assert_problem(client.post("/api/v1/action-proposals/EXPIRED/confirm", headers=ALYA,
                               json={"idempotency_key": "expired-key"}), 409)
    repository.save_proposal("EMP-001", proposal("SECOND"))
    assert_problem(client.post("/api/v1/action-proposals/SECOND/confirm", headers=ALYA, json=body), 409)


def test_memory_confirmation_is_atomic(repository):
    repository.save_proposal("EMP-001", proposal())
    def confirm(_):
        return repository.confirm_proposal("EMP-001", "AP-API", "concurrent-key")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(confirm, range(8)))
    assert len({result.request_id for result in results}) == 1
    assert sum(not result.idempotent_replay for result in results) == 1
    assert len(repository.list_leave_requests("EMP-001")) == 1


def test_monitoring_rules_and_admin_authorization(client, repository):
    assert_problem(client.get("/api/v1/monitoring/alerts", headers=ALYA), 403)
    assert client.get("/api/v1/monitoring/alerts", headers=ADMIN).json() == []
    assert repository.monitoring_summary() == {
        "run_count": 0, "error_rate": 0.0, "p95_latency_ms": 0, "tool_calls": 0,
    }
    for index in range(5):
        repository.save_trace({"trace_id": str(index), "employee_id": "EMP-001",
                               "outcome": "abstained", "error": "provider_failure" if index < 2 else None,
                               "latency_ms": 9000, "tool_calls": [
                                   {"name": "search_hr_policy", "status": "ok", "duration_ms": 1},
                               ]})
    response = client.get("/api/v1/monitoring/summary", headers=ADMIN)
    assert response.json() == {"run_count": 5, "error_rate": 0.4, "p95_latency_ms": 9000, "tool_calls": 5}
    alerts = client.get("/api/v1/monitoring/alerts", headers=ADMIN).json()
    assert {item["rule_id"] for item in alerts} == {"agent-error-spike", "agent-latency-p95"}
    assert active_alerts({"run_count": 4, "error_rate": 1, "p95_latency_ms": 9000}) == []
    assert active_alerts({"run_count": 5, "error_rate": 0.2, "p95_latency_ms": 8000}) == []


@pytest.mark.parametrize("invalid_result", [True, False])
def test_internal_errors_and_invalid_agent_response_are_safe(repository, settings, invalid_result):
    agent = Mock()
    if invalid_result:
        agent.run.return_value = {"answer": "token-secret_ABCDEF123"}
    else:
        agent.run.side_effect = RuntimeError("secret prompt token-secret_ABCDEF123")
    with TestClient(create_app(settings=settings, repository=repository, cache=NullCache(), agent=agent),
                    raise_server_exceptions=False) as client:
        response = client.post("/api/v1/chat", headers=ALYA, json={"message": "saldo"})
        assert_problem(response, 500)
        assert "ABCDEF123" not in response.text and "secret prompt" not in response.text
        assert_problem(client.get("/missing"), 404)
        assert_problem(client.get("/api/v1/chat"), 405)


def test_authentication_uses_only_token_hashes(repository):
    assert "token-alya" not in repository.identities
    assert repository.authenticate("token-alya").employee_id == "EMP-001"
    assert repository.authenticate("token-bima").employee_id == "EMP-002"
    assert repository.authenticate("token-admin").role == "monitoring_admin"
    assert repository.authenticate("' OR 1=1 --") is None


def test_runtime_closes_only_owned_dependencies(monkeypatch, repository, settings):
    built_repository = Mock()
    built_cache = Mock()
    monkeypatch.setattr("src.runtime.PostgresRepository", Mock(return_value=built_repository))
    monkeypatch.setattr("src.runtime.ResilientCache", Mock(return_value=built_cache))
    no_model = Settings(_env_file=None, openai_api_key="", agent_model="", embedding_model="")
    with TestClient(create_app(settings=no_model)):
        pass
    built_repository.close.assert_called_once()
    built_cache.close.assert_called_once()
    cache = Mock()
    agent = Mock()
    with TestClient(create_app(settings=settings, repository=repository, cache=cache, agent=agent)):
        pass
    cache.close.assert_not_called()


def test_runtime_cleanup_on_construction_error(monkeypatch):
    repository = Mock()
    monkeypatch.setattr("src.runtime.PostgresRepository", Mock(return_value=repository))
    monkeypatch.setattr("src.runtime.ResilientCache", Mock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        assemble_runtime(Settings(_env_file=None))
    repository.close.assert_called_once()
