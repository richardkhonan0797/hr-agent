# QA rows 11–16 — October 22, 2026

This is a separate verification from the October 19 evaluation. It does not replace that report or establish production readiness.

## Verified

- Non-live suite: **282 passed, 31 skipped, 2 live tests deselected**. Command: `.venv/Scripts/python.exe -m pytest -m "not live" -q`. The Windows sandbox prevented asyncio loopback sockets; the suite completed outside that sandbox. One dependency deprecation warning remains.
- All six workbook prompts and their English translations passed through the service and HTTP regression tests. Replies remain Indonesian. Approval is refused; tomorrow violates annual notice; weekend-only intent has zero working days; Bima's authenticated balance of 3 cannot cover 5; annual leave without dates asks for dates; recognized medical disclosure creates no ticket.
- `responses.json` records 12 actual responses, trace IDs, provider-call counts, and storage assertions from the current source with a live provider configured and **isolated synthetic memory storage**. All twelve short-circuit before model execution. No proposal, ticket, or leave request was stored. The duration-only request calls the existing authenticated balance tool.
- Tests cover typed domain errors, notice boundaries, weekend crossings, Jakarta midnight, sick/parental exemptions, malicious model-generated medical writes, and memory confirmation revalidation/replay.
- PostgreSQL write-boundary unit tests verify rejection before connection. These are not database integration tests.

## Incomplete or unsuccessful checks

- **31 PostgreSQL integration tests skipped.** Docker Desktop failed during service initialization of its inference-manager socket; Docker commands did not return a usable engine. The configured local PostgreSQL connection (`localhost:5432`) timed out. Six new database tests are included for changed balance, overlap, notice, past dates, legacy medical reasons, and successful replay.
- **Historical ticket inspection blocked:** ticket `HR-e895aeab94a34f96990fa346ca67d57b` was identified by the user as local. No contents were retrieved or modified. Whether prohibited details are present remains unknown.
- **Separate live balance smoke failed:** two provider turns were made; the model searched the empty memory policy fixture and read the balance. The service abstained because the run lacked a valid policy citation. See its response and trace ID in `responses.json`. This does not establish a passing live deployment.
- No hosted deployment was tested. Restart/rebuild the local service from this source before rehearsing; these changes have not been verified in the existing Docker application.

## Design and limits

Internal `LeaveDecision` classifications have constrained fields. The backend renders approved clarification/refusal text; model prose cannot bypass policy grounding. Classification is conservative and pattern based, not a general natural-language parser. Action dates must be explicit ISO dates before model-driven proposal preparation. Known duration without dates can read balance but cannot write.

Annual leave starts at least three weekdays after the Jakarta submission date (Monday → Thursday, Friday → Wednesday); public holidays are not modeled. Confirmation revalidates dates, balance, overlap and medical reason under the existing transaction/owner lock. A successful idempotent replay returns before those checks.

Medical detection recognizes a limited set of Indonesian/English disclosure patterns and condition names. It can miss paraphrases or overblock general questions containing those terms. It is not universal medical-text detection. Trace allowlisting remains in place. Historical records are not automatically scrubbed.

## Resume database verification

After Docker is healthy, use the documented local PostgreSQL integration workflow and set `TEST_DATABASE_URL` to the intended test database. Tests create isolated random schemas. Run the non-live suite again and inspect the historical ticket read-only, reporting only presence flags. Do not reset the existing database or rewrite historical records.

For a fresh isolated response recording, run `python scripts/verify_qa_leave.py` from `HR-Agent` with configured model credentials. It uses memory fixtures and does not access historical data. Its output is not evidence of deployed PostgreSQL behavior.

## Presentation wording

“On October 22, the six corrected QA scenarios passed in Indonesian and English against local source with synthetic memory storage. These responses are controlled by the backend before a model call. Database integration and local deployment verification are still pending.”

Keep the October 19 15/15, citation-validity and latency figures labeled with their original date and scope. QA01–09 from the new workbook remain unverified in this update.
