"""Opt-in PostgreSQL checks: set TEST_DATABASE_URL to a pgvector database."""
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from src import setup_lab
from src.security import token_hash


@pytest.fixture
def seeded_database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL schema checks")
    schema = "test_seed_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            test_url = make_conninfo(url, options=f"-c search_path={schema},public")
            settings = SimpleNamespace(
                database_url=test_url, embedding_dimensions=1536, policy_index_version='test-v1',
            )
            monkeypatch.setattr(setup_lab, "get_settings", lambda: settings)
            setup_lab.reset_and_seed()
            with psycopg.connect(test_url) as conn:
                yield conn
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_seed_preserves_existing_data(seeded_database):
    conn = seeded_database
    conn.execute("UPDATE leave_balances SET available_days = 7 WHERE employee_id = 'EMP-001' AND leave_type = 'annual'")
    conn.commit()
    setup_lab.reset_and_seed()
    assert conn.execute("SELECT count(*) FROM employees").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM leave_balances").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM leave_requests").fetchone()[0] == 2
    assert conn.execute("SELECT available_days FROM leave_balances WHERE employee_id = 'EMP-001' AND leave_type = 'annual'").fetchone()[0] == 7
    assert conn.execute("SELECT token_hash FROM employees WHERE employee_id = 'EMP-001'").fetchone()[0] == token_hash("token-alya")
    assert conn.execute("SELECT count(*) FROM policy_chunks").fetchone()[0] == 0


def test_confirmation_constraints(seeded_database):
    conn = seeded_database
    conn.execute("""
        INSERT INTO action_proposals (
            proposal_id, employee_id, leave_type, start_date, end_date,
            business_days, expires_at
        ) VALUES ('P1', 'EMP-001', 'annual', '2026-11-02', '2026-11-03',
                  2, CURRENT_TIMESTAMP + INTERVAL '15 minutes')
    """)
    insert_request = """
        INSERT INTO leave_requests (
            request_id, employee_id, proposal_id, leave_type, start_date,
            end_date, business_days, idempotency_key, audit_reference, status
        ) VALUES (%s, %s, %s, 'annual', '2026-11-02', '2026-11-03',
                  2, %s, 'audit-test', %s)
    """
    cases = [
        (psycopg.errors.CheckViolation, ('R0', 'EMP-001', None, 'key-test0', 'pending_manager_review')),
        (psycopg.errors.CheckViolation, ('R0', 'EMP-001', 'P1', 'key-test0', 'approved')),
        (psycopg.errors.ForeignKeyViolation, ('R0', 'EMP-002', 'P1', 'key-test0', 'pending_manager_review')),
    ]
    for error, values in cases:
        with pytest.raises(error), conn.transaction():
            conn.execute(insert_request, values)
    conn.execute(insert_request, ('R1', 'EMP-001', 'P1', 'key-test1', 'pending_manager_review'))
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(insert_request, ('R2', 'EMP-001', 'P1', 'key-test2', 'pending_manager_review'))
    conn.execute("""
        INSERT INTO action_proposals (
            proposal_id, employee_id, leave_type, start_date, end_date,
            business_days, expires_at
        ) SELECT 'P2', employee_id, leave_type, start_date, end_date,
                 business_days, expires_at FROM action_proposals WHERE proposal_id = 'P1'
    """)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(insert_request, ('R3', 'EMP-001', 'P2', 'key-test1', 'pending_manager_review'))
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("UPDATE action_proposals SET end_date = '2026-11-01' WHERE proposal_id = 'P1'")


def test_vector_dimension_and_full_text(seeded_database):
    conn = seeded_database
    insert_chunk = """
        INSERT INTO policy_chunks (
            chunk_id, document_id, title, section, version, effective_date,
            content, embedding, index_version
        ) VALUES ('C1', 'D1', 'Cuti', 'Hak', '1', '2026-01-01',
                  'jatah cuti tahunan', %s::vector, 'v1')
    """
    with pytest.raises(psycopg.errors.DataException), conn.transaction():
        conn.execute(insert_chunk, ('[1,2,3]',))
    conn.execute(insert_chunk, ('[' + ','.join(['0'] * 1536) + ']',))
    assert conn.execute("SELECT chunk_id FROM policy_chunks WHERE search_vector @@ plainto_tsquery('simple', 'cuti')").fetchone()[0] == 'C1'


