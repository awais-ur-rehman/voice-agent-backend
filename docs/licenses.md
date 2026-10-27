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
