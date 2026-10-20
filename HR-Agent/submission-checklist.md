# Submission Checklist

Status evidence 19 Oktober 2026; lihat `step11-verification.md`.

- [x] README dan perintah setup Docker telah diuji dari clean-source staging.
- [x] Dockerfile lengkap; image API/UI dibangun tanpa API key saat build dan berjalan non-root.
- [x] .dockerignore mencegah .env, venv, cache, staging, dan output lokal masuk image.
- [ ] Dari checkout Git bersih, grader dapat menjalankan urutan docker compose tanpa Python host. **Clean-source staging sudah lulus; source masih untracked, sehingga belum disebut checkout Git.**
- [x] Seed, deterministic tests dalam container, healthz, readyz ready, OpenAPI, dan UI berhasil.
- [ ] Diagram arsitektur final/ADR Step 1 lengkap. **Diagram ringkas demo tersedia; assignment final belum selesai.**
- [x] FastAPI terhubung ke Streamlit yang disediakan.
- [x] Policy indexing menghasilkan chunk versioned dan vector sesuai dimensi.
- [x] Pertanyaan kebijakan menampilkan sitasi valid.
- [x] Akses lintas pegawai diblokir.
- [x] Proposal tidak menulis leave request sebelum konfirmasi.
- [x] Konfirmasi ulang dengan key sama tidak membuat duplicate.
- [x] Semua request baru yang dibuat berstatus pending_manager_review; approval seed historis dikecualikan.
- [x] pytest non-live lulus: 226 passed termasuk integrasi database.
- [x] Kedua live test lulus.
- [x] Live evaluation report berisi 15 kasus dan GO.
- [x] Risk register memiliki owner, mitigation, evidence, dan residual risk; bukan sign-off manusia.
- [x] Screenshot UI dan monitoring serta output hasil test tersedia pada submission-evidence/step11.
- [ ] Demo 5–7 menit telah dilatih presenter. **Script 360 detik, walkthrough fungsional, dan backup sudah tersedia; rehearsal stopwatch belum dilakukan.**
