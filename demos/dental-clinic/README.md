# Pearl Dental Studio — Demo

A fictional English/Urdu dental clinic for the existing Phase 5 local demonstration. The clinicians, clinic, address, policies and prices are invented demo data. There are no patient records, real contact details, credentials or clinical treatment instructions in this package.

## What is included

- `clinic.json`: structured clinic/profile, providers, service prices/durations, opening hours and 24 FAQ sources (12 English and 12 Urdu).
- `knowledge-en.md` / `knowledge-ur.md`: readable copies of the same knowledge content. These are alternative import artifacts; do not upload them in addition to the 24 existing sources unless intentionally testing duplicate content.
- Twelve knowledge topics: available services, visitor/bicycle parking, accessibility, payments, insurance, children/guardians, anxious visitors, first consultation, orthodontics, walk-ins/late arrival, records/receipts, languages/waiting facilities.

Location: Suite 2, Demo Health Plaza, Example Avenue, Karachi — explicitly fictional. Timezone: Asia/Karachi. Monday–Thursday and Saturday: 10:00–19:00. Friday: 15:00–19:00. Sunday: closed. Both fictional providers use these hours; actual availability is computed from scheduling records, never promised from document text.

| Service | Dentist | Duration | Demo fee (PKR) |
| --- | --- | --- | --- |
| Dental consultation | Dr Ayesha Khan | 30 minutes | 2,500 |
| Scaling and polishing | Dr Ayesha Khan | 45 minutes | 6,500 |
| Dental filling assessment | Dr Ayesha Khan | 30 minutes | 2,500 |
| Root canal assessment | Dr Ayesha Khan | 30 minutes | 3,000 |
| Children dental consultation | Dr Ayesha Khan | 30 minutes | 2,000 |
| Orthodontic consultation | Dr Omar Raza | 45 minutes | 3,500 |

Five-minute after-appointment buffers apply. Filling/root-canal assessment fees are for the assessment, not complete treatment. Full braces/aligner plans and treatment quotations require the dentist; no full-treatment price is defined. Implants, sedation, oral surgery and home visits are not offered in this demo.

Scheduling policy: one hour minimum booking notice, 30-day booking horizon, two-hour cancellation window and three appointments per patient per day. Appointment confirmation, cancellation eligibility and available slots remain controlled by the scheduler. Arrive ten minutes early; bring previous dental reports, medicine/allergy lists and appointment details. Minors attend with a parent/legal guardian. Procedure-specific instructions come from the dentist.

## Current local state — 2026-11-08

A separate local clinic and published bilingual agent have been created through the normal authenticated API. There are two providers, six services, 12 weekly availability rules and 24 persisted FAQ sources. Setup checks for clinic profile/providers/services/hours/published agent are true. Existing clinics were not edited.

**Embedding is paused by the owner.** The real pinned BGE-M3 qualification attempt stopped at `memory_floor` before loading a child (peak child RSS zero). Sources are **queued**, with **zero chunks/generations** and processing/query availability false. No ready sources, retrieval results or RAG answers are claimed. Do not change the model, lower memory bounds or write fixture vectors to make the demo appear ready. No Phase 6 work is included.

Local dashboard: `http://localhost:5176`. The private ignored `backend/.local/dental-demo.json` contains the demo account credentials, tenant/agent IDs and direct Knowledge URL. It is mode 0600 and must never be committed. Sign out of another clinic before logging into this dedicated account. The private import proof is `.local/dental-demo-import-proof.json`.

Hours, fees, location and preparation are also configured in the structured records. They take precedence over documents. Native voice remains disabled; use the text test console. A local demo-agent Publish is separate from merging the unmerged Phase 5 code PR.

## Demo script after real ingestion succeeds

1. Show Knowledge sources as genuinely ready with real chunk counts and previews.
2. Start the isolated test clinic. Ask `Is parking available?` / `کیا پارکنگ موجود ہے؟`; expect visitor parking behind the plaza and a validated citation.
3. Ask `Do you accept dental insurance?` / `کیا انشورنس قبول ہوتی ہے؟`; expect no direct billing, a receipt for insurer reimbursement, and a citation.
4. Ask `Is the clinic wheelchair accessible?` or ask an English question against the Urdu source; check factual accuracy and literal citation provenance.
5. Ask `Which payment methods do you accept?`; expect cash/cards/bank transfer at reception, no payment link or collection of card details.
6. Ask hours, consultation fee, address and preparation; verify structured facts win. These do not establish knowledge retrieval quality because code owns their route.
7. Ask for the full braces package price, a guaranteed same-day treatment, an unrelated MRI price or another patient's records. Expect a safe limit/decline rather than invented data, a diagnosis or an operation claim.
8. End the isolated test. Confirm effects remain in the synthetic child clinic.

These are intended acceptance checks, **not completed tests** while embedding is paused. Some requests may be routed to a code-owned clarification instead of knowledge; preserve those safeguards.

## Resuming embedding

Wait for the owner to confirm readiness. Use the pinned existing profile and the explicit qualification command in `docs/phase-5-embedding.md`; preserve the 1792 MiB admission floor, 1536 MiB child RSS cap and existing deadlines. Then provision only the narrow worker credential, start the existing private embedding/worker services and ingest these queued sources through the normal worker. Preserve database volumes, RLS and existing credentials. Inspect actual source statuses/chunks/previews, then run held-out bilingual retrieval and bounded Nemotron answer checks. Only those observations can qualify the actual demo as RAG-ready.