class FakeEmbeddings:
    def embed(self, texts):
        return [[0.1] * 1536 for _ in texts]


@pytest.mark.parametrize("change", ["balance", "overlap", "notice", "past", "medical"])
def test_qa_confirmation_revalidates_database_state(seeded_database, change):
    from datetime import date, datetime, timedelta, timezone
    from src.contracts import ActionProposal
    from src.repository import ConflictError, PostgresRepository

    conn = seeded_database
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            today_factory=lambda: date(2026, 10, 22)) as repo:
        proposal = ActionProposal(proposal_id="QA-P", leave_type="annual",
            start_date=date(2026, 11, 2), end_date=date(2026, 11, 3), business_days=2,
            validation_summary=[], expires_at=datetime.now(timezone.utc)+timedelta(minutes=10))
        repo.save_proposal("EMP-001", proposal)
        if change == "balance":
            conn.execute("UPDATE leave_balances SET available_days=1 WHERE employee_id='EMP-001' AND leave_type='annual'")
        elif change == "overlap":
            repo.save_proposal("EMP-001", proposal.model_copy(update={"proposal_id": "QA-OTHER"}))
            repo.confirm_proposal("EMP-001", "QA-OTHER", "other-key")
        elif change == "notice":
            repo.today_factory = lambda: date(2026, 10, 30)
        elif change == "past":
            repo.today_factory = lambda: date(2026, 11, 4)
        else:
            # Simulate a legacy record written before medical protection existed.
            conn.execute("UPDATE action_proposals SET reason=%s WHERE proposal_id='QA-P'",
                         ("My diagnosis is diabetes",))
        conn.commit()
        with pytest.raises(ConflictError):
            repo.confirm_proposal("EMP-001", "QA-P", "qa-key")
        assert conn.execute("SELECT count(*) FROM leave_requests WHERE proposal_id='QA-P'").fetchone()[0] == 0


def test_qa_postgres_replay_precedes_changed_conditions(seeded_database):
    from datetime import date, datetime, timedelta, timezone
    from src.contracts import ActionProposal
    from src.repository import PostgresRepository
    conn = seeded_database
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            today_factory=lambda: date(2026, 10, 22)) as repo:
        repo.save_proposal("EMP-001", ActionProposal(proposal_id="QA-REPLAY", leave_type="annual",
            start_date=date(2026, 11, 2), end_date=date(2026, 11, 3), business_days=2,
            validation_summary=[], expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)))
        first = repo.confirm_proposal("EMP-001", "QA-REPLAY", "qa-replay")
        conn.execute("UPDATE leave_balances SET available_days=0 WHERE employee_id='EMP-001'")
        conn.commit()
        repo.today_factory = lambda: date(2026, 11, 4)
        replay = repo.confirm_proposal("EMP-001", "QA-REPLAY", "qa-replay")
        assert replay.idempotent_replay and replay.request_id == first.request_id


def test_index_snapshot_and_versions(seeded_database, tmp_path):
    conn = seeded_database
    assert setup_lab.index_policies(provider=FakeEmbeddings()) == 15
    assert setup_lab.index_policies(provider=FakeEmbeddings()) == 15
    assert conn.execute('SELECT count(*) FROM policy_chunks').fetchone()[0] == 15
    conn.commit()
    settings = setup_lab.get_settings()
    settings.policy_index_version = 'other-version'
    setup_lab.index_policies(provider=FakeEmbeddings())
    settings.policy_index_version = 'test-v1'
    source = setup_lab.ROOT / 'data/policies/annual-leave.md'
    (tmp_path / 'annual.md').write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    assert setup_lab.index_policies(provider=FakeEmbeddings(), policy_dir=tmp_path) == 3
    assert conn.execute('SELECT index_version, count(*) FROM policy_chunks GROUP BY index_version ORDER BY index_version').fetchall() == [('other-version', 15), ('test-v1', 3)]
    conn.commit()
    path = tmp_path / 'annual.md'
    path.write_text(path.read_text(encoding='utf-8').replace(
        'Kebijakan Cuti Tahunan', 'Updated title'), encoding='utf-8')
    setup_lab.index_policies(provider=FakeEmbeddings(), policy_dir=tmp_path)
    assert conn.execute("SELECT DISTINCT title FROM policy_chunks WHERE index_version = 'test-v1'").fetchall() == [('Updated title',)]


