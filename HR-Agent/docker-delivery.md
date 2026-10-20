# Kontrak Pengiriman Docker

Submission harus dapat dijalankan dari checkout bersih dengan Docker Engine dan Docker Compose saja; grader tidak menginstal dependency Python pada host. Anda melengkapi `Dockerfile`. `docker-compose.yml` disediakan sebagai kontrak PostgreSQL/pgvector, Redis, API, dan Streamlit. Jangan mengubah nama service, port internal, atau `API_URL=http://api:8000`.

Image yang sama dipakai oleh service `api` dan `streamlit`. Karena itu image harus berisi backend, `app.py`, `ui_contract.py`, `demo_personas.json`, data sintetis, dan tests. `docker compose run --rm api python -m src.setup_lab` serta `docker compose run --rm api python -m pytest` harus dapat mengganti perintah default image. Jalankan proses aplikasi sebagai user non-root. Build tidak boleh memerlukan API key atau koneksi ke provider model.

## Prasyarat grader

- Docker Engine dan Compose; internet untuk menarik image/dependency serta untuk seed embedding dan evaluasi live.
- `.env` lokal dibuat dari `.env.example`. Grader mengisi `OPENAI_API_KEY`, `AGENT_MODEL`, `EMBEDDING_MODEL`, dan `EMBEDDING_DIMENSIONS` yang cocok. File ini tidak dikumpulkan atau disalin ke image.
- Gunakan nama project Compose unik per submission. Contoh `p2m2_demo` di bawah harus diganti ketika beberapa submission dinilai pada mesin yang sama.
- Jika port host terpakai, ubah `POSTGRES_PORT`, `REDIS_PORT`, `API_PORT`, atau `STREAMLIT_PORT` di `.env`. Port dan hostname antar-container tidak berubah.

## Urutan dari checkout bersih

Jalankan dari folder `HR-Agent/`. Buat `.env` secara lokal (`Copy-Item .env.example .env` pada PowerShell atau `cp .env.example .env` pada macOS/Linux), lalu isi kredensial dari instruktur. Tidak perlu venv atau `pip install` pada host.

```bash
docker compose -p p2m2_demo build api streamlit
docker compose -p p2m2_demo up -d postgres redis
docker compose -p p2m2_demo run --rm api python -m src.setup_lab
docker compose -p p2m2_demo up -d api streamlit
docker compose -p p2m2_demo run --rm api python -m pytest -m "not live"
```

Seed tanpa `--reset` sengaja dipakai agar tidak menghapus data. Project name unik memberi volume PostgreSQL tersendiri untuk tiap submission. Jalankan `docker compose -p p2m2_demo ps` dan `docker compose -p p2m2_demo logs api` untuk diagnosis bila startup gagal.

## Pemeriksaan penerimaan

Dengan port default, buka `http://localhost:8000/healthz`, `http://localhost:8000/readyz`, `http://localhost:8000/openapi.json`, dan `http://localhost:8501`. `/healthz` harus mengembalikan `status: ok`; `/readyz` harus mengembalikan `status: ready` setelah seed dan konfigurasi model benar. Di Streamlit, pilih persona Alya, tanyakan “Berapa jatah cuti tahunan?”, dan pastikan jawaban memuat sitasi kebijakan serta trace. Uji pula saldo pribadi dan konfirmasi yang menghasilkan `pending_manager_review`.

Build image dan pemeriksaan endpoint dasar adalah kewajiban terpisah dari live evaluation. Bila grader belum memberikan kredensial, image tetap harus dapat dibangun, tetapi seed embedding, status `ready`, dan alur model live belum dapat dinilai. Untuk pemeriksaan HTTP dari PowerShell, gunakan `Invoke-RestMethod http://localhost:8000/healthz`; pada macOS/Linux gunakan `curl -fsS http://localhost:8000/healthz`.

Setelah API aktif, live test dan evaluator juga dapat dijalankan tanpa Python host:

```bash
docker compose -p p2m2_demo exec -T -e RUN_LIVE_TESTS=1 -e API_URL=http://localhost:8000 api python -m pytest -m live -o addopts=""
docker compose -p p2m2_demo exec -T api python -m src.run_evaluation --base-url http://localhost:8000 --token token-alya
docker compose -p p2m2_demo cp api:/app/outputs ./outputs
```

