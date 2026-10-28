# License ledger

Project source uses MIT. The original experimental HTTP runner uses Python's standard library (Python Software Foundation license: https://docs.python.org/3/license.html). Validation dependencies are now recorded below; no model weights or voice artifact have been downloaded or approved. NVIDIA hosted API access is a service governed by NVIDIA/account/model terms, not covered by this project's MIT license.

Before adoption, record the exact artifact identifier, version/revision, upstream license URL, license, redistribution obligations, and review date. Models, model weights, engines, and individual voices need separate entries. A candidate in the spec is not a verified license entry.

## Hosted Phase 0 candidate: Nemotron 3.5 Bolt E4B

Reviewed October 27, 2026. Requested API identifier: `nvidia/nemotron-3.5-bolt-e4b`. The hosted alias does not expose an immutable model-weight revision in our request; do not represent it as a pinned BF16 or quantized artifact. No weights have been downloaded.

NVIDIA's [hosted model card](https://build.nvidia.com/nvidia/nemotron-3.5-bolt-e4b/modelcard) lists API Trial Terms for hosting and OpenMDW-1.1 for model use, with additional Apache-2.0 information. The [upstream BF16 card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16) also identifies OpenMDW-1.1 as governing and Apache-2.0 for the Gemma base. This is not an MIT/Apache-only model approval; redistribution obligations and equivalence to the project's license requirement remain to be reviewed before self-hosting/adoption. Current use is the owner-requested hosted synthetic evaluation only.

## Additional hosted candidates reviewed October 27, 2026

No weights or voices downloaded; no immutable hosted weight revisions exposed or approved. Hosted requests are governed by NVIDIA API Trial Terms. This ledger records observed published terms rather than providing a production licensing clearance.