def test_failed_index_preserves_snapshot(seeded_database):
    conn = seeded_database
    setup_lab.index_policies(provider=FakeEmbeddings())
    before = conn.execute('SELECT * FROM policy_chunks ORDER BY chunk_id').fetchall()
    conn.commit()
    class BadEmbeddings:
        def embed(self, texts):
            return [[0.1] for _ in texts]
    with pytest.raises(ValueError, match='dimension'):
        setup_lab.index_policies(provider=BadEmbeddings())
    # Force a DB error after earlier upserts; the entire transaction must roll back.
    conn.execute("""CREATE FUNCTION fail_index() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.document_id = 'HR-LEAVE-002' THEN RAISE EXCEPTION 'test failure'; END IF;
          RETURN NEW;
        END $$""")
    conn.execute('CREATE TRIGGER fail_index BEFORE INSERT ON policy_chunks FOR EACH ROW EXECUTE FUNCTION fail_index()')
    conn.commit()
    class ChangedEmbeddings:
        def embed(self, texts):
            return [[0.2] * 1536 for _ in texts]
    with pytest.raises(psycopg.errors.RaiseException):
        setup_lab.index_policies(provider=ChangedEmbeddings())
    assert conn.execute('SELECT * FROM policy_chunks ORDER BY chunk_id').fetchall() == before


def test_index_rejects_database_dimension_before_embedding(seeded_database):
    from unittest.mock import Mock
    setup_lab.get_settings().embedding_dimensions = 64
    provider = Mock()
    with pytest.raises(ValueError, match='Database embedding dimension'):
        setup_lab.index_policies(provider=provider)
    provider.embed.assert_not_called()


def test_repository_hybrid_search_and_version_isolation(seeded_database):
    import json
    from src.repository import PostgresRepository

    connection = seeded_database
    settings = setup_lab.get_settings()
    setup_lab.index_policies(provider=FakeEmbeddings())
    with PostgresRepository(settings.database_url, 1536,
                            policy_index_version=settings.policy_index_version) as repository:
        results = repository.lexical_search('jatah cuti tahunan', 3)
        assert 'HR-LEAVE-001' in [chunk.document_id for chunk in results]
        assert all(chunk.title and chunk.section and chunk.version and chunk.effective_date
                   and chunk.content for chunk in results)
        assert repository.lexical_search("' | & !", 3) == []
        with pytest.raises(ValueError, match='dimension'):
            repository.vector_search([1.0], 3)
        connection.execute('DELETE FROM policy_chunks')
        for identifier, version, vector in [
            ('aligned', 'test-v1', [1.0, 0.0] + [0.0] * 1534),
            ('orthogonal', 'test-v1', [0.0, 1.0] + [0.0] * 1534),
            ('other-version', 'other', [1.0, 0.0] + [0.0] * 1534),
        ]:
            connection.execute("""
                INSERT INTO policy_chunks (chunk_id, document_id, title, section, version,
                    effective_date, content, embedding, index_version)
                VALUES (%s, 'TEST', 'Cuti', 'Hak', '1', '2026-01-01', 'cuti tahunan', %s::vector, %s)
            """, (identifier, json.dumps(vector), version))
        connection.commit()
        results = repository.vector_search([1.0] + [0.0] * 1535, 3)
        assert [chunk.chunk_id for chunk in results] == ['aligned', 'orthogonal']
        assert [chunk.score for chunk in results] == pytest.approx([1.0, 0.0])
        assert [chunk.chunk_id for chunk in repository.lexical_search('cuti', 3)] == ['aligned', 'orthogonal']
    assert repository.pool.closed


