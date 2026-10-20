# Render + Neon implementation verification

Verified locally on 2026-10-20 using Docker Desktop and a dedicated disposable
PostgreSQL/pgvector database. Existing lab containers and database were untouched.

| Check | Result |
| --- | --- |
| Application image builds without provider credentials | Passed |
| Root Blueprint validates against Render's official JSON schema | Passed |
| Non-live suite with `TEST_DATABASE_URL` configured | 246 passed, 2 live tests deselected |
| Quota atomicity across concurrent independent connection pools | Passed |
| Jakarta daily rollover and rolling minute boundaries | Passed |
| Repeated migration and repository restart preserve counters | Passed |
| Failed agent execution retains admitted quota | Passed |
| Quota errors block agent execution and preserve `Retry-After` | Passed |
| UI readiness, timeout, non-JSON error and single-attempt POST behavior | Passed |
| Streamlit AppTest retains confirmation key after uncertain response | Passed |
| Real HTTP smoke: health/readiness, 10 accepted chats, 11th returns 429 | Passed, deterministic model |
| API/UI process restart retains 10 reservations and 15 policy chunks | Passed |
| Basic container memory under 512 MB limits | API 72.8 MiB; UI 50.13 MiB |

Memory readings cover basic local smoke tests, not live-model traffic or a browser
load test. The suite emits an existing Starlette/httpx deprecation warning.

Still pending: connected Render/Neon accounts, a hosted Git repository, Neon TLS
initialization with real embeddings, cloud deployment, live 15-case evaluation,
cloud UI confirmation flow, cloud cold-start/memory checks, and a Neon backup and
restore drill. No public URL or successful cloud deployment is claimed.

See [the deployment guide](render-neon.md) for commands, secret configuration,
evaluation pacing, backups, and rollback.
