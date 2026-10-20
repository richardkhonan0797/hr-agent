# Public demo: Render + Neon

This is the cloud deployment path; the original Docker Compose grading setup is
unchanged. Infrastructure uses free plans. Model calls and initial embeddings
still incur provider usage. This is a shared synthetic demo, not an employee system.

## Architecture and limits

Browser -> Render Streamlit (HTTPS) -> Render FastAPI (HTTPS) -> Neon PostgreSQL 16
with pgvector. FastAPI also uses Render Key Value over its internal network.

The root `render.yaml` provisions two free Docker web services and a free cache in
Singapore. Neon should use the closest available region. Both web services have
512 MB RAM and share Render's 750 free instance hours/month; two continuously
active services exceed that allowance. Services sleep when idle. Do not configure
external keep-alive polling. Neon has its own free storage/compute quotas; inspect
the current Free allowance in its dashboard. Do not enable automatic paid upgrades.

Use `/healthz` for Render health checks: `/readyz` queries the database and is only
for operator/user checks, so routine health checks do not keep Neon compute awake.
The UI has an explicit wake/readiness button (120 seconds). A degraded cache is
usable. Readiness checks configuration/schema, not a successful live model call.

The API admits at most 500 validated, authenticated chat attempts per Jakarta day
and 10 in any rolling 60-second interval, globally across all personas and callers.
Quota is committed in PostgreSQL before invoking the agent. Model failures still
consume admitted attempts. Rejected requests do not. `429` includes `Retry-After`;
quota-storage failure produces `503` without invoking the model. This is a request
cap, not a currency cap. Shared demo tokens are not private user authentication.

## 1. Prepare source and accounts

Connect Render to the Git repository containing the root `render.yaml` and
`HR-Agent/`. Commit the reviewed application files and deployment changes first;
never add `.env*` secrets, database dumps, or virtual environments. This workspace
may not yet have a Git remote; create/select your repository before linking Render.

Create a Neon **Free** project with PostgreSQL **16**. In its connection dialog,
disable connection pooling and copy the direct connection string, preserving its
TLS parameters (`sslmode=require` and any supplied `channel_binding=require`).
Use a dedicated database for this demo, not a database containing real records.

From `HR-Agent/` in PowerShell:

```powershell
Copy-Item .env.example .env.neon
docker build -t hr-demo:release .
```

Edit `.env.neon` locally. Set `DATABASE_URL` to the Neon connection string,
`OPENAI_API_KEY`, `AGENT_MODEL`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMENSIONS` to
your provider's matching values; optionally set `OPENAI_BASE_URL`. Retain
`POLICY_INDEX_VERSION=policies-2026-01`, `MODEL_MODE=live`, and set
`PUBLIC_DEMO=true`. Do not paste secrets into chat or screenshots. `.env.neon` is
excluded from Git and Docker build context. The existing four-connection API pool
is retained; schema setup uses a separate short-lived connection.

## 2. Initialize Neon locally, once

```powershell
docker run --rm --env-file .env.neon hr-demo:release python -m src.setup_lab
if ($LASTEXITCODE -ne 0) { throw 'Neon initialization failed' }
docker run --rm --env-file .env.neon hr-demo:release python -m src.migrate_demo
if ($LASTEXITCODE -ne 0) { throw 'Quota migration failed' }
```

`setup_lab` creates the schema and vector extension, seeds synthetic data, and
indexes policy embeddings. Never use `--reset` against a shared deployed database.
`migrate_demo` is additive and safe to repeat; it preserves existing counters.
Neither command runs during image build or service startup. Later quota-only
updates need only the migration, not policy reindexing.

## 3. Deploy the Blueprint

In Render choose **New > Blueprint**, select the repository, and use root
`render.yaml`. Verify all three services show **Free**, region Singapore, with no
disk or paid add-ons. Dockerfile/build-context paths are relative to `HR-Agent`.

Provide the API environment secrets requested by the Blueprint:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Neon's direct TLS URL |
| `OPENAI_API_KEY` | Provider key |
| `OPENAI_BASE_URL` | Custom provider URL, or blank for the default provider |
| `AGENT_MODEL` | Existing configured chat model |
| `EMBEDDING_MODEL` | Model used during indexing |
| `EMBEDDING_DIMENSIONS` | Same dimensions used during indexing |

The Blueprint supplies live mode, public-demo mode, 500/day, 10/minute, index
version, and the cache's private connection string. Do not share the API's secret
environment group with Streamlit. Cache external access is disabled.

`API_URL` on the UI must be the **actual public HTTPS API URL**, not the internal
Render hostname. During initial Blueprint creation, if the hostname is not yet
available, enter `https://not-configured.invalid` as a temporary value. After the
API deploys, copy its assigned URL into the UI's `API_URL` and redeploy the UI.
Do not share the UI until this is complete. Streamlit receives no database or
provider credentials. No CORS change is needed: the Streamlit Python server makes
API calls, not browser JavaScript.

