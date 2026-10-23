# Milestone P2M2 — Asisten Kebijakan HR dan Cuti

**Quick commands (English, PowerShell):** [Start the app, run tests, and evaluate](COMMANDS.md).

Cloud demo deployment: [Render + Neon guide](render-neon.md), using the repository-root
`render.yaml`. The local Docker Compose workflow below remains unchanged.

Anda membangun backend FastAPI untuk UI Streamlit yang sudah disediakan. Sistem harus menjawab kebijakan dengan sitasi, membaca saldo cuti milik pengguna terautentikasi, menyiapkan proposal cuti, dan membuat permintaan `pending_manager_review` hanya sesudah konfirmasi. Semua persona dan data adalah sintetis.

**Level:** Intermediate · **Waktu:** tiga sesi mentoring × 180 menit + demo 5–7 menit · **Hasil akhir:** API, test, evaluasi, risk register, dan catatan arsitektur.

Mulai dari [brief.md](brief.md), ikuti [lab.md](lab.md), lalu gunakan [assessment.md](assessment.md), [rubric.md](rubric.md), dan [submission-checklist.md](submission-checklist.md). Instruktur menyimpan kunci dan implementasi referensi di folder `solution/` terpisah.

## Yang Anda bangun

- `src/`: indexing dan hybrid retrieval, repository PostgreSQL, agent empat tool, aturan cuti, FastAPI, telemetry, serta evaluator. Cari marker `TODO(M2)` sebagai titik implementasi.
- `Dockerfile`: image non-root untuk API dan UI. `docker-compose.yml` menyediakan wiring empat service; lihat [kontrak pengiriman Docker](docker-delivery.md).
- Test tambahan, laporan evaluasi 15 kasus, diagram arsitektur, risk register, dan demo.

`app.py`, `ui_contract.py`, `demo_personas.json`, `data/`, dan model Pydantic pada `src/contracts.py` adalah artefak yang disediakan. UI hanya memanggil API. Identitas pegawai ditentukan backend dari bearer token, bukan dari teks pertanyaan atau pilihan model.

## Menjalankan starter

Dari folder `HR-Agent/`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
docker compose up -d postgres redis
python -m pytest -m "not live"
```

Isi `.env` dengan kredensial dan model yang ditetapkan instruktur. Di macOS/Linux, gunakan `source .venv/bin/activate` dan `cp .env.example .env`. Sebagian public tests memang gagal sampai TODO backend selesai. Setelah implementasi, jalankan `python -m src.setup_lab`, API dengan `python -m uvicorn src.api:create_app --factory --reload`, lalu UI dengan `python -m streamlit run app.py` pada terminal kedua.

## Policy indexing (Step 3)

`python -m src.setup_lab` membuat schema dan seed idempotent, kemudian mengindeks
`data/policies/*.md`. Untuk Docker, build ulang setelah perubahan dependency, lalu
jalankan `docker compose run --rm api python -m src.setup_lab` setelah PostgreSQL sehat.
Konfigurasi yang diperlukan: `DATABASE_URL`, `OPENAI_API_KEY`, `EMBEDDING_MODEL`,
`EMBEDDING_DIMENSIONS`, dan `POLICY_INDEX_VERSION`; `OPENAI_BASE_URL` bersifat opsional.
`AGENT_MODEL` tidak diperlukan untuk indexing.

Front matter wajib memuat `document_id`, `title`, `version`, dan `effective_date`
(`YYYY-MM-DD`). Satu heading `##` menghasilkan satu chunk; tujuh dokumen bawaan
menghasilkan 15 chunk. Section kosong/duplikat dan document ID duplikat ditolak.
ID chunk deterministik mengikuti document ID, versi, section, dan isi.

Embedding dikirim maksimal 32 teks per request, dengan maksimal dua retry SDK.
Dimensi database diperiksa sebelum API dipanggil; jumlah, dimensi, dan nilai vector
divalidasi sebelum penyimpanan. Upsert dan penghapusan chunk usang pada index version
yang sama dilakukan atomik. Versi lain tetap tersimpan. Kegagalan indexing tidak
mempublikasikan sebagian index; seed Step 2 yang sudah commit tetap tersedia.
Pemanggilan langsung `reset_and_seed()` hanya menjalankan Step 2 tanpa API model.

Naikkan `POLICY_INDEX_VERSION` saat mengubah kebijakan yang dipublikasikan atau model
embedding agar cache retrieval kelak tidak memakai hasil lama. Perubahan dimensi
memerlukan rebuild/migrasi kolom vector secara eksplisit; script tidak mengubahnya
otomatis. Model selain keluarga `text-embedding-3-*` memakai dimensi output default
provider, yang tetap harus cocok dengan `EMBEDDING_DIMENSIONS`.

Test Step 3 tidak memanggil API berbayar:

```powershell
python -m pytest tests/test_policy_indexing.py tests/test_setup_schema.py tests/test_agent_reliability.py::test_policy_chunk_ids_and_metadata_are_stable
```

Set `TEST_DATABASE_URL` ke PostgreSQL dengan pgvector untuk mengaktifkan test integrasi.
Test membuat dan membersihkan schema sementara; tanpa variabel ini test database
di-skip.

Alur host-Python di atas untuk pengembangan lokal. **Submission dinilai melalui Docker saja**, tanpa instalasi Python pada host grader; ikuti [urutan build, seed, startup, dan test](docker-delivery.md).

## Batas sistem

- Tidak ada tool atau endpoint yang menyetujui cuti. Konfirmasi membuat satu permintaan `pending_manager_review`.
- Saldo, request, dan trace pegawai lain tidak boleh diakses.
- Jawaban kebijakan harus menyertakan sitasi dari dokumen yang ditemukan pada run yang sama; bila bukti tidak cukup, abstain atau eskalasi.
- Jangan mengunggah `.env`, API key, database, atau data pegawai nyata.

Lihat [brief.md](brief.md) untuk kontrak endpoint dan kategori kasus, dan [lab.md](lab.md) untuk langkah, checkpoint, serta troubleshooting.


## Step 11 — deployment dan demo

Hasil terbaru: [verifikasi Step 11](step11-verification.md),
[evidence](submission-evidence/step11/README.md),
[demo enam menit](templates/demo-script.md), dan
[risk register](templates/risk-register.csv).
Jalankan urutan Docker pada [docker-delivery.md](docker-delivery.md); gunakan
`TEST_DATABASE_URL` agar test database tidak terlewat. Evaluasi harus dijalankan
sebelum konfirmasi demo. Marker `TODO(M2)` sengaja dipertahankan sebagai penanda tugas,
bukan status implementasi.
