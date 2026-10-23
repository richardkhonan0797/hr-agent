# Policy-question regression fixes — October 22, 2026

The broad “saya ingin … cuti” rule incorrectly treated a carry-over question as leave preparation. Want/need phrasing now requires a direct leave-taking intent. Carry-over and diagnosis-channel questions retrieve policy evidence before the model answers; write tools are blocked for these policy-only runs. Actual recognized medical disclosures still stop before model execution.

Expected policy content: annual carry-over is capped at five days, used before March 31, with human-reviewed exceptions. More than two consecutive working days of sick leave require supporting documents through HR; diagnosis details should not be sent through assistant chat. Answers still require retrieved citations, and missing evidence still causes abstention.

Verification: **290 non-live tests passed, 31 database tests skipped, 2 live tests deselected**. Eight new regressions cover both reported prompts and English translations, retrieved citations, prohibited model writes, actual disclosure blocking, and absent evidence. Tests use local policy files, memory storage, and scripted model outputs; they do not prove live-model phrasing or deployed PostgreSQL behavior. Existing rows 11–16 regressions also pass.

After explicit user approval, all four live checks passed using the configured provider, local policy files, and isolated memory storage. Both carry-over responses cite HR-LEAVE-001 and explain the five-day limit, March 31 deadline, and human-reviewed exception for eight days. Both sick-leave responses cite HR-LEAVE-002, direct documents to HR, and explain the more-than-two-consecutive-working-days requirement. No proposals, tickets, or requests were created. Actual responses and trace IDs are saved in `responses.json`.

The initial live attempt exposed a missing function-call entry before the preflight tool result; all four responses abstained. The call/result history pairing was fixed and covered by a regression assertion before the successful rerun. The failed attempt is preserved in `responses-before-pairing-fix.json`. This evidence does not verify the deployed PostgreSQL application.

To load the source changes in the Docker application, run `docker compose up -d --build api streamlit` from `HR-Agent`, retaining the same Compose project name if one was used. Database integration and deployed behavior remain unverified in this update.