Launchers are `python -m src.render_start api` and `python -m src.render_start ui`.
They expand Render's `PORT` at runtime and replace themselves with the server
process; the API uses one worker. API health path: `/healthz`; UI health path:
`/_stcore/health`. Automatic deployment is off for controlled milestone releases.

## 4. Validate and capture evidence

Set `$demoApi` to the assigned API URL and `$demoUi` to the UI URL in your terminal.
Open the UI and click **Bangunkan / periksa backend**. Then verify:

```powershell
Invoke-RestMethod "$demoApi/healthz"
Invoke-RestMethod "$demoApi/readyz"
```

Require `ready` with the cache available. Confirm a real policy answer cites the
right passage, and query a persona's balance. Prepare and explicitly confirm one
future leave request with no overlap; verify `pending_manager_review` and replay
the same confirmation key to confirm no duplicate. Dates in supplied evaluation
cases can expire; inspect them before evaluating a later milestone.

Run evaluation locally, with a seven-second gap **after** each case. It does not
retry failed chats. Other visitors share the same quota, so use a quiet window.
The report is saved outside Render's ephemeral filesystem:

```powershell
New-Item -ItemType Directory -Force outputs | Out-Null
docker run --rm --mount "type=bind,source=$((Get-Location).Path)/outputs,target=/app/outputs" hr-demo:release python -m src.run_evaluation --base-url $demoApi --token token-alya --interval-seconds 7
if ($LASTEXITCODE -ne 0) { throw 'Evaluation NO-GO; inspect outputs/evaluation_report.json' }
```

This verifies the existing retrieval/citation/safety checks, not semantic
entailment of every answer. Do not bypass quotas to turn a failed report green.

Inspect Render's metrics for both services during chat and UI interactions;
confirm memory stays below the free instance limit and no OOM restarts occur.
Redeploy both web services, wake them again, and verify existing records remain.
Confirm cold starts show a useful message, errors do not crash the UI, and a lost
confirmation response retains the same idempotency key. Never automatically retry
chat or confirmation POSTs. Export screenshots/report to local evidence storage.

## 5. Backups, rollback, and credentials

Take a PostgreSQL custom-format dump before each release and after the demo. The
following avoids putting the database URL into shell command text or process
arguments; the container reads it from the env file. SQL dumps contain shared demo
records and should stay outside Git. Keep at least one downloaded backup.

```powershell
New-Item -ItemType Directory -Force outputs | Out-Null
docker run --rm --env-file .env.neon --mount "type=bind,source=$((Get-Location).Path)/outputs,target=/backup" postgres:16 sh -c 'export PGDATABASE="$DATABASE_URL"; pg_dump --format=custom --file=/backup/demo.dump'
if ($LASTEXITCODE -ne 0) { throw 'Backup failed' }
```

For a restore drill create a **separate empty test database**, put its direct TLS
connection string in `.env.neon-restore` as `DATABASE_URL`, then run:

```powershell
docker run --rm --env-file .env.neon-restore --mount "type=bind,source=$((Get-Location).Path)/outputs,target=/backup,readonly" postgres:16 sh -c 'export PGDATABASE="$DATABASE_URL"; pg_restore --dbname="$PGDATABASE" --no-owner --no-acl /backup/demo.dump'
if ($LASTEXITCODE -ne 0) { throw 'Restore failed' }
```

Restore never targets the live demo database. Verify policy and request counts,
quota state, and retrieval in the restored database before deleting that test copy.
For application rollback use the previous successful Render deploy. Leave the
additive quota table in place. Never roll back to a version lacking quota guards
while keeping a public endpoint with an active model key.

Rotate provider/Neon credentials through their dashboards, update backend secrets
and local `.env.neon`, redeploy the API, verify readiness/live calls, then revoke
old credentials. Preserve quota counters. Avoid printing environment or connection
strings in logs. Disable the public demo by suspending services when finished;
remove resources only after downloading evidence and backups.

## Verification before release

Run `python -m pytest -m "not live"` in the built image with `TEST_DATABASE_URL`
pointing to a disposable pgvector database. It must not point at the live demo.
Quota integration tests create isolated schemas and exercise concurrent pools,
restart persistence, midnight rollover, repeated migration, and failed agent runs.
UI tests verify bounded readiness retries, safe handling of platform errors,
and single-attempt POSTs. Cloud validation still requires connected accounts.

References: [Render free limits](https://render.com/docs/free),
[Blueprint specification](https://render.com/docs/blueprint-spec),
[Neon pgvector](https://neon.com/docs/extensions/pgvector),
[Neon connection strings](https://neon.com/docs/connect/connect-from-any-app).
