# Phase 3 native CPU speech dependencies

The API dependency lock is unchanged; its image excludes the native environments and model files. Native Python 3.12 workers use two
separate hash-locked environments under `voice/`. Released Parler-TTS 0.2.3 needs
`descript-audiotools-unofficial` with protobuf <5; released Pipecat 1.14.0 and
LiveKit 1.2.0 need protobuf >=5. They cannot truthfully share one resolver.
The media process launches isolated inference children using
`voice/inference/.venv/bin/python`; it never imports Torch into the media loop.

```sh
UV_CACHE_DIR=.local/uv-cache uv sync --project voice --locked
UV_CACHE_DIR=.local/uv-cache uv sync --project voice/inference --locked
UV_CACHE_DIR=.local/uv-cache uv run --project voice --locked pytest voice/tests
```

Run native subprocess/RSS tests with normal macOS process-inspection permissions.
The agent sandbox blocks child enumeration with `Operation not permitted`;
that must fail closed and is not evidence that the memory limit passed.

Primary release compatibility reviewed October 28, 2026:
[faster-whisper 1.2.1](https://pypi.org/project/faster-whisper/1.2.1/),
[CTranslate2 4.8.4](https://pypi.org/project/ctranslate2/4.8.4/),
[Kokoro 0.9.4](https://pypi.org/project/kokoro/0.9.4/),
[Parler-TTS 0.2.3](https://pypi.org/project/parler-tts/0.2.3/),
[Pipecat 1.14.0](https://pypi.org/project/pipecat-ai/1.14.0/).
Parler fixes Transformers 4.46.1. The inference lock uses Torch/torchaudio 2.5.1
with native macOS arm64 Python 3.12 wheels. The control lock uses Pipecat/LiveKit
without runner FastAPI extras; existing API dependencies remain independent.
Kokoro's English tokenizer `en_core_web_sm` 3.8.0 is an explicit release-wheel
reference with its hash in the inference lock, preventing Misaki's implicit
runtime tokenizer download. Exact transitive packages and wheel hashes are in
the two locks. Model/code notices and adoption scope are in `licenses.md`.

## Assets and secrets

The explicit download command reads only `HF_TOKEN` from existing ignored backend
`.env`, passes it in memory, and emits sanitized summary fields. It never prints
upstream exceptions because they can contain signed URLs. Hugging Face cache,
Xet cache, download manifests, package cache and private measurement results live
under ignored backend `.local/`. Direct HTTP downloads disable Xet after its
initial transfer stalled. Offline inference strips credential-bearing environment
keys, uses pinned local paths and refuses incomplete downloads.

```sh
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli download kokoro
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli download whisper
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli download parler
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli download description
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli gate kokoro
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.cli gate parler
UV_CACHE_DIR=.local/uv-cache uv run --project voice python -m voice_runtime.loopback
```

All revision pins are immutable: Whisper turbo
`0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`, Kokoro
`f3ff3571791e39611d31c381e3a41a3af07b4987`, Indic Parler
`660bc53c64ae83150613264111708a0465211237`. Parler's separate description tokenizer
is `google/flan-t5-large` at `0613663d0d48ea86ba8cb3d7a44f0f65dc596a2a`;
only tokenizer/config/license/card files are downloaded, not a second T5 weight
set. Per-file sizes and SHA256 hashes are generated in `.local/voice/*-manifest.json`.

## CPU capability gate, October 28

Apple M2 arm64, 8 GiB physical RAM. One heavy child at a time; two inference
threads; signed 16-bit little-endian mono PCM; <=30-second buffers, <=300-character
synthesis segment, bounded child output, 180-second deadline. Cancellation kills
the process group, discards late results and releases its serialized allocation.
Kokoro and Parler emit completed segments, not native frame streaming. Each
request starts a fresh child and reclaims all model residency afterward.

Real Kokoro CPU synthesis succeeded on synthetic public text with `af_heart`,
seed 42, speed 1: 55,800 samples at 24 kHz (2.325 seconds), nonzero finite PCM,
SHA256 `c9e1e45944a5fe382e484deee46f9e3e9744221478c2b5e5eea0deaa34d2d9bb`.
Cold load/import time 73.018 s; genuine first PCM ready 74.112 s; synthesis
1.097 s; total child lifecycle 82.816 s; sampled process-tree peak RSS
870,301,696 bytes. This is one cold synthetic result, not p50/p95 or production
latency acceptance. No microphone/native pronunciation evidence is implied.

**Urdu synthesis is blocked on this host's measured memory headroom.** At
2026-10-28 14:54:30 UTC, after dedicated project services were stopped, available
physical memory was 1,899,823,104 bytes and used swap was 8,789,753,856 bytes.
The 937,803,241-parameter Parler FP32 weights alone require about 3.49 GiB,
before activations/runtime overhead. The safe preflight requires 4.5 GiB available
and refuses the load. No Parler generation was attempted, no Urdu audio was
fabricated, and no smaller/unreviewed voice or acceleration profile was substituted.
This is a measured resource preflight refusal, not an observed model OOM or
an empirical minimum hardware requirement. Native Urdu quality, Urdu ASR loopback,
GPU SLOs and browser functional acceptance remain pending.

The safe English loopback was then attempted with the exact specified turbo
artifact on CPU int8, two threads, **1024 MiB child RSS cap**, 256 MiB minimum
remaining physical headroom, and a 180-second deadline. Kokoro regenerated the
synthetic input in memory. The ASR child reached a sampled RSS of
1,091,911,680 bytes and was killed for exceeding the memory budget. It produced
no accepted transcript. This proves cancellation/resource failure handling,
not successful ASR; English speech-to-speech readiness remains false too.
The failed result is `.local/voice/english-loopback.json`. No patient audio or
raw synthetic PCM was persisted by that attempt. A future larger-memory host
must rerun both real loopbacks before advertising working English/Urdu speech.

## Media worker and CI

The native control entry point is:

```sh
UV_CACHE_DIR=.local/uv-cache uv run --project voice --locked python -m voice_runtime.worker --api-url http://127.0.0.1:8000
UV_CACHE_DIR=.local/uv-cache uv run --project voice --locked python -m voice_runtime.rtc_smoke
```

Worker bootstrap authorization is read from ignored backend `.env.api` key
`SCHEDULER_VOICE_BOOTSTRAP_SECRET`; the worker does not receive the API's JWT
signing secret. HF/NIM credentials remain in the separate `.env`. Readiness
has a short server TTL and advertises a language only after a real successful
speech-to-speech loopback plus complete pinned local artifacts. Both language
capabilities currently remain false. PCM transport smoke uses an actual local
LiveKit room and decoded RTC audio; it explicitly does not claim ASR, voice
booking, microphone, speaker or pronunciation acceptance.

CI installs only the control lock (`uv sync --project voice --locked --all-groups`)
and runs `uv run --project voice --locked pytest voice/tests`. This suite uses
bounded real OS processes and deterministic control/media boundaries; it never
loads the workspace credential files, downloads model weights or starts database
resets. Check the inference lock with `uv lock --project voice/inference --check`
without installing the heavyweight environment in the API image.

At code freeze, the complete isolated worker suite has **20 passing tests**;
Ruff 0.17 lint and formatting pass, both locks pass `uv lock --check`, and actual
Pipecat/LiveKit native imports pass. Pipecat emits its upstream Python 3.12
`audioop` deprecation warning; Python 3.13 is not the supported profile.
The real RTC transport smoke currently times out at the native LiveKit
`Room.connect` boundary before worker connection, with zero input/output samples.
RoomService create/delete succeeds separately. This remains a transport blocker,
not a passed media test. The code is prepared for further integration review;
real microphone/speaker, RTC decoded speech, booking and owner acceptance are
not complete.


## Actual RTC transport verification

Initial peer-connection establishment repeatedly timed out, including a 60.016 s direct SDK test. After restarting the new local LiveKit service, two real Pipecat/LiveKit PCM smoke runs passed: 28,600 caller input samples and 45,360 nonzero decoded output samples. Warning-only server logging was restored before the second pass. The cause of the earlier ICE startup failure was not established. This transport evidence does not change either false speech readiness capability or imply microphone/booking acceptance.

## Engine and bundled-asset license inventory

Installed distribution metadata and upstream sources were checked October 28. Hashes for the full transitive graph are in each lock; this table records principal engine obligations and does not characterize the entire stack as MIT/Apache-only.

| Component | Exact version | Observed license / primary source |
| --- | --- | --- |
| faster-whisper | 1.2.1 | MIT; [upstream](https://github.com/SYSTRAN/faster-whisper) |
| CTranslate2 | 4.8.4 | MIT; [upstream](https://github.com/OpenNMT/CTranslate2) |
| Kokoro / Misaki | 0.9.4 / 0.9.4 | Apache-2.0 metadata; [Kokoro](https://github.com/hexgrad/kokoro), [Misaki](https://github.com/hexgrad/misaki) |
| Parler-TTS | 0.2.3 | [upstream Apache-2.0 license](https://github.com/huggingface/parler-tts/blob/main/LICENSE); wheel lacks a simple license metadata field |
| descript-audiotools-unofficial | 0.7.4 | MIT metadata; [PyPI release](https://pypi.org/project/descript-audiotools-unofficial/0.7.4/) |
| Torch / torchaudio | 2.5.1 / 2.5.1 | BSD-style upstream [Torch](https://github.com/pytorch/pytorch/blob/v2.5.1/LICENSE) / [audio](https://github.com/pytorch/audio/blob/v2.5.1/LICENSE); preserve bundled third-party notices |
| Transformers | 4.46.1 | Apache-2.0 metadata; [upstream](https://github.com/huggingface/transformers) |
| en_core_web_sm | 3.8.0 | MIT metadata; [official release](https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.8.0) |
| phonemizer-fork | 3.3.2 | GPL-3.0 license text in installed wheel; [declared upstream](https://github.com/bootphon/phonemizer) |
| espeakng-loader | 0.2.4 | [wrapper MIT](https://github.com/thewh1teagle/espeakng-loader); bundled eSpeak-NG remains governed by [its GPL license](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING), not the wrapper's MIT license |
| Pipecat | 1.14.0 | BSD-2-Clause metadata; [upstream](https://github.com/pipecat-ai/pipecat) |
| LiveKit Python / API | 1.2.0 / 1.2.2 | Apache-2.0 metadata; [upstream](https://github.com/livekit/python-sdks) |
| ONNX Runtime | 1.31.1 | MIT metadata; [upstream](https://github.com/microsoft/onnxruntime) |
| soxr | 1.0.0 | LGPL-2.1-or-later metadata; [upstream](https://github.com/dofuuz/python-soxr) |
| psutil | 7.2.2 | BSD-3-Clause metadata; [upstream](https://github.com/giampaolo/psutil) |
| httpx | 0.28.1 | BSD-3-Clause metadata; [upstream](https://github.com/encode/httpx) |

The Silero VAD ONNX bundled inside the pinned Pipecat wheel has SHA256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`; [Silero's upstream model/code license is MIT](https://github.com/snakers4/silero-vad/blob/master/LICENSE). The worker does not fetch a moving Torch Hub revision.

Retain upstream license/copyright/NOTICE text, BSD conditions/disclaimers and applicable GPL/LGPL source/redistribution obligations. GPL/LGPL transitive engines are present; no blanket permissive-license or production redistribution clearance is asserted. Neither virtual environments, upstream wheels/shared libraries nor weights are published in this source repository. Existing hosted NIM terms and model-production review remain separate in `licenses.md`.


The final native control/process suite has 32 passing tests. Gate evidence is now fingerprinted against both exact environment locks, current model revisions and frozen voice configuration; stale/missing fingerprints fail closed. The loopback CLI supports `--language en` and `--language ur`; neither language was rerun after the owner chose blocked acceptance. The current ASR implementation deliberately retains its tested 1024 MiB cap; another host/profile requires an explicit reviewed resource-budget change before attempting acceptance, rather than assuming more free RAM alone clears that cap.
