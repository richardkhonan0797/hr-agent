from __future__ import annotations

from pathlib import Path
from datetime import date
from typing import Any
import hashlib
import re
import psycopg
from psycopg import sql
import json
import yaml

from .config import Settings, get_settings
from .security import token_hash
from .providers import OpenAIProvider, ModelProvider, validate_embeddings


ROOT = Path(__file__).resolve().parents[1]


def chunk_policy(path: Path) -> list[dict[str, str]]:
    # Normalize line endings before parsing so file platform does not change chunk IDs.
    text = path.read_text(encoding="utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---\n(.*?)\n---(?:\n|$)(.*)\Z", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.name}: missing front matter")
    metadata = _parse_policy_metadata(path, match[1])
    return _chunk_policy_sections(path, metadata, match[2])


def _parse_policy_metadata(path: Path, front_matter: str) -> dict[str, str]:
    try:
        # BaseLoader constructs only strings/lists/maps, never Python objects or dates.
        metadata = yaml.load(front_matter, Loader=yaml.BaseLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"{path.name}: invalid front matter") from error
    required = ("document_id", "title", "version", "effective_date")
    if not isinstance(metadata, dict):
        raise ValueError(f"{path.name}: missing or invalid metadata")
    for key in required:
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"{path.name}: missing or invalid metadata")
    metadata = {key: metadata[key].strip() for key in required}
    # Check both the written format and calendar validity without converting the
    # stored metadata to a date object; callers expect strings here.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["effective_date"]):
        raise ValueError(f"{path.name}: effective_date must be YYYY-MM-DD")
    date.fromisoformat(metadata["effective_date"])
    return metadata


def _chunk_policy_sections(
    path: Path, metadata: dict[str, str], body: str
) -> list[dict[str, str]]:
    headings = list(re.finditer(r"^##[ \t]+([^\n]*)$", body, re.MULTILINE))
    if not headings or body[:headings[0].start()].strip():
        raise ValueError(f"{path.name}: expected content under ## sections")
    chunks: list[dict[str, str]] = []
    seen_sections: set[str] = set()
    # Each heading owns the text up to the next heading. Reject empty or repeated
    # sections before building identifiers so every chunk has a meaningful source.
    for position, heading in enumerate(headings):
        section = heading[1].strip()
        if position + 1 < len(headings):
            end = headings[position + 1].start()
        else:
            end = len(body)
        content = body[heading.end():end].strip()
        if not section or section in seen_sections or not content:
            raise ValueError(f"{path.name}: empty or duplicate section")
        seen_sections.add(section)
        chunk_identifier = _policy_chunk_identifier(metadata, section, content)
        chunk = dict(metadata, section=section, content=content, chunk_id=chunk_identifier)
        chunks.append(chunk)
    return chunks


