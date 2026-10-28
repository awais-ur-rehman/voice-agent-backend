# Phase 3 CPU browser voice design

## Intent and approved boundary

Connect the Phase 2 guarded scheduling brain to real browser audio. The owner selected **CPU browser voice now, with GPU latency acceptance pending**, and explicitly approved merging Phase 2 PR #3 into `develop`. That merge is complete (`bfcf0b7`); this work is on `feat/phase-3-voice-integration`. Keep both Phase 3 repositories on feature branches with draft PRs and stop at the owner testing checkpoint. No Phase 4 dashboard or Phase 5 knowledge ingestion is included.

The CPU checkpoint must demonstrate real English and Urdu speech-to-speech booking through PostgreSQL/Valkey, corrections, interruption, reconnection and cleanup. It cannot satisfy or claim the original production latency exit criteria. Native pronunciation/MOS, the multi-speaker Urdu accuracy corpus, GPU performance and pilot concurrency remain separate pending gates. A fake speech provider, typed transcript, sliced pre-rendered answer or HTTP-only test is not proof of real voice inference.

## Constraints and approaches

The inspected machine is Apple M2, arm64, with 8 GiB physical RAM. Preserve existing development database volumes, account passwords, JWT secrets and both credential files. Nemotron remains `nvidia/nemotron-3.5-bolt-e4b`, thinking disabled. New Hugging Face access is server-side only. Synthetic development data only; audio and identifying transcripts are not persisted by default.

The owner chose the CPU approach over providing a GPU endpoint. CPU permits functional local development now but requires careful model-memory control and honest timing. GPU-backed inference would support the original latency work sooner but is not selected, and no paid server is authorized. A browser-native speech replacement would differ across operating systems, obscure the Urdu model/voice choice and bypass the specified providers; it is not the acceptance path.

Use Pipecat with self-hosted LiveKit, Silero VAD, faster-whisper, Kokoro English and Indic Parler Urdu behind replaceable adapters. The first implementation milestone is a real speech capability/memory check, before integrating all models into a call. Start with the specified `large-v3-turbo` on CPU int8; do not silently replace it with a smaller Whisper model. A smaller multilingual development profile, if needed, must be explicitly labelled and measured separately. Full Urdu synthesis must run safely on available memory; if it cannot, report the measured blocker rather than fabricate completion or adopt an unreviewed voice.

## Dependencies, artifacts and CPU resources

Keep speech dependencies in a separately locked Python 3.12 environment under `voice/`, independent of the existing API environment. Run inference natively on this Mac to avoid an extra speech VM footprint. LiveKit, PostgreSQL and Valkey remain Compose services. Preserve the existing Compose project/volume identifiers. Lock compatible released packages, native wheel support, image digests and model revisions; inspect actual licenses before downloading/adopting weights. Do not install moving Git branches or resolve a compatibility problem by downgrading the API stack.

Public metadata checked on October 28, 2026 identifies:

- Indic Parler: `ai4bharat/indic-parler-tts`, revision `660bc53c64ae83150613264111708a0465211237`, Apache-2.0 metadata, gated access, 937,803,241 parameters.
- Kokoro: `hexgrad/Kokoro-82M`, revision `f3ff3571791e39611d31c381e3a41a3af07b4987`, Apache-2.0 metadata, ungated.
- Converted Whisper: `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`, MIT metadata, ungated. Verify the converter/upstream artifact chain in the license ledger.

These are planning pins, not downloaded-weight or native-quality approval. Verify the gated artifact and token without printing credentials or signed download URLs. The owner has accepted model access and saved a read-only `HF_TOKEN`; use the existing ignored backend `.env` as the explicit model-download credential source, never a browser variable or image layer. Cache downloaded public weights outside Git and keep file hashes/revisions in the ledger. No automatic fallback to Piper: its current maintained engine license and an actual Urdu voice artifact must be reviewed separately from the old spec's MIT shorthand.

One CPU call and one heavy inference job at a time are the initial capacity limit. Keep the media/control loop and VAD responsive independently of heavy model work. Benchmark cold/warm load, peak RSS, first PCM and completion times. Reclaim idle heavy speech processes/model memory between ASR and Urdu synthesis if simultaneous residency is unsafe. Apply bounded queues, thread counts and timeouts; terminate a stalled inference child and discard its late output. State a tested memory budget from measurements, rather than assuming all weights fit in 8 GiB. Report CPU, optional local acceleration and any future GPU results as distinct profiles. Native Apple acceleration is not a CPU benchmark and is not assumed for acceptance.

## Boundaries and media authorization

The API issues voice sessions only to authenticated staff for an enabled agent in their tenant, using its immutable agent version. Create a linked tenant-RLS-protected voice-session record and ordinary guarded agent session. Room names and participant identities are opaque and contain no patient data. Room provisioning occurs outside SQL locks; failed provisioning revokes the incomplete session and returns a truthful error. Creation retries must not create duplicate active sessions/workers.

