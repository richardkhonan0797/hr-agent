# Evidence Step 11

Lihat [verifikasi lengkap](../../step11-verification.md) dan [perintah](commands.md).
Semua data sintetis. Jangan menambahkan `.env`, authorization header, atau raw provider
exception ke folder ini. Folder ini disertakan dalam submission, tetapi tidak dalam image.

- `build.txt`, `image-check.json`, `image-identity.txt`: packaging/non-root.
- `seed-repeat.txt`, `index-count.txt`: indexing idempotent.
- `non-live-tests.txt`, `live-tests.txt`: hasil final, 226 + 2 passed.
- `non-live-tests-before.txt`, `live-tests-before.txt`: kegagalan awal sebelum fix.
- `evaluation_report.json`, `evaluation-run.txt`: GO terbaru, 15/15.
- `evaluation-report-20261016.json`: snapshot report historis, bukan run Step 11.
- `endpoints.json`, `compose-status.json`: HTTP dan service readiness.
- `api-demo.json`: respons/trace sintetis; ownership, pending/replay, fallback, monitoring.
- `ui-requests-before.json`, `ui-requests-after.json`: hanya satu request UI baru.
- `ui-policy.png`, `ui-balance.png`, `ui-blocked.png`, `ui-proposal.png`,
  `ui-confirmed.png`, `ui-replay.png`, `ui-fallback.png`, `ui-monitoring.png`: screenshot aktual.
- `manifest.sha256`: checksum evidence dan source yang diverifikasi.

Referensi nama file singkat di risk register mengacu pada folder evidence ini;
referensi tests/ mengacu pada root HR-Agent. Token demo dalam source adalah sintetis;
API key runtime tidak dikumpulkan. Nilai cost null bukan nol.

Sesi UI/API ini adalah walkthrough fungsional, bukan bukti rehearsal presenter selama
5–7 menit. Gunakan alokasi waktu dan backup pada `templates/demo-script.md`.
