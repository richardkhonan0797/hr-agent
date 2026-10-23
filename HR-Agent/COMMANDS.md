# HR Assistant command guide

Copy these commands into **Windows PowerShell**. Run them from `HR-Agent`, not the repository's parent folder. Docker is the recommended way to run the full app; local Python is an alternative for development.

```powershell
Set-Location 'D:\Richard\Documents\code\hacktiv8\hr-agent-milestone2\HR-Agent'
```

## 1. First-time setup

Open Docker Desktop and wait for its engine to start. Check both the client and server are available:

```powershell
docker version
docker compose version
```

Create the configuration only if it does not already exist:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Set `OPENAI_API_KEY`, `AGENT_MODEL`, `EMBEDDING_MODEL`, and matching `EMBEDDING_DIMENSIONS`. Set `OPENAI_BASE_URL` only if your provider requires it. Keep `POLICY_INDEX_VERSION` consistent with the indexed policies. Do not commit `.env` or put API keys into commands or screenshots.

Default host ports are PostgreSQL `5432`, Redis `6379`, API `8000`, and Streamlit `8501`. Change `POSTGRES_PORT`, `REDIS_PORT`, `API_PORT`, or `STREAMLIT_PORT` in `.env` if necessary. Update browser URLs accordingly. Container-to-container addresses stay unchanged.

All commands below use the Compose project selected by your current folder/configuration. If you previously used `docker compose -p <name>`, keep that same `-p <name>` on every command, including tests, logs, and shutdown. `docker compose ls` lists existing projects. Changing project names can select a different database volume.

## 2. Start the full app with Docker

Run each command in order and continue only after it succeeds:

```powershell
docker compose build api streamlit
docker compose up -d postgres redis
docker compose ps
docker compose run --rm api python -m src.setup_lab
docker compose up -d api streamlit
```

Wait for PostgreSQL and Redis to show healthy before indexing. `src.setup_lab` creates/seeds the schema without resetting existing records, then indexes policies. Indexing calls the embedding provider and requires valid credentials; it can incur API charges. It updates the active policy index, so it is not a read-only check.

Open these addresses with default ports:

| Purpose | URL |
| --- | --- |
| Streamlit app | http://localhost:8501 |
| Interactive API docs | http://localhost:8000/docs |
| API health | http://localhost:8000/healthz |
| Dependency/model readiness | http://localhost:8000/readyz |

```powershell
Invoke-RestMethod 'http://localhost:8000/healthz'
Invoke-RestMethod 'http://localhost:8000/readyz'
```

Health should report `ok`. Readiness should report `ready` once infrastructure, model configuration, and policy indexing are available. A running container alone does not prove the assistant is ready.

## 3. Everyday commands

### Start again after setup

```powershell
docker compose up -d
```

### Rebuild after changing code or dependencies

```powershell
docker compose up -d --build api streamlit
```

The source is copied into the image, not mounted from your checkout. A plain restart does not load edited Python files. If policy files changed, rebuild and rerun the indexing command from section 2. Follow the main README's index-version guidance.

### Check status and logs

```powershell
docker compose ps
docker compose logs --tail 100 api
docker compose logs --tail 100 postgres redis
docker compose logs -f --tail 100 api streamlit
```

Press `Ctrl+C` to stop following logs; the containers keep running.

### Stop without deleting the database

```powershell
docker compose stop
```

This preserves containers and the PostgreSQL volume. Use `docker compose up -d` to start again.

## 4. Run tests

Build the latest image first if code or tests changed:

```powershell
docker compose build api
```

### Quick non-live suite

No paid model calls and no running API required:

```powershell
docker compose run --rm --no-deps api python -m pytest -m 'not live' -q
```

PostgreSQL tests are skipped unless `TEST_DATABASE_URL` is supplied. A green result with database skips is not full integration verification.

### Non-live suite including PostgreSQL integration

Requires healthy PostgreSQL with pgvector. These tests create, seed, and remove temporary schemas in the specified database; they do not use the public application tables as test fixtures.

```powershell
docker compose up -d postgres redis
docker compose run --rm -e TEST_DATABASE_URL=postgresql://hr_app:hr_app@postgres:5432/hr_assistant api python -m pytest -m 'not live' -q
```

The connection above is inside Docker, so it uses `postgres:5432` even if the host port is different. The credentials shown are the repository's synthetic local database defaults.

### QA rows 11–16 and confirmation regressions

These cover Indonesian and English prompts, notice, balance, missing dates, medical-write protection, and confirmation behavior with memory fixtures:

```powershell
docker compose run --rm --no-deps api python -m pytest tests/test_qa_leave_safety.py tests/test_confirmation_boundary.py -q
```

Database confirmation regressions are included in the integration suite above.

### Opt-in live tests

Requires a running, seeded API, PostgreSQL, Redis, and valid model credentials. These tests call the model/embedding provider and write traces/cache entries; API charges may apply.

```powershell
docker compose exec -T -e RUN_LIVE_TESTS=1 -e API_URL=http://localhost:8000 api python -m pytest -m live -o 'addopts=' -q
```

