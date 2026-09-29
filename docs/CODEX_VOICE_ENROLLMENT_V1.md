# RVE-01 — Voice-only enrollment to a playable personalized agent

Status: implementation work order, not an implemented feature or a live-test report.
Owner: Mohammad. Prepared 29 September 2026.

## 1. Outcome, not another collection interface

The user must be able to sign in, consent, allow the microphone, talk naturally for a target 20–30 minutes, then converse with a new AI agent using a real clone of their voice and a demonstrated approximation of their dialect, phrasing and response decisions. They correct it by speaking. They never type transcripts, reasoning annotations, prompts, or provider settings and never manually export audio during enrollment. Administrative account setup is separate.

A 20–30-minute session is a target, not a voice-quality or processing-time guarantee. Clean contributor speech time is different from elapsed session time. Ask for extra speech or show verification pending when necessary. Never silently substitute a stock voice or replay enrollment audio as the generated clone. Never promise a perfect replica of a person's judgment.

Do not redesign the existing forms and call this complete. Implement a connected code path including voice capture, automated processing, actual cloning and in-browser agent testing.

## 2. Source and operating context

Repository: `m7md3thamneh97-cmyk/Ranin`.
Implementation branch: `work/voice-enrollment-v1`.
Runtime source baseline: `8c62b29b351da1531b2f82aef06f72fcef098e8c` from `deploy/render-staging`.
`main` is older; do not build from it by accident.
Existing Render service: `srv-dau047rncjis73a8fka0`, workspace `tea-datv577lk1mc73d35ql0`.
Existing app URL: https://raneen-teaching-studio-staging.onrender.com
Existing stack: FastAPI/Python, browser JavaScript, SQLite and PCM WAV on a persistent disk, Docker. The previous deployment used a 1 GB disk at `/var/data`; independently verify capacity before live enrollment. No hosting changes are authorized by this work order.

Read `AGENTS.md`, `docs/TEACHING_V02.md`, `docs/RENDER_DEPLOYMENT.md`, `studio/app.py`, `studio/teaching.py`, `studio/runtime.py`, `serve.py`, browser recording code, and the entire tests directory.

The existing implementation provides owner-only access, consent-scoped collection, manual examples, reviewed prompt packs, and unverified provider adapter paths. It does not yet provide live interviewing, cloning, employee enrollment or embedded personalized voice calls. Historical test counts vary because different subsets ran; rerun the complete suite instead of relying on the counts.

## 3. Execution and review boundary

Implement in this draft PR/work branch, leaving hosted `deploy/render-staging` unchanged. A child branch is acceptable if clearly reported and based on this branch. Never force-push. Return a reviewable implementation, not just a plan. Keep an ordered progress checklist in the PR so interrupted runs can continue without restarting completed work.

The first coding assignment is **Gate A**, the owner-only end-to-end implementation below. Do not bundle employee rollout, billing, CRM or production phone integrations into it. Do not merge, deploy, create paid resources, modify existing Sura, or use real private recordings. Live provider calls require a separately approved staging run with keys already in hosting secrets, contributor consent and a spending allowance.

Mocked tests establish code behavior, not actual voice cloning. If no live credentials or microphone are available, finish the adapters and reproducible test harness, report `Gate A complete / Gate B unverified`, and stop before external mutations. Do not set a user-facing Ready status based on mocked results or missing credentials.

## 4. Target architecture

Retain the existing stack. Add small modules for enrollment sessions, audio chunks, provider adapters, job processing, persona artifacts and preview calls.

### Spoken interviewer
- Default candidate: OpenAI Realtime over browser WebRTC, behind an adapter. Verify current session/auth/tool-event schemas and available model IDs before implementing. Do not assume that existing model aliases such as `gpt-transcribe` are valid.
- Private credentials remain server-side. Browser access must be scoped and short-lived where supported. Do not expose the owner login credential to a voice provider. Lock session configuration and authorization on the backend rather than trusting a client-supplied profile or tool result.
- The interviewer first establishes the contributor's natural language/dialect by spoken confirmation, then alternates natural discussion, customer role-play, relevant follow-ups and counterfactuals. The interviewer should not dictate all ideal answers or contaminate the evidence with its own suggestions.
- Confirm uncertain numbers, names, negations and extracted preferences aloud; preserve verbatim dialect. Never turn an unverified draft into approved training data just because an LLM produced it.
- Support pause, resume, repeat, corrections, and interruption without losing acknowledged state. Distinguish customer-facing demonstrations from the contributor's later teaching commentary.
- Use a production 20–30-minute target and a clearly labeled short developer test scenario. The shortened test is not evidence of long-session reliability.

