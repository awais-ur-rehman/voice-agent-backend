# Phase 7 CPU Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect and qualify the existing local CPU scheduler/control plane against provider failures, abuse and recovery faults, without claiming real voice/pilot readiness.

**Architecture:** Keep SQL the scheduling/authorization authority and wrap the existing planner with bounded endpoint routing. Add narrow SQL admission, ASGI limits/drain, operator-only isolated recovery and deterministic load/chaos tools; reuse dashboard components for safe failure messages.

**Tech Stack:** Existing Python3.12/FastAPI/SQLAlchemy/Postgres16/Valkey/httpx2, React/Vite/strict TypeScript/TanStack/shadcn; committed dependency locks, no new dependency/model planned.

**Spec:** `docs/superpowers/specs/2026-11-09-phase-7-hardening-design.md` (including builder-check resolutions), governing original v0.1/v0.2 documents in sibling `documents/`.

## Global Constraints

- Work only Phase7; stop before Phase8. Existing named backend/frontend checkouts, feat/phase-7-hardening stacked on unmerged feat/phase-6-analytics; no merge/retarget/force push. Public draft publication is authorized; controller verifies final exact-head CI.
- No GPU, real NIM/provider call, model/tokenizer asset download, embedding/knowledge/native voice service activation, invented vector/event, production/publicwidget/pilot or external message. Dental24sources stay queued/0vectors. Real voice20concurrency/latency/Urduquality/fallback and deployment SLAs remain pending.
- Runtime API/model/parser separation and existing locks/licenses remain. No new dependency without primary-source compatibility/license verification. Thinking remains disabled, state-constrained schemas/code-owned dates/names/numbers unchanged.
- Tenant/RLS/capability/idempotency/consent/retention fences remain authoritative. No raw body/query/provider error/signedURL/credentials/PII in logs or public evidence; no owner dotenv read by automated tests. Test secrets/identities only.
- DB tests ONLY voice_agent_test on55433, testValkey56379; ONE DB suite/harness at a time. Never stamp/downgrade/reset the OWNER/development database, delete owner volumes, rewrite migrations0001–0016 or alter existing role credentials. Existing guarded migration round-trip tests may downgrade/re-upgrade ONLY disposable voice_agent_test, serially. Migrations0017provider provenance and0018admission are additive.
- Provider wall3seconds/attempt, logical6seconds, <=1secondary on429/5xx/transport/timeout; no auth/malformed/policy fallback. Rolling5failures/30seconds, open60seconds, onehalfopen probe. Shared endpoint/key operator bucket10rpm/burst1, total request budget retained; outstanding attempts default4, configurable1–20, no unbounded queue/background audits.
- Ordinary JSON request cap64KiB; existing binary cap preserved. Auth rate10login/min/IP,30refresh/min/IP; authenticated new session30starts/10min/user and120/10min/IP, idempotent replays uncharged. SQL atomic limiter, HMACkeys/no raw identity, Retry-After1–3600; fail closed503 on unavailable admission DB. Default uvicorn no proxy header trust.
- Drain defaultfalse; readiness/new sessions503; existing end/resume/heartbeat/cleanup/auth/recovery allowed. No public drain control. All sensitive HTTP responses/errors no-store; nosniff/referrer-policy, explicitCORS, no false localTLS/encryption claims.
- Backup dir0700/files0600; actualrestore ONLY new tool-owned network-none/portless/tmpfs PostgreSQL container with pinnedpgvector image/memory512MiB/CPU1 and new private blob root. Authenticate original receipts then rebind only copies; plaintext/HMAC is not encryption. No real owner data in public reports.
- Retain failed receipts/qualifications and freeze tested bytes. Reuse prior unchanged-byte gates; full final gates once source stable. Native process tests/browser may need authorized OS access, no other-app kills. KeepownerAPI8000/frontend5176/privateCORSoverlay/volumes/secrets/flags intact.

## Review Focus

1. Timeout with a still-running primary or ledger writer: bounded work remains occupied, late success cannot execute tools/reset newer circuit/replace secondary; no new HTTP after deadline. Task1 controlled barriers + injected clocks.
2. Spoofed transport identity, concurrent limiter hits and idempotent start replay: no bypass, double charging or blocked cleanup; no-store/body limits apply to error/streamed responses. Task2 real SQL concurrency and ASGI seam cases.
3. Apparently valid backup with tampered receipt, copied inode, archive traversal or wrong Docker target: fail before destructive action; source untouched, restored receipt remains application-valid. Task3 adversarial bundle and actual isolated restore.
4. Oversized/malicious rate-limit or outage body arriving after logout: safe bounded UI message, no mutation retry/stale identity/storage. Task4 shared-client/session/browser cases.
5. Twentyclients driving one slot, cache/DB/provider outages or drain mid-session: exactlyone booking, no unauthorized replay/resurrection and honest observed capacity/timing. Task5 opt-in dedicated-test harness/chaos.