def test_step5_storage_and_proposal_boundary(seeded_database):
    from datetime import date
    from unittest.mock import Mock

    from src.config import Settings
    from src.contracts import AuthContext
    from src.domain import DomainError
    from src.repository import PostgresRepository
    from src.tools import ToolExecutor

    connection = seeded_database
    auth = AuthContext(employee_id="EMP-001", display_name="Alya")
    arguments = {"start_date": "2026-11-02", "end_date": "2026-11-03",
                 "leave_type": "annual", "reason": "Family event"}
    settings = setup_lab.get_settings()
    before_requests = connection.execute("SELECT * FROM leave_requests ORDER BY request_id").fetchall()
    before_balances = connection.execute("SELECT * FROM leave_balances ORDER BY employee_id, leave_type").fetchall()
    connection.commit()
    with PostgresRepository(settings.database_url, 1536, policy_index_version="test-v1") as repository:
        tools = ToolExecutor(repository, Mock(), Settings(), today_factory=lambda: date(2026, 9, 23))
        assert repository.get_balances("missing") == []
        balances = repository.get_balances(auth.employee_id)
        assert balances
        expected = connection.execute(
            "SELECT leave_type, available_days, as_of_date FROM leave_balances WHERE employee_id = %s ORDER BY leave_type",
            (auth.employee_id,),
        ).fetchall()
        assert [(item.leave_type, item.available_days, item.as_of_date) for item in balances] == expected
        _, proposal = tools.execute("prepare_leave_request", arguments, auth)
        row = connection.execute("""
            SELECT employee_id, leave_type, start_date, end_date, business_days,
                   reason, validation_summary, expires_at
            FROM action_proposals WHERE proposal_id = %s
        """, (proposal.proposal_id,)).fetchone()
        assert row == (auth.employee_id, proposal.leave_type, proposal.start_date,
                       proposal.end_date, 2, proposal.reason,
                       proposal.validation_summary, proposal.expires_at)
        assert not repository.has_overlap(auth.employee_id, proposal.start_date, proposal.end_date)
        result, _ = tools.execute("create_hr_ticket", {
            "category": "policy_question", "summary": "Quoted ' text remains data",
        }, auth)
        assert connection.execute(
            "SELECT employee_id, category, summary FROM hr_tickets WHERE ticket_id = %s",
            (result["ticket_id"],),
        ).fetchone() == (auth.employee_id, "policy_question", "Quoted ' text remains data")
        with pytest.raises(DomainError):
            tools.execute("prepare_leave_request", {**arguments, "start_date": "2026-09-22"}, auth)
        assert connection.execute("SELECT count(*) FROM action_proposals").fetchone()[0] == 1
        assert connection.execute("SELECT * FROM leave_requests ORDER BY request_id").fetchall() == before_requests
        assert connection.execute("SELECT * FROM leave_balances ORDER BY employee_id, leave_type").fetchall() == before_balances

        # A failed write must roll back and leave the pooled connection usable.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repository.create_ticket("missing", "privacy", "Invalid owner")
        assert repository.get_balances(auth.employee_id) == balances
        assert connection.execute("SELECT count(*) FROM hr_tickets").fetchone()[0] == 1


@pytest.mark.parametrize("status,blocks", [
    ("pending_manager_review", True), ("approved", True),
    ("rejected", False), ("cancelled", False),
])
def test_step5_postgres_overlap(seeded_database, status, blocks):
    from datetime import date
    from src.repository import PostgresRepository

    connection = seeded_database
    connection.execute("""
        INSERT INTO leave_requests (
            request_id, employee_id, leave_type, start_date, end_date,
            business_days, status, source
        ) VALUES ('STEP5', 'EMP-001', 'annual', '2026-11-02', '2026-11-03',
                  2, %s, 'historical')
    """, (status,))
    connection.commit()
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        assert repository.has_overlap("EMP-001", date(2026, 11, 1), date(2026, 11, 2)) is blocks
        assert repository.has_overlap("EMP-001", date(2026, 11, 3), date(2026, 11, 4)) is blocks
        assert not repository.has_overlap("EMP-001", date(2026, 11, 4), date(2026, 11, 5))
        assert not repository.has_overlap("EMP-002", date(2026, 11, 2), date(2026, 11, 3))


