# Engineering rules

- Work on one agreed phase at a time. Stop at its verification checkpoint and give the owner exact testing instructions. Do not start the next phase without their direction.
- Read the governing specifications before implementation. v0.2 overrides conflicting v0.1 requirements; missing v0.1 requirements must not be invented silently.
- Keep API, voice workers, domain logic, and provider adapters separate. Do not create services or abstractions until a concrete use requires them.
- Use Python 3.12, typed request/response contracts, validated configuration, migrations, structured logging, and reproducible dependency locks when implementation begins.
- Enforce tenant ownership, verification, scheduling policy, idempotency, and concurrency in code/database constraints, never solely through prompts.
- Generate spoken confirmations involving names, numbers, times, and dates using bilingual code templates.
- Never log credentials, raw patient audio, or identifying transcripts by default. Public fixtures must be synthetic; local recordings/results containing personal data stay untracked.
- Test critical behavior: slot races, authorization, retries, tool envelopes, Urdu date ambiguity, number normalization, script handling, and language switching.
- Record exact model/voice revisions and licenses in `docs/licenses.md` before adoption. Never label an unverified artifact commercially usable.
- Keep secrets server-side and use environment examples containing placeholders only.
- Review dependency stability and licenses against primary sources at installation time. Do not equate newest with compatible.
- Use the workflow in CONTRIBUTING.md. Report checks actually run and distinguish preparation from measured feasibility.