def _policy_chunk_identifier(metadata: dict[str, str], section: str, content: str) -> str:
    # Preserve field order and compact JSON encoding: both are part of the stable ID.
    identity = json.dumps(
        [metadata["document_id"], metadata["version"], section, content],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def index_policies(*, provider: ModelProvider | None = None,
                   policy_dir: Path | None = None) -> int:
    """Build a complete index snapshot; publish only after all vectors validate."""
    settings = get_settings()
    chunks = _load_policy_chunks(policy_dir)
    if not settings.policy_index_version.strip():
        raise ValueError("POLICY_INDEX_VERSION is required")

    _validate_database_dimensions(settings)
    vectors = _embed_policy_chunks(chunks, settings, provider)
    _publish_policy_chunks(chunks, vectors, settings)
    return len(chunks)


def _load_policy_chunks(policy_dir: Path | None) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    document_ids: set[str] = set()
    for path in sorted((policy_dir or ROOT / "data" / "policies").glob("*.md")):
        parsed = chunk_policy(path)
        document_id = parsed[0]["document_id"]
        if document_id in document_ids:
            raise ValueError(f"Duplicate document ID: {document_id}")
        document_ids.add(document_id)
        chunks.extend(parsed)
    if not chunks:
        raise ValueError("No policies found; refusing to clear the index")
    return chunks


def _validate_database_dimensions(settings: Settings) -> None:
    # Fail before paying for embeddings if the existing column cannot store them.
    with psycopg.connect(settings.database_url) as connection:
        row = connection.execute("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
            WHERE attrelid = to_regclass('policy_chunks')
              AND attname = 'embedding' AND NOT attisdropped
        """).fetchone()
        if not row or row[0] != f"vector({settings.embedding_dimensions})":
            raise ValueError("Database embedding dimension mismatch; rebuild the schema explicitly")


def _embed_policy_chunks(
    chunks: list[dict[str, str]], settings: Settings, provider: ModelProvider | None
) -> list[list[float]]:
    # Only close clients created here; an injected provider belongs to the caller.
    owned_provider = provider is None
    if provider is None:
        provider = OpenAIProvider(settings=settings)
    try:
        embedding_texts = [
            f"{chunk['title']}\n{chunk['section']}\n{chunk['content']}" for chunk in chunks
        ]
        vectors = provider.embed(embedding_texts)
        validate_embeddings(vectors, len(chunks), settings.embedding_dimensions)
    finally:
        if owned_provider:
            provider.client.close()
    return vectors


def _publish_policy_chunks(
    chunks: list[dict[str, str]], vectors: list[list[float]], settings: Settings
) -> None:
    with psycopg.connect(settings.database_url) as connection:
        # Serialize publishers; readers continue seeing the previous committed snapshot.
        connection.execute("LOCK TABLE policy_chunks IN SHARE ROW EXCLUSIVE MODE")
        with connection.cursor() as cursor:
            # Build all rows before writing. Strict pairing prevents silently
            # dropping a chunk or vector if their counts ever diverge.
            rows = _policy_index_rows(chunks, vectors, settings.policy_index_version)
            cursor.executemany("""
                INSERT INTO policy_chunks (chunk_id, document_id, title, section, version,
                    effective_date, content, embedding, index_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (index_version, chunk_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id, title = EXCLUDED.title,
                    section = EXCLUDED.section, version = EXCLUDED.version,
                    effective_date = EXCLUDED.effective_date, content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding
            """, rows)
            cursor.execute("""
                DELETE FROM policy_chunks WHERE index_version = %s
                AND NOT (chunk_id = ANY(%s))
            """, (settings.policy_index_version, [chunk["chunk_id"] for chunk in chunks]))


def _policy_index_rows(
    chunks: list[dict[str, str]], vectors: list[list[float]], index_version: str
) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        row = (
            chunk["chunk_id"], chunk["document_id"], chunk["title"], chunk["section"],
            chunk["version"], chunk["effective_date"], chunk["content"],
            json.dumps(vector, allow_nan=False), index_version,
        )
        rows.append(row)
    return rows


def reset_and_seed() -> None:
    """Create missing tables and seed demo data without overwriting existing data."""
    settings = get_settings()
    
    employees = json.loads(
        (ROOT / "data" / "employees.json").read_text(encoding="utf-8")
    )
    historical_requests = json.loads(
        (ROOT / "data" / "historical_requests.json").read_text(encoding="utf-8")
    )

    # Keep schema creation and both seed phases inside one transaction: a failure
    # rolls everything back, and ON CONFLICT preserves previously seeded records.
    with psycopg.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            _create_schema(cursor, settings)
            _seed_employees_and_balances(cursor, employees)
            _seed_historical_requests(cursor, historical_requests)

    print("Schema dan seed pegawai, saldo, serta request historis selesai.")


def _create_schema(cursor: psycopg.Cursor[Any], settings: Settings) -> None:
    # Parent tables must exist before foreign keys are declared. All helpers use
    # the caller's cursor so schema creation remains in the seed transaction.
    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
    _create_employee_tables(cursor)
    _create_action_tables(cursor)
    _create_policy_table(cursor, settings)
    _create_trace_table_and_lookup_indexes(cursor)


def _create_employee_tables(cursor: psycopg.Cursor[Any]) -> None:
    cursor.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL
                        CHECK (role IN ('employee', 'monitoring_admin')),
                    token_hash TEXT NOT NULL UNIQUE
                )
            """)

    cursor.execute("""
                CREATE TABLE IF NOT EXISTS leave_balances (
                    employee_id TEXT NOT NULL
                        REFERENCES employees(employee_id),
                    leave_type TEXT NOT NULL
                        CHECK (leave_type IN ('annual', 'sick', 'parental')),
                    available_days INTEGER NOT NULL
                        CHECK (available_days >= 0),
                    as_of_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    PRIMARY KEY (employee_id, leave_type)
                )
            """)


def _create_action_tables(cursor: psycopg.Cursor[Any]) -> None:
    # Application requests require an owner-matched proposal and pending status;
    # historical records retain their separate constraints.
    cursor.execute("""
                CREATE TABLE IF NOT EXISTS action_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
                    action_type TEXT NOT NULL DEFAULT 'leave_request'
                        CHECK (action_type = 'leave_request'),
                    leave_type TEXT NOT NULL
                        CHECK (leave_type IN ('annual', 'sick', 'parental')),
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL CHECK (end_date >= start_date),
                    business_days INTEGER NOT NULL CHECK (business_days > 0),
                    reason TEXT,
                    validation_summary JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(validation_summary) = 'array'),
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (proposal_id, employee_id)
                )
            """)

    # Historical records have no proposal/key. Application-created
    # requests must refer to a proposal owned by the same employee.
    cursor.execute("""
                CREATE TABLE IF NOT EXISTS leave_requests (
                    request_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
                    proposal_id TEXT UNIQUE,
                    leave_type TEXT NOT NULL
                        CHECK (leave_type IN ('annual', 'sick', 'parental')),
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL CHECK (end_date >= start_date),
                    business_days INTEGER NOT NULL CHECK (business_days > 0),
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending_manager_review'
                        CHECK (status IN (
                            'pending_manager_review', 'approved', 'rejected', 'cancelled'
                        )),
                    source TEXT NOT NULL DEFAULT 'application'
                        CHECK (source IN ('application', 'historical')),
                    idempotency_key TEXT,
                    audit_reference TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (proposal_id, employee_id)
                        REFERENCES action_proposals(proposal_id, employee_id),
                    UNIQUE (employee_id, idempotency_key),
                    CHECK (
                        (source = 'historical' AND proposal_id IS NULL
                            AND idempotency_key IS NULL)
                        OR
                        (source = 'application' AND proposal_id IS NOT NULL
                            AND idempotency_key IS NOT NULL
                            AND length(idempotency_key) BETWEEN 8 AND 100
                            AND audit_reference IS NOT NULL
                            AND status = 'pending_manager_review')
                    )
                )
            """)

    cursor.execute("""
                CREATE TABLE IF NOT EXISTS hr_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
                    category TEXT NOT NULL CHECK (category IN (
                        'policy_question', 'leave_exception', 'technical_issue', 'privacy'
                    )),
                    summary TEXT NOT NULL CHECK (length(summary) BETWEEN 5 AND 500),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)


def _create_policy_table(cursor: psycopg.Cursor[Any], settings: Settings) -> None:
    # The vector size comes from validated settings; text search uses the stored index.
    cursor.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS policy_chunks (
                    chunk_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL,
                    version TEXT NOT NULL,
                    effective_date DATE NOT NULL,
                    content TEXT NOT NULL,
                    embedding vector({dimensions}) NOT NULL,
                    index_version TEXT NOT NULL,
                    search_vector TSVECTOR GENERATED ALWAYS AS (
                        to_tsvector('simple', title || ' ' || section || ' ' || content)
                    ) STORED,
                    PRIMARY KEY (index_version, chunk_id)
                )
            """).format(dimensions=sql.Literal(settings.embedding_dimensions)))
    cursor.execute("""
                CREATE INDEX IF NOT EXISTS policy_chunks_search_idx
                ON policy_chunks USING GIN (search_vector)
            """)


def _create_trace_table_and_lookup_indexes(cursor: psycopg.Cursor[Any]) -> None:
    # Traces contain redacted metadata. Lookup indexes support employee-scoped reads.
    cursor.execute("""
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
                    outcome TEXT NOT NULL CHECK (outcome IN (
                        'answered', 'action_proposed', 'escalated', 'abstained'
                    )),
                    retrieved_document_ids JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(retrieved_document_ids) = 'array'),
                    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(tool_calls) = 'array'),
                    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
                    estimated_cost DOUBLE PRECISION CHECK (estimated_cost >= 0),
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
    cursor.execute("""
                CREATE INDEX IF NOT EXISTS leave_requests_employee_idx
                ON leave_requests (employee_id, start_date, end_date)
            """)
    cursor.execute("""
                CREATE INDEX IF NOT EXISTS traces_employee_created_idx
                ON traces (employee_id, created_at)
            """)


def _seed_employees_and_balances(
    cursor: psycopg.Cursor[Any], employees: list[dict[str, Any]]
) -> None:
    # Hash demo tokens before persistence and seed each employee before their balances.
    for employee in employees:
        cursor.execute(
            """
                    INSERT INTO employees (
                        employee_id, display_name, role, token_hash
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (employee_id) DO NOTHING
                    """,
            (
                employee["employee_id"],
                employee["display_name"],
                employee["role"],
                token_hash(employee["token"]),
            ),
        )

        for leave_type, days in employee["balances"].items():
            cursor.execute(
                """
                        INSERT INTO leave_balances (
                            employee_id, leave_type, available_days
                        )
                        VALUES (%s, %s, %s)
                        ON CONFLICT (employee_id, leave_type) DO NOTHING
                        """,
                (employee["employee_id"], leave_type, days),
            )


def _seed_historical_requests(
    cursor: psycopg.Cursor[Any], historical_requests: list[dict[str, Any]]
) -> None:
    # Historical records retain their original statuses and have no action proposal.
    for request in historical_requests:
        cursor.execute(
            """
                    INSERT INTO leave_requests (
                        request_id, employee_id, leave_type, start_date,
                        end_date, business_days, status, created_at, source
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'historical')
                    ON CONFLICT (request_id) DO NOTHING
                    """,
            (
                request["request_id"], request["employee_id"],
                request["leave_type"], request["start_date"],
                request["end_date"], request["business_days"],
                request["status"], request["created_at"],
            ),
        )


if __name__ == "__main__":
    reset_and_seed()
    print(f"Policy indexing selesai: {index_policies()} chunks.")

