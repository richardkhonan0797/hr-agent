# Evaluasi final Step 11

Sumber: `submission-evidence/step11/evaluation_report.json`, 19 Oktober 2026.

| Metric | Result | Target | Pass? |
|---|---:|---:|---|
| Overall pass rate | 15/15 (100%) | 80% | Ya |
| Hard gates | 100% | 100% | Ya |
| Retrieval recall@3 (document trace proxy) | 100% | 80% | Ya |
| Citation validity (case-level) | 100% | 100% | Ya |
| Expected tool selection | 100% | Report only | Ya |
| p50 / p95 latency | 4034 / 4945 ms | Report only | — |
| Input / output tokens | 20001 / 1436 | Report only | — |
| Estimated cost | null | Optional | Tidak dikonfigurasi |

## Failure analysis

Build awal menemukan dua test yang menganggap environment model kosong, padahal
Compose memasok konfigurasi live. Test kini memberi nilai kosong eksplisit pada
Settings yang memang menguji kondisi tanpa model. Assertion tidak dilonggarkan.
Live policy test awal memilih tool saldo untuk pertanyaan jatah tahunan; instruksi
agent kini membedakan hak/jatah kebijakan dari saldo/sisa aktual. Test live yang sama
lulus setelah perubahan. Log awal dan akhir disimpan terpisah.

## Release decision

GO untuk gate milestone pada run ini. Seluruh gate pada JSON bernilai true.
Tidak ada request baru dibuat selama evaluasi. Nilai recall adalah proxy document
trace, bukan ranking chunk independen; validitas sitasi tidak mengukur entailment.
Dataset 15 kasus bukan bukti kesiapan produksi atau uji beban.

## Regression evidence

Existing `test_live_policy_question` dipertahankan sebagai regression untuk pemilihan
tool kebijakan. Suite non-live 226 passed; live 2 passed. Script
`scripts/verify-step11-demo.ps1` menambah pemeriksaan integrasi demo terhadap ownership,
proposal tanpa request, konfirmasi pending, replay identik, fallback, dan monitoring.
Evaluasi dijalankan sebelum script tersebut agar request demo tidak memengaruhi hasil.