---

### Task 1: Bounded provider router and truthful invocation provenance

**Files:** create `brain/provider_router.py`, `tests_brain/test_provider_router.py`, `migrations/versions/0017_provider_routing.py`; modify `brain/provider.py`, `scheduler/config.py`, `analytics/{contracts,models,telemetry}.py`, affected usage/provider tests, `.env.api.example` and `docs/phase-7-provider.md` as needed. Preserve public Nemotron compatibility.

**Interfaces:** Consume existing Nemotron.plan(state,text,config,facts), ground(query,language,chunks,*,deadline), current_sink/MeasurementSink and independent database_writer. Produce EndpointPolicy/Circuit state in the internal router, `Nemotron.drain(timeout:float)->bool`, validated primary/optionalsecondary settings, per-attempt UsageObservation route primary/secondary + nullable fallback_from UUID/provider nim|openai_compatible. Forward results only through existing decoder. Builder chooses cohesive internal class signatures; document them in report for Task5.

- [x] Write deterministic RED tests `test_primary_429_falls_back_once_with_distinct_usage`, `test_auth_and_invalid_action_never_fall_back`, `test_shared_endpoint_key_bucket_and_total_budget`, `test_rolling_breaker_single_half_open_probe`, `test_timeout_late_success_cannot_reset_circuit_or_replace_result`, `test_hung_transport_or_ledger_retains_bounded_slot`, `test_ground_deadline_and_close_do_not_queue_new_http`. Use MockTransport/barriers/injectedmonotonic clocks; no real sleeps for60second windows. Assert wall3/6 bounds, correct request counts, no thread growth/SQLlock/secret leakage, honest missing audit on outage.
- [x] Run new router/provider/usage focused tests and retain actual failure output before product changes. Existing tests with multiple MockTransport calls may set explicit synthetic budget/burst; don't weaken productiondefaults or existing auth/malformed guards to force green.
- [x] Implement bounded synchronous facade with network workers whose capacity remains held through actual work/persistence exit. Reserve actual metadata without caller SQL I/O, resolve within deadline, persist bounded best-effort observation after result; no arbitrary background queue. Preserve sink requests/stage association and known/null cost semantics across late completion/domain rollback. Extend optional provenance additively through0017, immutable earlier migrations.
- [x] GREEN focused tests plus affected SQL usage/contracts/RLS, Ruff/format/types and public contract checks. Check actualcallcounts, circuit snapshots at boundaries and compatibleplan/ground/close. No runningdev deployment/fullDB repetition yet; final core gate belongsTask5.
- [x] Self-review, freeze changedproduct/test hashes, record exactargv/cwd/exit/logs and any retained failures, commit focused feature. Report public signatures/usage persistence timing so later tasks cannot guess.

### Task 2: Distributed abuse protection, HTTP perimeter and drain

**Files:** create `scheduler/admission.py`, `scheduler/http_security.py`, `migrations/versions/0018_admission.py`, `tests_api/test_hardening.py`; modify `scheduler/{main,config,auth,errors}.py`, `brain/{routes,voice_routes,voice_sessions,draft_test_routes,draft_tests}.py`, relevant tests, runtimeDockerCMD/Compose/example settings and `docs/phase-7-security.md`.

**Interfaces:** Consume session_factory/Principal/auth_runtime/current session start idempotency seams. Produce narrow SQL `admission_admit(scope,key,limit,window_seconds)` boolean+retrysec via fixed SECURITYDEFINERsearch_path; `admit_auth(request,scope)` and `admit_start(request,user,*,replay_key=None)` helper(s) with deterministic test injection; RequestLimits ASGI outer boundary; validated drain_mode with existing readiness/state authority. Internal naming may be refined before implementation and must be reported, no public application API created.

