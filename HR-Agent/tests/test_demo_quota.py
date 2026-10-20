from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from src.demo_quota import JAKARTA, SCHEMA, decide, reserve
from src.repository import PostgresRepository


def test_jakarta_midnight_rollover_preserves_minute_window():
    before = datetime(2026, 10, 20, 16, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=1)
    retry, count, _ = decide(before, before.astimezone(JAKARTA).date(), 500, [], 500, 10)
    assert (retry, count) == (1, 500)
    retry, count, _ = decide(after, before.astimezone(JAKARTA).date(), 500, [], 500, 10)
    assert (retry, count) == (0, 1)
    retry, _, _ = decide(after, before.astimezone(JAKARTA).date(), 500, [before] * 10, 500, 10)
    assert retry == 59


def test_rolling_minute_boundary_and_daily_limit():
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    assert decide(now, now.date(), 10, [now - timedelta(seconds=59)] * 10, 500, 10)[0] == 1
    assert decide(now, now.date(), 10, [now - timedelta(seconds=60)] * 10, 500, 10)[0] == 0
    assert decide(now, now.date(), 500, [], 500, 10)[0] == 5 * 3600


@pytest.mark.parametrize("result,status", [(42, 429), (RuntimeError("private SQL"), 503)])
def test_api_blocks_before_agent_and_preserves_retry(client, monkeypatch, result, status):
    app = client.app
    app.state.settings.public_demo = True
    def admission(*args):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(app.state.runtime.repository, "reserve_demo_chat", admission, raising=False)
    monkeypatch.setattr(app.state.runtime.agent, "run", lambda *args: pytest.fail("Model ran"))
    response = client.post("/api/v1/chat", headers={"Authorization": "Bearer token-alya"},
                           json={"message": "hello"})
    assert response.status_code == status
    assert "private SQL" not in response.text
    if status == 429:
        assert response.headers["Retry-After"] == "42"


def test_local_mode_does_not_reserve_quota(client, monkeypatch):
    monkeypatch.setattr(client.app.state.runtime.repository, "reserve_demo_chat",
                        lambda *args: pytest.fail("Quota called locally"), raising=False)
    assert client.post("/api/v1/chat", headers={"Authorization": "Bearer token-alya"},
                       json={"message": "hello"}).status_code == 200


@pytest.fixture
def quota_database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for quota concurrency/persistence tests")
    schema = "test_quota_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped = make_conninfo(url, options=f"-c search_path={schema},public")
            with psycopg.connect(scoped) as connection:
                connection.execute(SCHEMA)
            yield scoped
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_atomic_concurrency_and_repository_restart(quota_database):
    # Two independent pools simulate separate workers; neither can exceed the cap.
    with PostgresRepository(quota_database, 1536) as first, PostgresRepository(quota_database, 1536) as second:
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(
                lambda n: (first if n % 2 else second).reserve_demo_chat(500, 10), range(30)))
        assert results.count(0) == 10
        assert all(result > 0 for result in results if result != 0)
    with PostgresRepository(quota_database, 1536) as restarted:
        assert restarted.reserve_demo_chat(500, 10) > 0
    with psycopg.connect(quota_database, row_factory=dict_row) as connection:
        connection.execute(SCHEMA)  # migration replay must not reset the counter
        row = connection.execute("SELECT * FROM demo_chat_quota").fetchone()
        assert row["attempts"] == 10
        assert len(row["recent"]) == 10


def test_database_daily_limit_and_rollover(quota_database):
    with psycopg.connect(quota_database, row_factory=dict_row) as connection:
        connection.execute("UPDATE demo_chat_quota SET day = "
                           "(clock_timestamp() AT TIME ZONE 'Asia/Jakarta')::date, attempts = 500")
        connection.commit()
        assert reserve(connection, 500, 10) > 0
        connection.execute("UPDATE demo_chat_quota SET day = day - 1")
        connection.commit()
        assert reserve(connection, 500, 10) == 0
        assert connection.execute("SELECT attempts FROM demo_chat_quota").fetchone()["attempts"] == 1


def test_failed_agent_attempt_stays_charged(client, monkeypatch, quota_database):
    with PostgresRepository(quota_database, 1536) as quota_repo:
        client.app.state.settings.public_demo = True
        monkeypatch.setattr(client.app.state.runtime.repository, "reserve_demo_chat",
                            quota_repo.reserve_demo_chat, raising=False)
        def fail(*args):
            raise RuntimeError("provider failure")
        monkeypatch.setattr(client.app.state.runtime.agent, "run", fail)
        with pytest.raises(RuntimeError):
            client.post("/api/v1/chat", headers={"Authorization": "Bearer token-alya"},
                        json={"message": "hello"})
        with psycopg.connect(quota_database) as connection:
            assert connection.execute("SELECT attempts FROM demo_chat_quota").fetchone()[0] == 1
