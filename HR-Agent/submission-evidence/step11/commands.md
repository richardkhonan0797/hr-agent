# Perintah verifikasi terisolasi

Tanggal: 19 Oktober 2026. PowerShell 7 pada host; semua Python dijalankan dalam Docker.
Source allowlist: src/, data/, tests/, Dockerfile, .dockerignore, requirements.txt,
pyproject.toml, app.py, ui_contract.py, demo_personas.json, docker-compose.yml.
Disalin ke `.step11-stage/`; `.env` tidak disalin. Dockerignore mengecualikan cache.

Override lokal `.step11-stage/ports.yml`:

```yaml
services:
  postgres:
    ports: !override ["15439:5432"]
  redis:
    ports: !override ["16389:6379"]
  api:
    ports: !override ["18089:8000"]
  streamlit:
    ports: !override ["18589:8501"]
```

```powershell
# Build dari staging tanpa .env/provider build arguments.
docker compose -p hr_step11_20261019 -f .step11-stage/docker-compose.yml -f .step11-stage/ports.yml build api streamlit
$c=@('--env-file','.env','-p','hr_step11_20261019','-f','.step11-stage/docker-compose.yml','-f','.step11-stage/ports.yml')
docker compose @c up -d postgres redis
docker compose @c run --rm api python -m src.setup_lab
# Ulangi untuk membuktikan indexing idempotent.
docker compose @c run --rm api python -m src.setup_lab
docker compose @c exec -T postgres psql -U hr_app -d hr_assistant -c 'SELECT count(*) AS policy_chunks, count(DISTINCT chunk_id) AS unique_chunks FROM policy_chunks;'
docker compose @c up -d api streamlit
docker compose @c run --rm -e TEST_DATABASE_URL=postgresql://hr_app:hr_app@postgres:5432/hr_assistant api python -m pytest -m 'not live' -q
docker compose @c exec -T -e RUN_LIVE_TESTS=1 -e API_URL=http://localhost:8000 api python -m pytest -m live -o 'addopts=' -q
docker compose @c exec -T api python -m src.run_evaluation --base-url http://localhost:8000 --token token-alya
docker compose @c cp api:/app/outputs/evaluation_report.json submission-evidence/step11/evaluation_report.json
./scripts/verify-step11-demo.ps1
# Hentikan hanya project ini bila demo selesai; volume dipertahankan.
# docker compose @c stop
```

Dua defect diperbaiki setelah run pertama; staging file terkait diperbarui, image
rebuilt, dan API/UI direcreate sebelum pengulangan kedua suite dan evaluasi.
Log `*-before.txt` dipertahankan. Build akhir memakai dependency layer cache Docker,
bukan instalasi Python host. Ini bukan klaim build tanpa network/cache.