def test_step6_trace_and_owned_request_reads(seeded_database):
    from src.repository import PostgresRepository, MemoryRepository

    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        trace = {"trace_id": "TRACE-STEP6", "employee_id": "EMP-001", "outcome": "abstained",
                 "error": "token-secret_ABCDEF123 diagnosis: diabetes",
                 "prompt": "do not save", "chain_of_thought": "do not save",
                 "tool_calls": [{"name": "get_my_leave_balance", "status": "ok", "duration_ms": 1}],
                 "input_tokens": 2, "output_tokens": 3}
        memory = MemoryRepository()
        for backend in (repository, memory):
            backend.save_trace(trace)
            saved = backend.get_trace(trace["trace_id"])
            assert saved["employee_id"] == "EMP-001"
            assert saved["tool_calls"] == trace["tool_calls"]
            assert "ABCDEF123" not in saved["error"] and "diabetes" not in saved["error"]
            assert "prompt" not in saved and "chain_of_thought" not in saved
            assert saved["output_tokens"] == 3
            assert backend.get_trace("missing") is None
        for employee_id in ("EMP-001", "EMP-002", "missing"):
            expected = seeded_database.execute(
                "SELECT request_id FROM leave_requests WHERE employee_id = %s ORDER BY created_at DESC, request_id DESC",
                (employee_id,),
            ).fetchall()
            requests = repository.list_leave_requests(employee_id)
            assert [item.request_id for item in requests] == [row[0] for row in expected]
            memory.requests[employee_id] = requests
            assert memory.list_leave_requests(employee_id) == requests


def test_step6_database_deadline_cancels_wait_and_resets_connection(seeded_database):
    from src.execution import RunBudget, budget_scope
    from src.repository import PostgresRepository

    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        # Warm the pool so this test measures SQL cancellation, not connection setup.
        repository.get_balances("EMP-001")
        with budget_scope(RunBudget(0.3)), pytest.raises(psycopg.errors.QueryCanceled):
            with repository._connection() as connection:
                connection.execute("SELECT pg_sleep(2)")
        assert repository.get_balances("EMP-001")


def test_step6_agent_persists_safe_trace(seeded_database):
    from datetime import date
    from src.agent import AgentService
    from src.cache import NullCache
    from src.config import Settings
    from src.contracts import AuthContext, ChatRequest
    from src.providers import DeterministicProvider
    from src.repository import PostgresRepository
    from src.retrieval import RetrievalService
    from src.tools import ToolExecutor

    settings = Settings(_env_file=None, model_mode="fake", policy_index_version="test-v1")
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        provider = DeterministicProvider()
        retrieval = RetrievalService(repository, provider, NullCache(), 0, settings=settings)
        tools = ToolExecutor(repository, retrieval, settings, today_factory=lambda: date(2026, 9, 23))
        agent = AgentService(repository, provider, tools, settings)
        response = agent.run(ChatRequest(message="Berapa saldo cuti saya?"),
                             AuthContext(employee_id="EMP-001", display_name="Alya"))
        assert response.outcome == "answered"
        trace = repository.get_trace(response.trace_id)
        assert trace["outcome"] == "answered" and trace["employee_id"] == "EMP-001"
        assert trace["tool_calls"][0]["name"] == "get_my_leave_balance"
        assert trace["output_tokens"] == response.usage.output_tokens


def test_step7_postgres_auth_health_and_monitoring(seeded_database):
    from src.repository import MemoryRepository, PostgresRepository

    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        assert repository.health() is True
        assert repository.authenticate("token-alya").employee_id == "EMP-001"
        assert repository.authenticate("token-admin").role == "monitoring_admin"
        assert repository.authenticate("' OR 1=1 --") is None
        memory = MemoryRepository()
        assert repository.monitoring_summary() == memory.monitoring_summary()
        for index, latency in enumerate([1, 100, 300, 9000, 10000]):
            trace = {"trace_id": f"MON-{index}", "employee_id": "EMP-001", "outcome": "abstained",
                     "latency_ms": latency, "error": "failure" if index < 2 else None,
                     "tool_calls": [{"name": "search_hr_policy", "status": "ok", "duration_ms": 1}]}
            repository.save_trace(trace)
            memory.save_trace(trace)
        assert repository.monitoring_summary() == memory.monitoring_summary()