### Audio and evidence capture
- Capture contributor microphone audio separately from interviewer output. Never upload a mixed call recording for cloning. Preserve available source quality and actual browser sample/processing metadata; calling a file WAV does not reverse earlier lossy encoding.
- Stream bounded chunks with sequence numbers, checksums and acknowledgements. Reconnect/resume must deduplicate uploads and preserve ordering. Do not hold the full session in browser memory or increase every API body limit to fit a 30-minute upload.
- Relate exact response spans, transcripts, interview context, confirmations and corrections. Speaker/consent IDs are internal evidence keys, not public URLs.
- Check clipping, silence, signal quality and speaker contamination conservatively. Flag uncertain samples instead of fabricating quality scores. Track effective clean speech time and disk quotas before accepting more data.

### Actual voice clone
- Default candidate: ElevenLabs Instant Voice Cloning API, not Professional Voice Cloning as the immediate path. Verify formats, limits, account eligibility and current terms in official docs.
- Select a provider-appropriate clean subset; do not automatically send the entire conversation. Selection must be reproducible and tied to the consenting speaker.
- Implement create -> verification/processing status -> synthesized preview -> artifact persistence. Handle `requires_verification` without bypasses. A provider voice ID alone is not a playable result.
- Synthesize at least one fresh utterance absent from enrollment. Arabic preview cases must include questions, numbers and corrections. Do not regenerate a clone on every page load or every behavior correction.
- Model/provider changes must not silently replace the target voice. Display failures, verification requirements and insufficient-quality conditions truthfully.

### Response personalization
- Build a versioned, evidence-backed profile: dialect, phrasing, turn length, demonstrated responses, decision cues, change conditions and explicitly spoken preferences.
- Distinguish contributor-confirmed evidence from model inference and uncovered situations. Avoid inferring sensitive attributes or unrelated personality labels.
- Use reviewed in-context demonstrations initially, with bounded context selection behind a retrieval interface. Do not pretend to weight-train a foundation model. Do not require fine-tuning just to finish enrollment.
- Example prices/inventory are fictional; keep business facts and action authorization outside the persona. Corrections to caller budget or intent must update conversation state.
- Compilation cannot silently truncate an oversize session. Use a traceable bounded selection for the first preview and surface unsupported capacity honestly. Scalable retrieval can be expanded in the next gate.

### Playable personalized agent
- Use Vapi as the initial preview runtime with the newly created ElevenLabs voice and the compiled response profile. Create a NEW isolated test assistant, not a patch to Sura. Browser voice testing must be embedded in Raneen.
- Verify how the Vapi account accesses the private cloned voice; having a `voice_id` is not sufficient evidence. Never publish the voice globally as an access workaround.
- Keep a Raneen-owned interface for profile/version resolution. An inline bounded profile is acceptable for the first preview; production-scale dynamic retrieval/custom-model streaming is a subsequent extension, not a prerequisite for testing one person.
- Authenticate call creation and enforce duration/concurrency/budget controls on the server. Do not rely on a UI-only time limit or unrestricted public credential as a spending guard.
- Track whether spoken corrections have been confirmed, create a new behavior version, and let the contributor retest without creating another clone. Bind each preview to a specific voice and profile version.
- No PSTN calls, phone-number assignments, CRM/inventory writes, inherited production webhooks, transfers or business actions. Identify the agent as AI using a licensed/consented synthetic voice.

## 5. Consent, jobs and operational controls

- Add a new explicit enrollment consent version for recording, transcription/external processing, private voice cloning and private agent preview. Existing research permission does not silently grant these scopes. Commercial use requires a separate approval. Provide a visible stop/revoke path and safe accessible confirmation controls.
- One enrollment approval can cover its disclosed processing sequence; do not require technical consent popups on every turn. Backend workers still recheck authorization and consent before each provider job.
- Maintain durable job states such as collecting, processing, verification_required, preview_ready, failed, cancel_requested and revoked. Track provider operation IDs and manifests. Use atomic claims/leases so a restart cannot duplicate jobs. No fire-and-forget request-local task is the only record of a paid operation.
- Reconcile ambiguous create timeouts before retry; do not assume a network failure means the provider created nothing. Backoff only for operations known to be safe to repeat. Signed/authenticated callbacks or bounded polling must validate session ownership.
- Revocation stops local processing/playback immediately and enqueues scoped deletion of this enrollment's provider artifacts. Show pending/failed external deletion; never claim that downloads, backups or model weights were universally erased.
- Preserve owner access, origin restrictions and private audio APIs. Gate new behavior off by default, for example with `RANEEN_VOICE_ENROLLMENT_ENABLED=0` until a release operator approves activation.
- Keep credentials out of prompts, logs, source, fixtures and browser storage. Temporary audio objects must be private. Add request/job/duration/storage limits and clear cost estimates; an operation counter is not a dollar spending cap.
- On the existing small host, keep concurrency conservative and memory bounded. A lightweight durable SQLite-backed job runner is acceptable for the first owner test if crash recovery works. Do not provision Redis, new servers or storage upgrades without approval. Document when larger deployment capacity is actually needed.