Perintah terakhir menyalin report ke folder `outputs/` lokal yang diabaikan Git. Jika Anda mengubah lokasi output evaluator, dokumentasikan perintah ekspor yang setara agar grader dapat mengumpulkan buktinya.

> **Security Note:** `.dockerignore` harus mencegah `.env`, cache, virtual environment, dan laporan lokal masuk ke build context. Jangan memakai build argument untuk mengirim API key. Compose hanya meneruskan kredensial saat container berjalan.


## Evidence Step 11 (PowerShell 7; tanpa Python host)

Jalankan dari `HR-Agent/`. Contoh berikut memakai file Compose utama dan port default;
untuk isolasi gunakan nama project unik dan port host yang tersedia di `.env`.
Jangan mengubah port internal atau `API_URL` antar-service.

```powershell
$project = 'p2m2_demo_anda'
$evidence = 'submission-evidence/step11'
New-Item -ItemType Directory -Force $evidence | Out-Null
docker compose -p $project build api streamlit 2>&1 | Tee-Object "$evidence/build.txt"
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
docker compose -p $project up -d postgres redis
docker compose -p $project run --rm api python -m src.setup_lab
docker compose -p $project run --rm api python -m src.setup_lab
docker compose -p $project exec -T postgres psql -U hr_app -d hr_assistant -c 'SELECT count(*) FROM policy_chunks;'
docker compose -p $project up -d api streamlit
docker compose -p $project run --rm -e TEST_DATABASE_URL=postgresql://hr_app:hr_app@postgres:5432/hr_assistant api python -m pytest -m 'not live' -q 2>&1 | Tee-Object "$evidence/non-live-tests.txt"
if ($LASTEXITCODE -ne 0) { throw 'Non-live tests failed' }
docker compose -p $project exec -T -e RUN_LIVE_TESTS=1 -e API_URL=http://localhost:8000 api python -m pytest -m live -o 'addopts=' -q 2>&1 | Tee-Object "$evidence/live-tests.txt"
if ($LASTEXITCODE -ne 0) { throw 'Live tests failed' }
docker compose -p $project exec -T api python -m src.run_evaluation --base-url http://localhost:8000 --token token-alya
$evaluationExit = $LASTEXITCODE
docker compose -p $project cp api:/app/outputs/evaluation_report.json "$evidence/evaluation_report.json"
if ($evaluationExit -ne 0) { throw 'Evaluation NO-GO or failed; inspect exported report' }
```

`TEST_DATABASE_URL` wajib untuk bukti integrasi PostgreSQL: tanpa variabel ini,
sebagian test di-skip. Kredensial demo pada command adalah data sintetis bawaan;
API key provider hanya dimasukkan lewat `.env` runtime, tidak lewat build argument.
Tidak perlu menyalin `.env` ke image atau evidence. Jangan simpan output `compose config`
atau `docker inspect` environment container karena dapat memuat API key.

Setelah evaluasi, gunakan PowerShell 7 untuk pemeriksaan demo API:

```powershell
./scripts/verify-step11-demo.ps1 -BaseUrl http://localhost:8000 -StartDate 2026-11-09 -EndDate 2026-11-10
```

Script membuat **satu request sintetis pending** dan memverifikasi replay. Untuk run
berikutnya gunakan tanggal masa depan yang tidak overlap atau project baru; jangan
menjalankan ulang membabi buta setelah error jaringan. UI diverifikasi terpisah dengan
`templates/demo-script.md`. Simpan screenshot UI/monitoring ke direktori evidence.

Build verifikasi 19 Oktober memakai clean staging copy (bukan Git checkout), project
`hr_step11_20261019`, port host 15439/16389/18089/18589. Source tidak dipasang sebagai
bind mount; test dan aplikasi dijalankan dari image. `step11-verification.md` mencatat
hasil dan `submission-evidence/step11/commands.md` mencatat perintah isolasinya.
Untuk menghentikan deployment tanpa menghapus evidence database, gunakan `docker compose`
dengan project/file yang sama lalu `stop`; jangan memakai `down -v` pada environment lain.
