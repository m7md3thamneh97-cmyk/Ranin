# Raneen voice enrollment — architecture v1

Design date: 2026-09-29. Status: implementation contract, NOT a delivered feature or quality certificate.

This elaborates `CODEX_VOICE_ENROLLMENT_V1.md` and preserves its Gate A/B/C authorization boundaries. It does not authorize a deployment, provider purchase, real recording, credential change, or employee rollout. An implementation already in progress must continue from its checkpoint, not restart because this document was added. Reconcile deviations during review.

## 1. Product contract

A consenting person signs in, starts an interview, speaks naturally for a target 20–30 minutes, and then talks inside the same application to an AI using a genuine clone of that person's voice and evidence-backed response style. Corrections are spoken. Ordinary enrollment requires no transcript editing, reasoning forms, prompt authoring, provider-dashboard visits, or manual exports. Sign-in, consent, microphone permission, stop controls and accessibility alternatives are allowed.

Elapsed enrollment time is not clean speech duration, cloning latency, or a guarantee of similarity. The valid outcomes are a playable candidate, more speech requested, provider verification required, an actionable failure, or revocation. Never hide a failure with a stock voice or a replay of enrollment audio.

Three products of enrollment remain distinct:

1. Voice version: provider voice ID, selected speaker-only sample manifest, voice-model configuration and generated speech evidence.
2. Behavior version: confirmed demonstrations, language/dialect and response preferences, decision conditions and unsupported areas, each linked to source turns.
3. Agent version: a binding of one voice version, one behavior version, a business-policy version, recognition/turn-taking settings and evaluation evidence.

Changing a response preference does not create another voice clone. A voice clone does not itself learn commercial facts, authorize actions, or reproduce a person's entire judgment.

## 2. Existing foundation and explicit gaps

Inspected source: runtime baseline `8c62b29b351da1531b2f82aef06f72fcef098e8c`; work-order head `d19b6764ce561189ef195a5d28943b90f8ebcec2` before this design was added. PR #1 targets `deploy/render-staging`; `main` is older.

Keep Python/FastAPI, the browser JavaScript frontend, Docker and Render. `studio/runtime.py` composes the guarded foundation and teaching modules. Existing consent, review, database, export and owner-login behavior must remain functional when enrollment is disabled.

The existing `Recorder` accumulates Float32 chunks until Stop and requests echo cancellation off. It is a short-recording component, not a safe long-call capture pipeline. Existing upload and WAV limits must not simply be enlarged to accept a 30-minute blob. The current Vapi test creation reuses a template's stock/custom voice and emits no browser call. It is not the new clone assembly path. The current behavior compiler has a 32-item/28,000-character limit; it cannot hold an unlimited interview.

No current account/model/voice entitlement is inferred from a configured environment variable. Historical test totals are not a current baseline.

## 3. Architecture decisions

Use a modular monolith initially: one codebase, explicit domain modules, one web process and one bounded durable-job runner in the same single-instance deployment. Realtime network I/O must not block the event loop; CPU-intensive audio conversion uses bounded subprocess/thread work. Durable work lives in the database, not only in FastAPI BackgroundTasks or an in-memory queue.

Gate A uses SQLite with WAL, short transactions, unique constraints and leases on the existing dedicated disk. Enforce one active owner enrollment and bounded processing concurrency. Do not provision Redis, Postgres, extra services or a vector database merely to implement the first complete path.

Before multi-instance employee rollout, move metadata/jobs to Postgres and audio to private S3-compatible object storage, then run workers separately. A Render disk cannot be assumed to be shared across services. Storage and database interfaces must permit this migration without changing the contributor journey. The actual migration and spending require their own review/approval.

No framework rewrite is required. New browser modules can coexist with the legacy guided and advanced pages. If a provider SDK needs bundling, introduce a minimal reproducible frontend build rather than loading arbitrary third-party scripts at runtime.

### Logical topology

```text
Contributor browser
  |-- authenticated control + progress -------------------> Raneen FastAPI
  |-- bounded speaker-only audio chunks ------------------> private artifact store
  |-- live interview audio via WebRTC --------------------> OpenAI Realtime
  |                                                        ^ server control/events
  |                                                        | (authenticated sideband)
  |-- personalized preview audio -------------------------> Vapi web call
                                                           | recognition / turn-taking
                                                           | Raneen response runtime
                                                           | ElevenLabs cloned TTS

Raneen FastAPI + durable jobs
  |-- consent and session state
  |-- voice sample selection -> ElevenLabs clone -> novel preview audio
  |-- evidence extraction -> spoken confirmation -> behavior version
  |-- version binding -> isolated Vapi assistant/call -> spoken corrections
  `-- usage, audit, cancellation, reconciliation and deletion