## 6. Three delivery gates

### Gate A — First coding assignment: connected owner-only path
Implement the complete path behind a feature flag, not disconnected placeholders:
spoken interview -> isolated chunks -> draft/confirmed response evidence -> real provider clone adapter -> synthesized fresh preview -> new personalized Vapi test assistant -> embedded browser voice test -> spoken correction/new behavior version.

Adapters must have real documented request handling plus mocked contract tests. A missing key produces an explicit not-configured state; never mark a clone ready. Include a single setup/check command or screen for the operator. Do not stop at another upload form, export button, or diagram.

Required automated tests: all existing suites; auth/consent gates; chunk ordering/resume/duplicate requests; multi-speaker/interviewer exclusion decisions; preserved Arabic text; cancellation and revocation; provider verification pending; voice/assistant timeout reconciliation; restart recovery; bounded memory/storage; no secret/recording leakage; no stock-voice success fallback; same voice reused for a behavior-only correction. Browser automation should cover permission denial, disconnect/reconnect and the full flow with labeled synthetic fixtures. Report fake-microphone tests separately from a physical microphone.

### Gate B — Controlled real-owner acceptance, after review and operator approval
Using Mohammad's own newly consented audio, funded provider accounts and an agreed allowance: complete a real 20–30-minute Arabic enrollment; synthesize novel questions/numbers; run the new agent inside the app; verbally correct its wording/decision and observe the next version; restart/resume and verify artifacts; confirm original Sura is unchanged. Record provider IDs, source revision, test timestamps, durations and private evidence locations without exposing secrets or voice samples in Git.

Human/native-listener checks must separately judge voice identity, dialect/wording, pronunciation/question melody, critical-detail retention and interruption recovery. Compare held-out cases to an untaught baseline using the same model/voice. Set acceptance criteria before looking at results. Engineering passing does not imply that the Arabic quality is accepted.

### Gate C — Employee-ready infrastructure
Separate invited contributor accounts; role isolation; reviewed employee licensing/retention policy; no shared owner tokens; multiple isolated enrollments; meaningful storage and cost controls; tested backup/restore; external deletion/revocation tracking; operational diagnostics; version promotion/rollback; native-listener evaluation across representative dialects. No claims of employee readiness until these pass. This is follow-up scope, not permission to weaken the owner-only boundary in Gate A.

## 7. Deliverables for the first Codex task

1. Inspect code and run a complete baseline. Record existing failures without removing tests.
2. Implement Gate A on this work branch/PR. Continue to code after the brief inspection; do not produce another planning-only result.
3. Add reproducible tests, migrations, provider contracts and a concise operator activation checklist. Keep the existing app functional when the feature is disabled.
4. Report exact files/commits, test commands/results, mocked vs live evidence, unresolved issues, and the smallest next step for Gate B. Mark this draft ready for review only when the implementation is reviewable; never merge it yourself.
5. If interrupted, checkpoint code and update the PR checklist. Do not describe unimplemented or untested paths as complete.

## 8. Administrator-only setup and reference sources

Later runtime credentials: `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `VAPI_API_KEY`; any Vapi credential linking or browser-scoped authorization needs must be verified and listed once. Do not require employees to obtain API keys. Existing provider-key presence and permissions have not been established by this work order.

Codex coding runs can use synthetic fixtures without paid API access. Codex Cloud environment secrets may only be available during setup; do not put production keys there or persist them to bypass that boundary. Live acceptance should use a separately approved staging environment's secret storage.

Verify current documentation before using exact provider models, endpoints or SDK versions:
- https://developers.openai.com/codex/cloud/environments
- https://developers.openai.com/codex/guides/agents-md
- https://developers.openai.com/api/docs/guides/realtime-webrtc
- https://developers.openai.com/api/docs/guides/realtime-server-controls
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://elevenlabs.io/docs/api-reference/voices/ivc/create
- https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning/instant-voice-cloning
- https://docs.vapi.ai/customization/custom-voices/elevenlabs
- https://docs.vapi.ai/quickstart/web
- https://docs.vapi.ai/api-reference/assistants/create

These references document building blocks, not measured Arabic quality or proof that our account integration works.