- `minimaxai/minimax-m3.1-flash`: the [NVIDIA model card](https://build.nvidia.com/minimaxai/minimax-m3.1-flash/modelcard) expressly states research and development only. It is not approved for a production clinic deployment and is not a verified MIT/Apache-equivalent artifact. The card describes a model far beyond the planned single-GPU weight budget; hosted testing does not establish a self-hosting path.
- `meta/muse-glimmer-30b`: the [NVIDIA model card](https://build.nvidia.com/meta/muse-glimmer-30b/modelcard) lists the NVIDIA Open Model Agreement and additional Apache-2.0 base-model information. Production adoption, redistribution obligations, exact artifact license, and target GPU fit remain unreviewed.
- `google/diffusiongemma-26b-a4b-it`: the [NVIDIA model card](https://build.nvidia.com/google/diffusiongemma-26b-a4b-it/modelcard) lists the NVIDIA Open Model Agreement with additional Apache-2.0, Gemma Terms of Use, and Gemma Prohibited Use Policy information. Do not simplify this to an Apache-only clearance; exact artifact terms and redistribution obligations remain unreviewed.

## CPU validation dependencies — October 27, 2026

`requirements.lock` pins versions and artifact hashes; `pyproject.toml` records the direct dependency range. Installed distribution metadata was inspected for the versions below. This is tooling, not a model/voice license approval. Python itself remains PSF licensed.

| Package | Locked version | Installed license expression | Upstream |
| --- | --- | --- | --- |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing |
| rpds-py | 2026.9.1 | MIT | https://github.com/crate-py/rpds |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |

## Phase 1 application dependencies — October 27, 2026

The full locked runtime/development graph and published license metadata are recorded in the [Phase 1 inventory](phase-1-dependencies.md). Official PyPI metadata was checked before adoption. The PostgreSQL driver is explicitly LGPL-3.0-only, not MIT. Container image digests are pinned. Model/voice production approvals remain unchanged.

## Phase 2 runtime additions — October 28, 2026

`uv.lock` pins the Python distributions; `compose.yaml` pins the server image. Review covers the named software components, not hosted model production approval.

| Component | Adopted artifact | License and primary review sources | Redistribution notes |
| --- | --- | --- | --- |
| Valkey server | `valkey/valkey:8.1.11-alpine@sha256:2602ace2be6569bc2bbb06f873776516ab9089821525be34f4305c67eb9db02a` | BSD-3-Clause; upstream [COPYING for the 8.1 line](https://github.com/valkey-io/valkey/blob/8.1/COPYING), [official releases/downloads](https://valkey.io/download/) | Retain the Valkey and inherited Redis copyright notices, conditions and disclaimers; respect the non-endorsement condition. Container OS packages retain their own licenses. |
| valkey-py (`valkey` package) | `6.2.0` | MIT; [versioned PyPI metadata](https://pypi.org/project/valkey/6.2.0/), [official client listing](https://valkey.io/clients/), [upstream repository](https://github.com/valkey-io/valkey-py) | Retain copyright and permission notices in copies or substantial portions. The synchronous client is used for live dialogue state and holds. |
| httpx2 | `2.16.0` | BSD-3-Clause; [versioned PyPI metadata](https://pypi.org/project/httpx2/2.16.0/), [existing Phase 1 inventory](phase-1-dependencies.md) | Previously reviewed in Phase 1; now also a runtime dependency for the NIM HTTP adapter. Retain upstream notices, conditions and disclaimer; respect non-endorsement. |

The Valkey image was pulled successfully and its registry digest checked before use. Both local services require authentication. Server/driver adoption does not change the hosted Nemotron terms recorded above, approve a self-hosted weight artifact, or establish native language/voice acceptance.

## Phase 3 CPU speech artifacts — October 28, 2026

These pins authorize owner-requested local synthetic evaluation; native voice quality and production deployment approval remain pending. Store downloaded artifacts and copied upstream notices under ignored `.local/`, outside Git and application images. Runtime hashes are recorded by the download manifest. Gated access does not supersede the published license or account terms.

| Artifact | Immutable revision | Primary license source | Obligations |
| --- | --- | --- | --- |
| `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` | [model card](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo), MIT | Preserve MIT notices; converted Whisper weights are distinct from the engine package. |
| `hexgrad/Kokoro-82M` | `f3ff3571791e39611d31c381e3a41a3af07b4987` | [model card](https://huggingface.co/hexgrad/Kokoro-82M), Apache-2.0 | Preserve license, copyright and applicable NOTICE; identify modifications. Voice tensors must come from this pinned repository and retain its notices. |
| `ai4bharat/indic-parler-tts` | `660bc53c64ae83150613264111708a0465211237` | [model card](https://huggingface.co/ai4bharat/indic-parler-tts), Apache-2.0 metadata | Access accepted by owner; preserve upstream license/NOTICE and attribution. No redistribution of the downloaded model in this project. |

The isolated speech dependency graph and engine/transitive-license review are recorded in `phase-3-dependencies.md`; this entry does not characterize every dependency as permissively licensed or approve an arbitrary replacement voice/model.

Indic Parler's description tokenizer is `google/flan-t5-large` at `0613663d0d48ea86ba8cb3d7a44f0f65dc596a2a` ([upstream model card](https://huggingface.co/google/flan-t5-large), Apache-2.0 metadata). Only tokenizer/config files are used; preserve their upstream notices. This is not adoption of a separate Flan language-model inference service.

LiveKit server `v1.13.11` is pinned to registry digest `sha256:25cd902de7cacbd5f1f779f6a6ac1eae20ba8f56b0935934ce6b0ab80d1e3d20`; the [upstream LICENSE](https://github.com/livekit/livekit/blob/v1.13.11/LICENSE) is Apache-2.0. Retain upstream license/NOTICE and identify modifications. The local container provides transport only, without recording/egress. Individual OS dependencies retain their own licenses. Pipecat's pinned distribution includes its Silero ONNX VAD asset; see the speech inventory for the engine and bundled-asset notices rather than downloading an unpinned Torch Hub model.

The [Phase 3 engine inventory](phase-3-dependencies.md#engine-and-bundled-asset-license-inventory) records exact native versions and actual transitive GPL/LGPL components, including phonemizer/eSpeak-NG and soxr. Kokoro/Parler weight licenses do not make every engine dependency Apache/MIT. The bundled Silero VAD artifact is pinned by Pipecat wheel plus SHA256 and attributed to the upstream MIT-licensed model. Native environment/wheel/model redistribution is outside this source-only checkpoint; preserve all applicable upstream obligations.