The browser receives a short-lived LiveKit token scoped to one room and one server-selected caller identity: microphone publication, subscription and bounded call-control/playback messages only. It receives neither the LiveKit signing secret, NIM/HF credentials, backend runtime capability nor worker credentials. Authentication/refresh material stays in browser memory. The CPU capacity limit is enforced on the server and returns a generic busy response without disclosing other tenants.

A separately authenticated worker claims a voice session with a PostgreSQL lease/generation fence. Renew the lease while its control loop is healthy; another worker may resume only after expiry. Internal runtime capabilities are delivered server-to-worker and remain bound to tenant, agent, version and session. A stale worker cannot send a new command or reinstate an old delivery after losing its lease. Valkey queue notifications contain identifiers, not credentials or transcripts. Browser messages are not trusted state patches; validate their schema, participant, sequence, size and generation.

## Voice-to-brain integration and critical confirmation

Reuse Phase 2 action planning, literal date resolution, exact patient verification, offered references, holds, shared scheduling transactions and command receipts. Refactor shared turn orchestration only where required; do not fork a second scheduling implementation. All authorization-relevant voice transitions commit through the existing SQL guard. Provider calls remain outside SQL locks, with revision checks before applying results.

A voice-bound session has a server-fixed transport mode and protected delivery state. **Issuing a readback is not the same as delivering it.** Confirmation in the core guard requires the current readback revision/generation to be completely delivered, followed by a fresh explicit affirmative utterance that began after delivery completion. Apply this in the core confirmation boundary, so a raw text command with an internal capability cannot bypass voice delivery requirements. Caller/model arguments cannot set transport, delivery, verification or confirmed flags.

Only the selected code-generated `say[lang]` reaches speech synthesis. Model text, reasoning, additional confirmation prose and unsolicited translations never enter TTS. Check hold ownership, expiry, current catalog/readback snapshots and policy again at commit. CPU synthesis never extends a two-minute hold. If preparing/playing the readback consumes the lease, invalidate it and offer fresh availability. Keep server-generated operation keys stable across the single transient mutation retry. A disconnect after database commit replays the receipt; it never re-books or treats a completed transaction as cancelled.

## Speech recognition and identity input

Capture the caller's audio track only, not the worker's output/mixed room audio. Browser echo cancellation/noise suppression and supported sample formats are explicit. Normalize bounded mono PCM for ASR; cap utterance size/duration and call length (initial maximum ten minutes). Do not retain buffers after their use.

Fixed English/Urdu agents force that ASR language. Bilingual mode evaluates only English and Urdu from the first useful audio, records honest confidence/ambiguity, and transcribes using the chosen language. Do not let an unrestricted Hindi result produce Devanagari or silently transliterate corruption. Reject/retry uncertain or invalid-script extraction. Use the existing two-dominant-turn response-language rule with trusted ASR metadata; text-only callers continue using their existing adapter. Do not claim Whisper language-ID was implemented by text heuristics.

Public provider/service vocabulary may improve ASR. Never prompt ASR with candidate private patient names/DOBs or accept fuzzy identity matches. Add exact matching against an approved `name_ur` alongside the existing canonical name, still bound to the looked-up phone and unique candidate. Handle unambiguous English/Urdu digit sequences before phone validation; ambiguous numbers require repetition and readback. DOB parsing, if used, must be explicit/unambiguous and stay out of NIM. No ASR confidence or model statement grants verification. Names/phone factors remain in code, and the global three-attempt budget survives subject/task changes.

## Streaming, output and interruption

Use actual Pipecat/LiveKit frames and cancellable provider adapters. Send mono PCM at the required transport rate with bounded output queue duration, resampling from each engine's real output rate. Measure genuine first PCM latency. If an engine can only finish a segment before emitting audio, report segmented synthesis; do not label slicing a complete WAV as native model streaming.

Every spoken utterance has a generation ID and bounded text/audio segments. On sustained caller speech, promptly stop/flush output, cancel pending planning/synthesis, invalidate the interrupted readback and fence late frames/results. Process the new utterance from current SQL state. A backchannel may resume a noncritical answer from a safe boundary; a critical readback is reissued fully. An early “جی/yes,” even if its ASR result arrives later, cannot confirm a still-playing readback. Distinguish interruption before mutation from a result already committed: reconcile the receipt before any further corrective transaction.

Maintain a delivery ledger using output timing and participant-bound playback acknowledgments capped at published samples. Persist/use only fully acknowledged segments as delivered history; drop an unacknowledged in-flight segment on interruption. This is a conservative lower bound on played text, not invented word alignment or proof that a human heard an unmuted device. If reliable word timing exists, use it; otherwise do not estimate an unheard partial word. Critical confirmation requires acknowledgment of every segment of the current readback. Missing/late/stale acknowledgments fail closed. The owner still checks actual speaker playback during manual browser testing.

