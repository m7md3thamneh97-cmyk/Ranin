# Raneen engineering instructions

## Product objective
Build a voice-only enrollment system: a consenting person talks for a target 20–30 minutes, then tests a new AI agent using a real clone of their voice and an evidence-backed approximation of their response style. No typing training transcripts, decision annotations, prompts, or manual audio exports in the ordinary contributor journey. Authentication, consent, and accessibility controls are allowed. A form redesign or a prompt export is not completion.

Read `docs/CODEX_VOICE_ENROLLMENT_V1.md` before implementation. Existing `docs/TEACHING_V02.md` describes the previous implementation, not the final target.

## Repository and release boundary
- Work only in `m7md3thamneh97-cmyk/Ranin`. Do not touch Clerkit repositories.
- This work branch starts from Render code baseline `8c62b29b351da1531b2f82aef06f72fcef098e8c`, not the older `main` branch.
- Keep implementation in this PR/work branch or a clearly identified child branch. No force pushes. Do not merge or push to `main` or `deploy/render-staging`.
- No Render/Railway resource changes, paid plan changes, deployment triggers, credential rotation, production assistant changes, phone-number assignments, or outbound calls without a separate operator approval.
- Reuse the current Python/FastAPI, browser-JavaScript, Docker and persistent-storage foundation. Do not replatform for convenience. Preserve existing data and owner access. Schema changes must be additive, versioned, and tested against an existing database.

## Safety and data requirements
- Never commit, echo, log, embed in browser bundles, or request in chat a private API key, login token, real recording, enrollment transcript, or personally identifying test fixture. Use synthetic fixtures.
- Production provider credentials stay in hosting secret storage. Do not smuggle Codex setup secrets into files, caches, normal environment variables, or shell profiles for the agent phase.
- New provider integrations are off by default. Unit and CI tests mock outbound calls. Creating a real clone or voice call needs an approved staging test with the contributor's consent and a spending allowance.
- Check authorization and unrevoked consent on every enrollment resource, processing job, artifact and playback path. A research-only checkbox is not a commercial voice license.
- Do not bypass provider speaker verification. Do not mix different people's audio into a clone. Do not include the AI interviewer's audio in speaker training samples.
- A cloned agent must be identified as AI, not falsely claim to be the human contributor. Separate demonstrated behavior from current business facts and higher-priority business rules.
- Provider calls need timeouts, durable operation tracking and reconciliation for uncertain outcomes; never blindly repeat clone/assistant creation after a timeout.

## Engineering and verification
- Read actual code and run the entire existing suite before changes. Record failures honestly; previous test counts are not current evidence.
- Start with `python -m pip install -r requirements-dev.txt`, `python -m pytest -q`, and `node tests/wav_encoder.mjs`. Inspect browser-test prerequisites rather than guessing commands.
- Prefer small modules and typed contracts; avoid growing `studio/app.py` into a monolith.
- Validate current provider endpoints, model identifiers, browser capabilities and voice compatibility against official documentation. Unknown credentials or model access are blockers to a live check, not to implementing a tested adapter.
- Distinguish four results: implemented; passes mocked tests; passes real provider test; accepted by a human in a real Arabic microphone conversation. Never substitute one for another.
- Deliver code, tests, a runnable setup and concise evidence. Do not stop with another architecture dossier. Checkpoint partial code safely if the session is exhausted, with exact remaining tasks.

## Code Review Rules
- Flag any cross-user access, consent bypass, credential leak, destructive migration, uncontrolled spending or duplicate external resource creation.
- Flag claims of a cloned voice that actually use a stock voice, replay enrollment audio, or only return a provider ID without playable synthesized speech.
- Flag training-flow text entry requirements, claims that prompt adaptation is weight training, or success claims based only on mocks.
- Keep business integrations, commercial promotion and other repositories outside the initial enrollment PR.