Here `localhost:8000` is inside the API container, not the host's published port. These live tests exercise policy Q&A and retrieval, not leave confirmation.

## 5. Run the 15-case evaluation

Requires the same live environment as live tests. It calls chat and can persist proposals, tickets, and traces. The evaluator does not confirm proposals or retry chat automatically. Existing balances and overlapping leave can affect results.

```powershell
docker compose exec -T api python -m src.run_evaluation --base-url http://localhost:8000 --token token-alya
$evaluationExit = $LASTEXITCODE
New-Item -ItemType Directory -Force ./outputs | Out-Null
docker compose cp api:/app/outputs/evaluation_report.json ./outputs/evaluation_report.json
Write-Output "Evaluation exit code: $evaluationExit"
```

`token-alya` is a synthetic demo token. Exit `0` means GO, `1` means NO-GO, and `2` means setup/report failure. After a failed run, check the report timestamp so an older file is not mistaken for fresh evidence. For the public-demo rate limit, append `--interval-seconds 7` to the evaluator command. Preserve the October 19 report separately from new results.

## 6. Local Python development alternative

Use this instead of Docker's API/UI services. Do not run both stacks on the same host ports. Keep PostgreSQL and Redis in Docker.

### Prepare Python and infrastructure

With Python 3.11 or newer installed:

```powershell
if (-not (Test-Path .venv/Scripts/python.exe)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose stop api streamlit
docker compose up -d postgres redis
```

Use the `.env` from section 1. For host Python, `DATABASE_URL` must use `localhost` and the published PostgreSQL port; `REDIS_URL` must use `localhost` and the published Redis port. The defaults in `.env.example` already match default host ports. Compose overrides these addresses for containers.

On first setup or after policy changes, index using host Python:

```powershell
.\.venv\Scripts\python.exe -m src.setup_lab
```

### Terminal 1: API

```powershell
Set-Location 'D:\Richard\Documents\code\hacktiv8\hr-agent-milestone2\HR-Agent'
.\.venv\Scripts\python.exe -m uvicorn src.api:create_app --factory --reload --port 8000
```

### Terminal 2: Streamlit

```powershell
Set-Location 'D:\Richard\Documents\code\hacktiv8\hr-agent-milestone2\HR-Agent'
$env:API_URL = 'http://localhost:8000'
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8501
```

Use matching explicit ports if you change them; the host commands do not automatically read Compose's `API_PORT` or `STREAMLIT_PORT`. Press `Ctrl+C` in each terminal to stop its server. Direct `.venv` executable paths avoid PowerShell activation-policy issues.

### Terminal 3: local tests

From `HR-Agent`:

```powershell
.\.venv\Scripts\python.exe -m pytest -m 'not live' -q
```

To include PostgreSQL integration with default host ports:

```powershell
$env:TEST_DATABASE_URL = 'postgresql://hr_app:hr_app@localhost:5432/hr_assistant'
.\.venv\Scripts\python.exe -m pytest -m 'not live' -q
Remove-Item Env:TEST_DATABASE_URL
```

Adjust the host port if needed. These variables apply only to the current PowerShell process and its children.

### Record QA responses with synthetic memory storage

```powershell
.\.venv\Scripts\python.exe scripts/verify_qa_leave.py
```

This host-only script uses configured live credentials but isolated synthetic memory storage. The twelve bilingual QA cases resolve before model calls; a separate balance smoke test calls the live provider. It overwrites `submission-evidence/qa-rows-11-16/responses.json`, so preserve previous evidence before rerunning. Inspect `live_smoke_passed` as well as the QA result: successful script completion alone does not mean the live smoke passed. This is **not deployed PostgreSQL verification** and does not inspect historical tickets. The current Dockerfile does not include `scripts/`.

## 7. Troubleshooting before presenting

| Symptom | What to check |
| --- | --- |
| Docker command hangs or cannot connect | Open Docker Desktop and check the engine error. `docker version` must show a responding server before running Compose commands. |
| `/healthz` works but `/readyz` fails | Check PostgreSQL/Redis health, model settings, and policy indexing; read API logs. |
| Edited behavior is missing | Rebuild and recreate API/UI images using section 3. |
| Database tests are skipped | Set `TEST_DATABASE_URL` using the correct host or container address. |
| Port already in use | Stop the other stack or change host ports in `.env`, then recreate services and use matching URLs. |
| QA recording passes but live smoke fails | Read the separate smoke result and trace ID. Memory verification does not prove the deployed app works. |

**Last observed on October 22, 2026:** Docker Desktop failed during inference-manager socket initialization and local PostgreSQL timed out. Database integration and historical-ticket inspection were therefore blocked. This is a dated observation, not a new check of your current machine. Restore Docker health before relying on the startup or integration workflow; no database reset is required by this guide.

The demo confirmation script `scripts/verify-step11-demo.ps1` creates a real synthetic pending leave request. Use the [Docker delivery guide](docker-delivery.md) for that workflow, with future working dates that satisfy notice and do not overlap. It is not a read-only health check.

For more detail, see the [main README](README.md), [Docker delivery guide](docker-delivery.md), and [QA verification record](submission-evidence/qa-rows-11-16/README.md).