```

These are logical boundaries, not a microservices deployment proposal.

## 4. Provider decisions and contracts

### Interviewer

Default adapter: OpenAI Realtime with browser WebRTC and a server-owned control connection. Use currently documented session creation/authentication/events; verify available model IDs instead of inheriting an unverified alias. Keep account API keys and private tool execution server-side. The server authenticates the person, authorizes consent and reserves a session allowance before creating any provider session.

The browser receives only the necessary short-lived session capability/SDP, not a private API key. Session IDs from the browser never authorize access. Register the provider session against the authenticated enrollment. Tool requests are schema-validated commands checked against server state; the model cannot approve consent, choose another person's artifacts, publish an agent, or raise its own budget.

Candidate interviewer tools: propose_situation, record_turn_candidate, request_spoken_confirmation, mark_correction, request_capture_pause, and request_session_finish. Commands cannot directly create paid clones. The orchestrator determines when downstream jobs become eligible.

Use explicit interview modes: calibration, natural discussion, customer role-play, teaching reflection, counterfactual, confirmation and evaluation. Do not teach the person what to answer and then call the echoed answer independent evidence. Keep customer-facing responses separate from reflections. Do not infer unrelated sensitive traits or personality labels.

Spoken user controls must cover pause, continue, repeat, stop and correction. Always provide a visible Stop/Cancel control; it must work without the speech model. Provider-side session expiry/termination is part of the adapter contract, not a client-only timer. If a provider cannot enforce a required server-side limit, report that integration as not ready rather than bypassing it.

### Voice

Default enrollment adapter: ElevenLabs Instant Voice Cloning. The API returns a voice ID and whether verification is required. Do not substitute Professional Voice Cloning for the immediate path; it has different data and speaker-verification requirements and processing latency.

Select approximately 1–2 minutes of consistent, clean contributor speech from the larger session; follow the provider's actual limits. Rich and varied interview evidence is useful for behavior, but the acoustic cloning subset must avoid wildly inconsistent delivery. Keep natural questions in a consistent style. Excess audio is not automatically better.

API surface: create_voice(manifest), inspect_voice(provider_id), synthesize_preview(provider_id, novel_text, model_config), delete_voice(provider_id). Preserve request hashes and provider resource identifiers. A returned ID alone is not `voice_ready`: synthesis must return decodable nonempty new audio with recorded provenance. Verification-required and unknown-result states prevent assistant assembly.

Do not assume a WAV master improves provider output; master audio is retained for provenance and reproducible processing. Generate provider-supported derivative files as required. Do not apply irreversible cleanup to masters, or run aggressive denoising by default. Provider voice weights remain provider-hosted; storing a voice ID is not owning/exporting a trained model checkpoint.

### Personalized calls

Default runtime: Vapi web calls with the actual newly created ElevenLabs voice. Verify private-voice access through the owner's linked provider account once; do not publish a clone globally as an access workaround. Contributors never need provider accounts/API keys for the default enrollment flow; provider-mandated verification is an honest exception.

Create calls server-side after authorization, quota checks and a version binding. Return only the provider's documented call-scoped join capability. Do not expose an unrestricted organization credential or rely on a browser Stop button for billing control. Verify SDK compatibility with a server-created call; inability to join safely is a failed acceptance condition, not a reason to use an unrestricted public key. The browser must show call end and recover from rejected permissions/disconnection.

For Gate A, an immutable bounded inline behavior pack is acceptable for the first isolated preview, as the original work order permits. The complete infrastructure uses a Raneen-owned authenticated streaming response endpoint with bounded relevant-example selection; provider-specific request schemas stay in an adapter. Do not block the first audible result by building a large retrieval platform first. If inline snapshots are used, mark that limitation and do not claim immediate removal of already copied prompts.

Original Sura, PSTN numbers, transfers, inventory/CRM tools and production webhooks are never inherited. Only explicitly approved new test infrastructure is used. Raw provider recording defaults, transcripts and retention must be inspected independently; `recordingEnabled=false` is not proof of zero provider data retention.

## 5. Session and job state machines

Do not collapse enrollment state and provider job state into one ambiguous status.

Enrollment:
`draft -> consented -> calibrating -> interviewing <-> paused -> finalizing -> candidate_ready -> previewing -> accepted_for_internal_test`

Alternative states: `needs_more_audio`, `verification_required`, `recoverable_failure`, `cancelled`, `revoked`. A candidate may be prepared while interviewing; ready is only displayed after both required artifacts and their binding pass checks. Acceptance for internal testing is not commercial approval.

Job:
`queued -> leased -> succeeded | retry_wait | verification_required | outcome_unknown | failed | cancelled`

Operation:
`reserved -> submitting -> confirmed | known_failed | outcome_unknown -> reconciled | cleanup_pending`

A timed-out external create may already have succeeded. Never retry that create automatically without documented provider idempotency or a conclusive reconciliation. Record the external intent before dispatch, use a stable operation key, and retain unknown outcomes across restarts. Do not describe distributed side effects as exactly-once when a provider gives no such guarantee.

Worker claims use atomic updates with a lease generation. A stale worker cannot overwrite newer job state. Long jobs renew leases; lost leases with a possible external side effect move to reconciliation instead of resubmission. No transaction stays open across a network call. Cancellation is cooperative; both before dispatch and after completion, check current consent/grant epoch. If revocation races with creation, register the late artifact and schedule cleanup rather than serving it.

## 6. Audio ingestion and interruption handling

Use a new streaming capture module. Default transport chunks: about four seconds of mono 16-bit PCM with actual sample-rate metadata, sequence number, capture epoch, start/end monotonic timestamps and SHA-256. Enforce a separate chunk endpoint maximum (1 MiB initially), allowed audio shapes and a session-level byte/duration budget. Actual device capture may vary; reject unsupported formats explicitly rather than relabeling their sample rate.

Unique key: `(enrollment_id, stream_epoch, sequence)`. The same key/hash is an idempotent retry; the same key/different hash is a conflict. Store to a temporary file, verify checksum/format and size, atomically promote, record committed metadata, then acknowledge. Use filesystem sync where supported before acknowledging durable local storage. DB failures leave recoverable orphan files for a scoped sweeper, never an acknowledged missing chunk. Finalization verifies the declared terminal sequence and missing ranges.

Keep at most a bounded unacknowledged browser backlog (for example 30 seconds / 8 MiB); pause capture visibly before exceeding it. Do not retain the entire enrollment in a JavaScript array. Reconnect resumes from the server's acknowledgement ledger. An abrupt tab close may lose an unacknowledged tail; disclose and rerecord only that portion. Do not promise lossless capture of audio never acknowledged by the server. Local persistent buffering is optional, consented, bounded and cleared after acknowledgement; no hidden indefinite local speech cache.

Never mix interviewer output into the recording graph. Headphones are preferred for enrollment. Physical speaker echo can still enter microphone-only capture, so track playback overlap and exclude questionable spans. Speakerphone mode may require echo cancellation and separate clean calibration prompts; record actual processing settings. Do not claim single-speaker detection from an RMS/clipping heuristic. Unknown speaker contamination -> request another sample.

Tie live provider events, playback and capture timelines with capture epochs and offsets. Track output generated versus actually played. When a person interrupts, cancel stale output and make later context reflect what they heard; preserve corrections rather than restarting the questionnaire. No overlapping response generation from stale turn IDs. Final ASR text is still a hypothesis, not the authoritative intended number or negation.

## 7. Behavior learning and versioning

The data hierarchy is `audio span -> verbatim transcript -> contextual demonstration -> proposed rule/preference -> spoken confirmation -> approved behavior version`. Every transformation records its source IDs and extractor/model version. Maintain raw Arabic separately from any normalized search representation. Normalization must not rewrite the response or silently remove negation, names, English insertions or numbers.

Initial evidence states: `draft`, `confirmed`, `rejected`, `superseded`. Extraction confidence, when available, is advisory; no fabricated probability and no confidence threshold alone may create confirmation. Spoken confirmation requires a specific pending candidate and an eligible human turn; an interviewer saying "yes" cannot approve itself.

Each evidence item records scenario family, mode, transcript/audio spans, response, chosen action, decision cue, changed-condition example, explicit preferences, confirmation provenance and validity scope. Contradictory instructions trigger a short spoken clarification. A correction usually creates a contextual exception, not a global replacement.

Select the next scenario to fill explicit coverage gaps: answer a direct question, clarify ambiguity, retain numbers/negation, accept correction, recover from interruption, change objective, handle uncertainty, hand off and switch language. Allocate a small held-out evaluation set by scenario family before collecting teaching answers. Do not silently reuse evaluation turns as training; if a correction is promoted, retire that test and generate a fresh held-out case.

Policy precedence:
`consent/access restrictions -> business safety and action policy -> current verified business facts -> current conversation state -> confirmed speaker preferences -> relevant demonstrations -> generic style fallback`.

The persona never supplies live inventory, promises a guaranteed return, or authorizes bookings. Such facts/actions belong to separately governed business integrations, outside enrollment v1.

Compile a versioned profile plus a deterministic bounded evidence selection; include source IDs and omitted coverage in the manifest. A normalized lexical/family selector is sufficient initially. Later semantic retrieval requires its own accuracy tests. Do not concatenate the entire interview or silently truncate. At runtime use the smallest relevant examples that fit a token budget. A correction creates a new behavior version, retains the voice version, invalidates incompatible caches and binds subsequent previews explicitly. No uncontrolled real-time learning from arbitrary customer calls.

Spoken delivery has its own versioned settings: tested pronunciation aliases/diacritics, clause boundaries, speech rate and provider-supported expressive controls. Do not invent unsupported SSML or promise that a prompt fixes question prosody. Test complete short questions versus streaming before changing global latency settings.

## 8. Data model and interfaces

Reuse users/profiles where safe. Add versioned tables rather than destructive replacements:

| Entity | Key fields / invariants |
|---|---|
| enrollment_sessions | id, owner_id, profile_id, consent_grant_id, phase, version, capture_epoch, coverage, timestamps |
| consent_grants | subject, scopes, terms_version, approved_at, revoked_at, epoch; never silently upgrade research consent |
| audio_chunks | enrollment, epoch, sequence, object_key, hash, bytes, rate, frames, timing, quality flags; unique sequence key |
| enrollment_turns | provider event ID, mode, role, transcript, playback boundary, chunk-span references; event deduplication |
| response_evidence | source turns/spans, contextual response and rules, confirmation turn, status, scenario family/split |
| jobs / provider_operations | type, stable operation key, lease/fencing generation, request digest, external ID, state, attempt count, error code |
| voice_versions | profile, provider/account binding, voice_id, sample-manifest hash, verification, preview artifact, status |
| behavior_versions | profile, schema/model version, confirmed source IDs, profile JSON, digest, omissions, status |
| agent_versions | immutable voice+behavior+policy+recognition binding, external assistant ID, candidate/accepted/revoked state |
| preview_sessions | owner, agent version, provider call ID, grant epoch, expiry, duration/cost reservation, status |
| usage_ledger / deletion_jobs | reserved/observed usage and pricing version; resource-specific cleanup outcomes |

New API routes are authenticated and feature-flagged. Proposed contracts, not claims of existing endpoints:

- POST `/api/enrollments`: authorized owner/profile and explicit scoped consent; idempotency key.
- POST `/api/enrollments/{id}/interview`: create/resume a server-authorized realtime session.
- PUT `/api/enrollments/{id}/chunks/{epoch}/{seq}`: bounded binary upload, checksum, shape and timestamps.
- GET `/api/enrollments/{id}`: persisted status, durable acknowledgement boundary, missing ranges, safe progress.
- POST `/api/enrollments/{id}/control`: pause/resume/finalize/cancel commands with expected state version.
- GET `/api/enrollments/{id}/events`: authenticated replayable progress (fetch streaming or bounded polling); no bearer token in query strings.
- POST `/api/enrollments/{id}/previews`: authorized call creation for the bound candidate.
- POST `/api/enrollments/{id}/revoke`: immediate local deny plus tracked external cleanup.
- Provider callbacks/control endpoints: distinct authentication, schema validation, replay protection and operation ownership; do not share the owner token.

Exact route names may differ with equivalent tested boundaries. Test mounting behind the existing middleware, which currently assumes bearer auth for `/api/`, bounded Content-Length, same origin and a restrictive CSP. Do not weaken the entire site's guards to make WebRTC work. Any cookie-auth path must include CSRF/origin and websocket-origin protections. Add only documented required browser network origins; no wildcard CSP or arbitrary URL proxy.

## 9. Security, capacity and operational acceptance

Initial mode is owner-only with `RANEEN_VOICE_ENROLLMENT_ENABLED=0` by default. One scoped consent can cover disclosed enrollment processing; workers revalidate it on every operation. No mic capture before permission/consent. No real recordings, credentials or transcripts in Git, Codex fixtures, CI artifacts or ordinary logs. Synthetic samples must not imitate unconsenting people. The agent identifies itself as AI.

Revocation immediately denies new calls, job submissions and application playback, attempts termination of active sessions, and tracks external deletion. Already transmitted audio or downloaded files cannot be remotely erased by assertion. If a provider deletion fails, surface cleanup pending and retry safely. Backup tombstones must be reapplied on restoration. Employee rollout requires invitation identities, nonshared sessions, review of licensing/retention and verified provider permissions; it is not authorized merely by a model's self-reported consent.

Capacity example (calculation, not measurement): 48,000 samples/sec * 2 bytes * 1,800 sec = 172.8 MB (about 164.8 MiB) for one uncompressed mono stream before transcripts, derived files, previews and backups. The present 1 GB disk is an owner-test resource, not an employee corpus. Preflight/reserve capacity, enforce a session cap, reject safely before disk exhaustion, and show recorded bytes/clean speech duration. Preserve acknowledged recordings rather than automatically deleting them to make space.

Resource limits must cover active enrollment duration, call duration, provider operations, tokens/TTS characters, simultaneous jobs and storage. A daily request counter alone cannot cap realtime costs. Use an operator-approved session allowance and conservative cost reservations; reconcile actual vendor usage, label delayed/estimated billing and stop authorizing work at the ceiling. Account-level provider caps remain an additional protection. No live dollar allowance is granted by this document.

Observe metadata, not voice content: correlation IDs, job phase, provider request IDs, retries, outcome_unknown counts, captured/acknowledged seconds, queue depth, latency percentiles, storage and estimated/actual usage. Distinguish provider availability, code correctness and native-listener quality. Run backup/restore checks before valuable data collection. A live Render process is not this acceptance test.

## 10. Delivery and review

Do not open a new repository or rehost. Use the existing PR/work branch. Only one implementation writer at a time; a separate reviewer follows the implementation. The existing Codex invocation has an acknowledgement; do not create duplicate broad jobs.

Ordered implementation checkpoints inside Gate A:

A1. Add session/grant/job/artifact contracts, versioned migrations, strict guards and streaming capture; preserve old workflow.
A2. Connect actual interviewer, speaker-only manifests and ElevenLabs adapter to newly synthesized speech, with documented wire contracts and mocked tests.
A3. Complete automatic behavior confirmation, clone+persona binding, embedded Vapi preview and spoken correction using the same voice. Include failure/recovery paths, not only the happy path.
A4. Run complete regression and synthetic browser/long-session tests; publish exact evidence and remaining live unknowns. Checkpoint unfinished code rather than announcing full completion.

Then Gate B: separately approved real-owner enrollment and Arabic listening tests using real accounts, one controlled candidate, no production promotion. Gate C: invitation/auth/storage/worker scale, deletion/restore and representative employee trials. These are release gates, not excuses to return another form instead of a connected pipeline.

Review against `RVE_ACCEPTANCE.md`. Feature completion requires executable code. User acceptance requires real audio evidence. Production readiness additionally requires operational and consent review. No single test count establishes all three.

## 11. Primary references checked for this design

Capabilities are documented building blocks, not measured Arabic performance. Codex must confirm exact current schemas/models before implementing contracts.

- OpenAI Realtime WebRTC: https://developers.openai.com/api/docs/guides/realtime-webrtc
- OpenAI server controls: https://developers.openai.com/api/docs/guides/voice-server-controls
- ElevenLabs IVC create: https://elevenlabs.io/docs/api-reference/voices/ivc/create
- ElevenLabs sample guidance: https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning/instant-voice-cloning
- Vapi custom ElevenLabs voices: https://docs.vapi.ai/customization/custom-voices/elevenlabs
- Vapi web calls: https://docs.vapi.ai/quickstart/web
- Vapi custom LLM: https://docs.vapi.ai/customization/custom-llm/using-your-server
- Vapi server authentication: https://docs.vapi.ai/server-url/server-authentication
- Vapi data flow: https://docs.vapi.ai/security-and-privacy/data-flow
- Codex GitHub task invocation: https://developers.openai.com/codex/integrations/github/