def test_step7_postgres_confirmation_is_atomic_and_idempotent(seeded_database):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date, datetime, timedelta, timezone
    from src.contracts import ActionProposal
    from src.repository import ConflictError, PostgresRepository

    proposal = ActionProposal(
        proposal_id="AP-CONCURRENT", leave_type="annual", start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        balances = repository.get_balances("EMP-001")
        repository.save_proposal("EMP-001", proposal)
        assert repository.get_proposal(proposal.proposal_id) == ("EMP-001", proposal)
        assert repository.get_proposal("missing") is None
        with pytest.raises(KeyError):
            repository.confirm_proposal("EMP-002", proposal.proposal_id, "same-key-123")
        def confirm(_):
            return repository.confirm_proposal("EMP-001", proposal.proposal_id, "same-key-123")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(confirm, range(8)))
        assert len({result.request_id for result in results}) == 1
        assert sum(not result.idempotent_replay for result in results) == 1
        assert all(result.status == "pending_manager_review" for result in results)
        assert len({result.audit_reference for result in results}) == 1
        with pytest.raises(ConflictError):
            repository.confirm_proposal("EMP-001", proposal.proposal_id, "another-key")
        other = proposal.model_copy(update={"proposal_id": "AP-OTHER"})
        repository.save_proposal("EMP-001", other)
        with pytest.raises(ConflictError):
            repository.confirm_proposal("EMP-001", other.proposal_id, "same-key-123")
        expired = proposal.model_copy(update={"proposal_id": "AP-EXPIRED",
                                              "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        repository.save_proposal("EMP-001", expired)
        with pytest.raises(ConflictError, match="expired"):
            repository.confirm_proposal("EMP-001", expired.proposal_id, "expired-key")
        assert repository.get_balances("EMP-001") == balances
        rows = seeded_database.execute("SELECT request_id, status FROM leave_requests WHERE source = 'application'").fetchall()
        assert rows == [(results[0].request_id, "pending_manager_review")]
        seeded_database.execute("UPDATE action_proposals SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE proposal_id = %s",
                                (proposal.proposal_id,))
        seeded_database.commit()
        assert confirm(0).request_id == results[0].request_id


def test_step7_failed_confirmation_rolls_back(seeded_database):
    from datetime import date, datetime, timedelta, timezone
    from src.contracts import ActionProposal
    from src.repository import PostgresRepository

    connection = seeded_database
    connection.execute("""CREATE FUNCTION fail_confirmation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'test failure'; END $$""")
    connection.execute("CREATE TRIGGER fail_confirmation BEFORE INSERT ON leave_requests FOR EACH ROW EXECUTE FUNCTION fail_confirmation()")
    connection.commit()
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        proposal = ActionProposal(
            proposal_id="AP-ROLLBACK", leave_type="annual", start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        repository.save_proposal("EMP-001", proposal)
        with pytest.raises(psycopg.errors.RaiseException):
            repository.confirm_proposal("EMP-001", proposal.proposal_id, "rollback-key")
        assert connection.execute("SELECT count(*) FROM leave_requests WHERE source = 'application'").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_confirmation ON leave_requests")
        connection.commit()
        result = repository.confirm_proposal("EMP-001", proposal.proposal_id, "rollback-key")
        assert result.idempotent_replay is False and result.status == "pending_manager_review"


def test_step7_api_with_postgres_runtime(seeded_database):
    from fastapi.testclient import TestClient
    from src.api import create_app
    from src.cache import NullCache
    from src.config import Settings
    from src.repository import PostgresRepository

    settings = Settings(_env_file=None, model_mode="fake", policy_index_version="test-v1")
    auth = {"Authorization": "Bearer token-alya"}
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        with TestClient(create_app(settings=settings, repository=repository, cache=NullCache())) as client:
            ready = client.get("/readyz")
            assert ready.status_code == 200 and ready.json()["status"] == "degraded"
            response = client.post("/api/v1/chat", headers=auth, json={"message": "Berapa saldo cuti saya?"})
            assert response.status_code == 200 and "8 hari" in response.json()["answer"]
            path = "/api/v1/traces/" + response.json()["trace_id"]
            assert client.get(path, headers=auth).status_code == 200
            assert client.get(path, headers={"Authorization": "Bearer token-bima"}).status_code == 403
            assert client.get(path, headers={"Authorization": "Bearer token-admin"}).status_code == 200
            assert client.get("/api/v1/leave-requests/me", headers=auth).json()[0]["request_id"] == "LR-HIST-001"
            summary = client.get("/api/v1/monitoring/summary", headers={"Authorization": "Bearer token-admin"})
            assert summary.json()["run_count"] == 1


def test_step8_postgres_prepare_confirm_checkpoint(seeded_database):
    from datetime import date
    from fastapi.testclient import TestClient
    from src.api import create_app
    from src.cache import NullCache
    from src.config import Settings
    from src.repository import PostgresRepository

    settings = Settings(_env_file=None, model_mode="fake", policy_index_version="test-v1")
    auth = {"Authorization": "Bearer token-alya"}
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        app = create_app(settings=settings, repository=repository, cache=NullCache())
        app.state.runtime.agent.tools.today_factory = lambda: date(2026, 9, 23)
        balances = repository.get_balances("EMP-001")
        before = repository.list_leave_requests("EMP-001")
        with TestClient(app) as client:
            response = client.post("/api/v1/chat", headers=auth, json={
                "message": "Siapkan cuti tahunan 2026-11-02 sampai 2026-11-03",
            })
            assert response.status_code == 200
            prepared = response.json()
            assert prepared["outcome"] == "action_proposed"
            assert prepared["action_proposal"]["requires_confirmation"] is True
            assert repository.list_leave_requests("EMP-001") == before
            assert repository.get_balances("EMP-001") == balances
            proposal_id = prepared["action_proposal"]["proposal_id"]
            path = f"/api/v1/action-proposals/{proposal_id}/confirm"
            key = {"idempotency_key": "checkpoint-key"}
            first = client.post(path, headers=auth, json=key)
            replay = client.post(path, headers=auth, json=key)
            assert first.status_code == replay.status_code == 200
            assert first.json()["request_id"] == replay.json()["request_id"]
            assert first.json()["audit_reference"] == replay.json()["audit_reference"]
            assert replay.json()["idempotent_replay"] is True
            rows = seeded_database.execute("""
                SELECT request_id, employee_id, status FROM leave_requests WHERE proposal_id = %s
            """, (proposal_id,)).fetchall()
            assert rows == [(first.json()["request_id"], "EMP-001", "pending_manager_review")]
            assert repository.get_balances("EMP-001") == balances


def test_step8_postgres_competing_proposals_and_owner_scoped_keys(seeded_database):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date, datetime, timedelta, timezone
    from threading import Barrier
    from src.contracts import ActionProposal
    from src.repository import ConflictError, PostgresRepository

    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        for identifier, owner in (("P1", "EMP-001"), ("P2", "EMP-001"), ("P3", "EMP-002")):
            repository.save_proposal(owner, ActionProposal(
                proposal_id=identifier, leave_type="annual", start_date=date(2026, 11, 2),
                end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            ))
        start = Barrier(2)

        def confirm(identifier):
            start.wait(timeout=5)
            try:
                return repository.confirm_proposal("EMP-001", identifier, "shared-key")
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, ("P1", "P2")))
        assert sum(result is not None for result in results) == 1
        other_owner = repository.confirm_proposal("EMP-002", "P3", "shared-key")
        winner = next(result for result in results if result is not None)
        assert other_owner.request_id != winner.request_id
        rows = seeded_database.execute("""
            SELECT employee_id, count(*) FROM leave_requests
            WHERE source = 'application' GROUP BY employee_id ORDER BY employee_id
        """).fetchall()
        assert rows == [("EMP-001", 1), ("EMP-002", 1)]


def test_step8_postgres_checks_expiry_after_waiting_for_lock(seeded_database):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date, datetime, timedelta, timezone
    import time
    from src.contracts import ActionProposal
    from src.repository import ConflictError, PostgresRepository

    connection = seeded_database
    with PostgresRepository(setup_lab.get_settings().database_url, 1536,
                            policy_index_version="test-v1") as repository:
        repository.save_proposal("EMP-001", ActionProposal(
            proposal_id="WAITING", leave_type="annual", start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 3), business_days=2, validation_summary=["valid"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        connection.execute("SELECT employee_id FROM employees WHERE employee_id = 'EMP-001' FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(repository.confirm_proposal, "EMP-001", "WAITING", "waiting-key")
            try:
                # Observe the real lock wait before expiring the proposal. This
                # distinguishes wall-clock expiry from transaction-start time.
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    connection.execute("SELECT pg_stat_clear_snapshot()")
                    blocked = connection.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM pg_stat_activity
                            WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid))
                        )
                    """).fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.01)
                assert blocked, "Confirmation did not reach the expected row lock"
                connection.execute("""
                    UPDATE action_proposals SET expires_at = clock_timestamp()
                    WHERE proposal_id = 'WAITING'
                """)
                connection.commit()
                with pytest.raises(ConflictError, match="expired"):
                    future.result(timeout=5)
            finally:
                connection.rollback()
        assert connection.execute("SELECT count(*) FROM leave_requests WHERE proposal_id = 'WAITING'").fetchone()[0] == 0