Use pre-rendered public greeting/filler clips keyed by exact model/voice/config/text/sample-rate hashes. Fillers are status speech, do not enter task history or count as successful answers, and must not overlap the actual response. Dynamic patient confirmations/audio remain ephemeral and are not placed in a shared persistent cache. Freeze one tested English/Urdu voice pair/config/seed for this checkpoint; six-voice native acceptance/full picker remain pending for the voice catalog/dashboard work.

## Reconnect, timers and failure behavior

A transient media disconnect immediately fences playback and invalidates its readback/hold, while retaining allowed structured task state. Allow an initial thirty-second reconnection grace period within the existing live SQL session lease. Resume requires the original authorized media session or staff authorization, rotates scoped grants and rechecks the worker lease. A phone number alone cannot restore a session. After grace expiry, hangup or maximum call time, end/revoke the session, release holds and clean up the room/worker. Browser refresh does not silently revive verification from local storage.

Silence uses the existing six/twelve-second reprompts and polite end, with timing driven by real VAD/media activity. Keep deliberate microphone mute distinguishable from an ASR failure. Missing models, memory exhaustion, provider timeout, rejected media authorization and disconnected output have explicit terminal/retry states. Never show a working speaker/connected state for an unavailable pipeline. Callback/handoff remains a queued clinic request; no external phone/SMS/WhatsApp action is added.

## Browser test harness and separate repositories

The frontend repo gains a minimal React/Vite/strict-TypeScript voice test page, TanStack Query for server state and a coherent shadcn/ui primitive foundation. It includes existing staff sign-in, selection of a tenant-scoped published agent, start/mute/end/reconnect controls, actual connection/listening/thinking/speaking/error states, optional ephemeral captions and measured stage timings. This is the audio testing harness required by Phase 3, not the Phase 4 configuration/calendar/dashboard.

Use reusable controls/status/error treatments, accessible keyboard/focus behavior, responsive layout, reduced motion and direction-aware Urdu captions with appropriate Nastaliq line height. Follow the repository's established design references; use an orb only if it communicates real media activity. No invented metrics, decorative dashboard scaffolding, client-side provider keys or patient-content persistence. Browser microphone/autoplay permissions have explicit user-gesture and retry behavior. API/media contracts are versioned/generated from backend schema artifacts and checked for drift; do not hand-maintain incompatible copies across repositories.

## Verification and checkpoint

Preserve all 429 Phase 2 and 63 Phase 0 checks. Keep the earlier unreproduced retry-test investigation visible; do not relabel it fixed without evidence. Add real SQL/Valkey tests for voice-session RLS, grant isolation, worker fencing, delivery-bound confirmation, early affirmative, interruption/correction, expired hold during synthesis, duplicate command/reconnect and committed-receipt recovery. Test media/provider failure cancellation with deterministic adapters, clearly separate from real inference results.

Run an actual local LiveKit room with a real worker and RTC participant audio, then real browser microphone/playback testing where hardware is available. Execute synthetic English and Urdu voice booking/reschedule/cancel, a digression/correction, family/subject switch and reconnect. Assert actual scheduling effects and no mutation on incomplete/early confirmation. Verify late audio/results are discarded, static caches are safe and cleanup works after errors. Test the frontend interactions/accessibility/build and visually inspect desktop/mobile and Urdu content. No synthetic loopback recording is presented as the uncollected multi-speaker/native accuracy corpus.

Record artifact/license pins, CPU resource use, p50/p95 per stage and end-to-end, failures, actual NIM request counts, cold/warm cache conditions and whether playback evidence is automated or owner-observed. GPU SLOs (English p50 ~0.8 s, Urdu p50 <1.2 s/p95 <2 s), full native MOS/STT corpus and production capacity remain explicitly pending under the owner's CPU decision. If real Urdu audio or safe CPU inference is blocked, report that blocker and incomplete exit rather than counting mocks as completion.

Finish with independent review, relevant tests/types/lint/build/CI, runnable local services, exact owner instructions and separate backend/frontend draft PRs into `develop`. Leave Phase 3 unmerged and stop before Phase 4. Routine implementation details—module names, exact compatible package versions, measured queue/time budgets and the tested default voice pair—are builder decisions, recorded before adoption and bounded by this design.

## Primary references checked during design

- [Pipecat LiveKit transport](https://docs.pipecat.ai/server/services/transport/livekit)
- [faster-whisper CPU/int8 and streaming integrations](https://github.com/SYSTRAN/faster-whisper)
- [Indic Parler model](https://huggingface.co/ai4bharat/indic-parler-tts)
- [Kokoro model](https://huggingface.co/hexgrad/Kokoro-82M)
- [Maintained Piper engine](https://github.com/OHF-Voice/piper1-gpl)