- [x] RED realSQL atomic limit races/expiry/cleanup/grants and independent rejectedlogin transactions; invalid/staff/runtime credentials, spoofedForwarded/XFF/Origin, opaqueHMACkeys and logcapture/exception path PII. Pin10/30auth and30/120starts plus integerRetryAfter, no store on401/403/422/429/503. Exact voice/draft replay remains uncharged and no clinical/receipt duplication.
- [x] RED rawASGI streamed/lying/duplicate/negativeContentLength/oversized JSON and unchangedknowledgebinary bounds; earlyrejection before parsing/provider/SQL side effects where possible. Drain stops onlynewtext/voice/draft/voiceattach and readiness, existinginspect/resume/end/workerheartbeat/metrics/cleanup remain permitted. No response-cache/body/forwardedidentity leakage.
- [x] Implement0018 narrow opaque SQL limiter plus boundedexpiry; no optionalcachedependency. Wire admission after authentication and at authoritative idempotency seam; short independent tx for admission, no lock across provider/media. Add safe no-store/security/genericexception handling, schema-declared validation field paths (no attacker extra-key echo), expose Retry-After through existing explicit CORS, trustedproxy-off default and drain config. Integrate close then bounded asynchronous planner.drain before engine disposal in app lifespan, retaining safe shutdown on timeout and existing cleanup behavior; cover actual lifespan ordering and no caller/loop blocking. Preserve existing registrationpolicy and image separation.
- [x] GREEN focused SQL/ASGI/auth/voice/draft tests thenstatic/types/contracts; check immutable0001–0017, no falseprovider rows, no cleanupdenial. Final backend/fullimage deployment belongsTask5.
- [x] Self-review/freeze/report exact testedsource/logs, privilegecatalog proof and limits; commit. Report any settings/migration/publicerrors consumed by frontendTask4 and loadTask5.

### Task 3: Private backup bundle and isolated restore drill

**Files:** create `scripts/recovery.py`, focused helper `scripts/recovery_bundle.py` if needed, `tests_environment/test_recovery.py`, `docs/phase-7-recovery.md`; modify only necessary packaging/CI/runbook references. No HTTP restore endpoint or owner database mutation.

**Interfaces:** Produce versioned private manifest+DBdump+rolesSQL+blob snapshot bundle and operator `python -m scripts.recovery backup|verify|drill --allow-local-operations ...` (exact arguments documented in report). Defaults refuse action beforeoptin. Target is always newly tool-created network-none/tmpfscontainer and private blobdirectory, never arbitraryDSN/containerinput. Source credential loading is explicit operator-only/inprocess, never testdotenv/defaultstartup.

- [x] RED optin-before-read, wrong target/DB/image/volume/network/ports refusal, malformedmanifest/version/path/archive/symlink/hardlink/hash/truncation, unsafepermissions, tamperedHMAC/identity/retirement metadata and copiedinode. Assert no source mutation and cleanup onlytool-ownedtarget; subprocess failures bounded/sanitized, noenv/SQLrows/credentials in exceptions/logs. Syntheticfixtures forbackup/restore commands only.
- [x] Implement consistentDBsnapshot export/dump+privatecriticaltable/role/blob manifests; require an out-of-band expected manifest SHA256 from the trusted creation receipt when verifying/drilling, not a self-asserted hash from untrusted bundle contents; fail changedroles/blob snapshots, preservealloriginals. Restore savedroles underuniquebootstrapadmin, customdump into new pinnedPGcontainer and copyblobs into newroot. Verify original receiptHMAC+canonicalidentity+size/hash, rebind copieddevice/inode+signature only aftervalidation, keepkey/timestamps. PreserveliveBlobStore checks.
- [x] GREEN isolated unit/adversarial tests, lint/format/types and tooloptin/disabled smoke without readingownerfiles or spawningunapprovedtargets. Resourceadmission and hardcommandcleanup deadlines explicit; leave actualdrill forTask5 afterbackup review.
- [x] Document exact recoverablecontents/backupconsistency/rolekeyrequirements/plaintext limits, sourcevsrestoreguards, RPO/RTO observations ratherthanSLA, nooffsite/encryption claim. Freeze/report/commit. Task5 consumes the reviewedtool, not ad-hoc restore scripts.

### Task 4: Safe bounded frontend failure and retry messages

**Files:** modify frontend `src/api/{client,dashboard,session}.ts` onlywhere required, focused api/session tests and shared query/form error UI; create `e2e/hardening.spec.ts`, `docs/phase-7-testing.md`. Reuse currentUI/tokens; no visualredesign/dependencies.

**Interfaces:** Consume Task2 existingerror envelope+Retry-After1–3600. Produce a shared bounded error-body reader if bothclients need it; DashboardError/ApiError exposes validated optional retryAfterSeconds and allowlisted genericmessages. Existing generatedcontracts/operations/session.request signatures unchanged;401singleflightrecovery policy unchanged.

- [ ] RED readstreamhardcap16KiB (legacyvoice2KiBmayretainstrictercap), chunked/infinitebodycancel, hostilebodymessage/fieldkey/HTML/script/signedURL, invalidRetryAfter,429/503/hardeningcodes, existingfield/alternatives/conflict/permission behavior. Sessionlogout/late401/rate-limit/abort cannot replaymutation or restoreidentity; zerostorage/logPII.
- [ ] Implement bounded reader/cancel and safe code-owned messages/validatedfield+alternatives mapping; parseintegerRetryAfter1–3600 only, visiblehumanretrynotice without timers or auto credential/mutation replay. Keep existing keyboard/responsiveforms. Usefrontend-design/Vercelguidance ifproductUIchangesneeded.
- [ ] GREEN fullfrontend `npm run check` and maintainedaffected/browser hardening fixtures1440/390; visuallyinspect screenshots, no overflow/focusregressions/storage, preserve deliberateexistingwarningqualifications. Do not rerunbackend/native here.
- [ ] Freeze/report/commit ownfrontend only. Record exactlinkage to backenderrorcontract andcopy, no patientfixtures/ownersecretreads. Controllerownsindependentreview.

### Task 5: CPU load/chaos qualification, security matrix and local checkpoint

**Files:** create `scripts/hardening_load.py` (small workloadhelper if needed), `tests_environment/test_hardening_load.py`, `tests_brain/test_hardening_chaos.py`, `docs/phase-7-{testing,results,runbook,security-review}.md`; modify CI/CONTRIBUTING/README/exampleoverlays forPhase7stackedbase. Frontenddocs/results ifactualproofneeded; no unrelatedproductexpansion.

**Interfaces:** Consume reviewed router/drain/admission/recoverytool APIs and existing fixed dentaloptinread-onlybrowser. Produce safeJSON evidence with runprofile/revisions/clock/runtimehost/requested+observedconcurrency/statuscounts/errorcounts/p50/p95/correctness/limitations; publishonlysyntheticfixtures. Controllerownsfinalreview/publication.

- [ ] RED optin/dedicatedvoice_agent_test+testValkey/loopbackguards beforewrite/network, max20clients/30rounds and externalcallsblocked. Implement deterministicinjectedprovider/mediaASGI workload with realtestDB:20parallelclients*30rounds slotsearch, independentsessioninfocommands+exactreplay,20-wayoneslotraceexactlyonesuccess. Warmup separate, nofixturethresholdtuning; p95tool150mstarget explicitfail/nfr_unmet, notruntimevoiceacceptance. Record datasource/method/status/timeouts and enoughsamplecounts.
- [ ] RED/GREEN maintainedchaos forprovider429/5xx/timeout/circuit/latework/cacheoutage/DBconflict+unavailability/clientcancellation/lease/reconnect/drain. ExistingSQLguards remainauthoritative, exactlyoneoperation/callback, safeerror/logs, nolateserverauthority. No killingowner services or true provider/audiofaults.
- [ ] Run fullserialbackend/static/types/publicschemas/offline/native onfrozenbytes, affectedruntimebuild/import/hardenedstartup and fullfrontendcheck+maintainedbrowser. Reuseunchangedearliergates byhash; no duplicateDBsuites. Preservefailedlogs/warnings. StartonlyexistingtestPG/Valkeyandstopifappropriateafterfinishwithoutvolumedeletion.
- [ ] Execute reviewedbackup+actualisolatedrestoredrill withresourceadmission; private0600sourceproof includescount/digest/role/blobmatches, restoresignedcopiedreceipts; zero sourcecredential/clinicalchange. Ifmemoryblocktruthfullyrecordpendingrealrestore. BeforeactualdevAPIrefresh takecurrentprivatebackup; normaladditivemigration0016→0018 and refreshAPIonlyplusneededanalyticsworkerimage, preserveallrolehashes/CORSoverlay/flags/ports/volumes. Verifyactualsourceimagebytes/readiness/429+drainconfigurationwithoutnewdemosessions. Keepinferenceoff andownerfrontend5176available.
- [ ] Runmaintainedread-onlydentaldemoafterfinalrefresh. Updateboundaries/ownerchecklist/securitymatrix/runbook withactual20clientmeasurements, crashrecovery/cache/sourceparity andpendingrealGPU/Urdu/NIMquota/fallback/publicwidget/TLS/offsite/pilot. Checknonmutationofexistingclinical/sourcecounts/digestsandthebaseline5Text/0Voice demo; don'trefillprocessmetrics artificially. Freezeallsource/test/docs/evidencemaps,commit/report readyforcontrollerreview.
- [ ] Controllerfreshwholebranchreview,oneconsolidatedfixwave/scopedrereviewifneeded,publicdraftPhase7PRsbasefeat/phase-6-analytics(allowlistCI), attachartifacts/verifyauthorship/bases/files/testedheads/allhostedchecks. AllPhase4–7remainunmerged. Archiveprivatefailed+finalevidence beforeplanworkspacecleanup; exhaustiveRulings+costs inownerhandoff. StopbeforePhase8.
