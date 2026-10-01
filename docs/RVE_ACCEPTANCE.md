# RVE acceptance contract v1

2026-09-29. Companion to `RVE_ARCHITECTURE.md` and the existing RVE-01 work order.

This is a test specification, not a test report. All numeric targets below are proposed release criteria, not observed product performance. No live provider spending, customer calls, employee enrollment or deployment is authorized here.

## 1. Evidence levels

Every result must identify its level:

- A0: code/contract implemented; not executed.
- A1: automated tests passed with mocked providers and synthetic audio.
- B1: a real provider accepted the request and returned the expected artifact.
- B2: a consenting person completed physical-microphone enrollment and heard generated speech/new agent responses.
- C: employee access, deletion, capacity and operations separately accepted.

A test double returning `voice_id` never qualifies as B1. A real provider ID without playable fresh generated audio never qualifies as B2. A public HTTP healthcheck is not proof of a completed enrollment.

## 2. Gate A — automated required checks

Run the entire original test suite first, including Render regression tests, then all new tests. Record exact commands and failures without excluding inconvenient suites. Exercise the actual hosted composition (`serve.py` -> runtime -> foundation + enrollment), not only isolated helper functions. Keep missing credentials and disabled feature flags explicit in the UI.

| ID | Scenario | Required result |
|---|---|---|
| A-01 | Enrollment disabled or provider keys absent | Existing app remains usable. No live job, provider session, clone or false Ready state. |
| A-02 | Missing/wrong auth, alternate profile ID, guessed session/chunk/voice ID | Reject before returning data or dispatching paid operations. Cover every new route and replayable progress. |
| A-03 | Research-only consent or a model-generated consent claim | Does not authorize new external processing or cloning. |
| A-04 | Create/restart the same enrollment request twice | Stable idempotent result; conflicting payload rejected. |
| A-05 | Upload sequence 0,2,1; repeat 1 with same hash and then different bytes | Out-of-order chunks reconcile, identical retry deduplicates, conflict rejected; finalize detects any missing chunk. |
| A-06 | 30-minute synthetic capture, slow network and dropped connection | Bounded memory and request size; acknowledged chunks survive; backlog pauses before its cap; no whole-session array/blob. |
| A-07 | Unsupported audio, clipping/silence, fake content length, full disk | Safe rejection/actionable rerecord or quota state; no loss of previously acknowledged data. |
| A-08 | AI playback overlaps microphone or another voice is suspected | Exclude questionable clone spans or request more audio; do not call RMS a speaker classifier. |
| A-09 | Incorrect transcript number/negation, colloquial Arabic and embedded English name | Keep source text/spans and draft status; confirmation is specific, human-originated and contextual; no formal-Arabic rewrite. |
| A-10 | Interruption while output is generated but not played | Drop stale output, retain latest caller correction, do not assume unheard audio was delivered. |
| A-11 | Clone request returns verification required | Persist voice resource reference securely, show verification_required, block Ready and test calls. |
| A-12 | Clone/assistant create succeeds remotely but local response times out | Persist outcome_unknown; restarting or clicking again does not repeat creation; reconcile or require operator action. |
| A-13 | Process killed with a leased job/provider operation | Recovery respects lease fencing; no duplicate non-idempotent call; confirmed artifacts retained. |
| A-14 | Voice synthesis empty/invalid or wrong voice ID is returned to assembly | Fail the candidate. No stock voice, replayed enrollment, or invented success. |
| A-15 | Provider account cannot access the private voice from Vapi | Explicit integration failure; never make the clone public or substitute the template's voice. |
| A-16 | Unauthorized preview or mutated assistant override | Deny before call creation; enforce binding, duration/concurrency/reservation on the server. |
| A-17 | Spoken correction affects only a response preference | Confirm it; create behavior v2; reuse voice v1; preview records exact new binding; no silent global rule rewrite. |
| A-18 | Held-out example enters a prompt or extraction set | Exclude it. If promoted by approval, retire/replace that held-out test with an audit record. |
| A-19 | Revoke while collecting, cloning, previewing or awaiting reconciliation | Immediate local deny; stop active work as supported; late provider artifact registered for cleanup; no universal deletion claim. |
| A-20 | Malformed/replayed provider callback, stale event epoch or forged browser tool result | Reject. Do not apply the same confirmation/correction twice. |
| A-21 | Provider redirect, 429, 5xx, timeout and invalid JSON | Bounded sanitized errors; only safe retries; no private response body/credential logged. |
| A-22 | Microphone denied, device removed, page reload, no WebRTC support | Accessible actionable state and pause/resume without fabricating audio; typing not required as the only recovery path. |
| A-23 | Provider durations/usage arrive late or request counter differs from cost | Reservation stays conservative, daily counter not represented as dollar cap, no unbounded runtime. |
| A-24 | Upgrade a copy of the existing database, then restart twice | Versioned additive migrations; existing accounts, audio and examples retained. No live DB use in tests. |
| A-25 | Browser security integration | No wildcard CSP, secret in URL or private key in bundle; narrowly scoped connections; tests for CSRF/origin/websocket boundaries where applicable. |
| A-26 | New candidate identity | AI disclosure preserved; original Sura configuration, number bindings, tools and production webhooks untouched. |

Browser tests must exercise the connected flow with explicitly labeled synthetic/mock providers, including failure and recovery screens. Fake-microphone browser input proves orchestration, not Arabic voice quality. Include a machine-readable test manifest listing what was mocked and what was not run.

## 3. Gate B — real-owner test, separately authorized

### Before the session

An operator verifies the exact release revision, rollback path and disk capacity; configures account credentials privately; checks permitted model/voice API access and Vapi's ability to use private ElevenLabs voices; records an approved session spending allowance. Back up existing data using a consistent database/files procedure and verify recovery. No employee or customer audio is used for this gate.

The contributor reviews the actual enrollment scopes and retention/provider disclosures. Use a headset initially. Speak the natural dialect; do not request a fake Emirati accent from a Jordanian speaker. Fix the hold-out scenarios and listening criteria before examining generated results.

### Required journey

1. Sign in and consent; start audio without entering training text.
2. Complete a target 20–30-minute adaptive conversation, with useful role-play, a counterfactual and at least one critical-detail spoken confirmation. Record elapsed and accepted clean-speech duration separately.
3. Pause/reconnect once and resume the correct interview state, with no lost acknowledged chunks or repeated chargeable creation.
4. Create one real voice version from recorded speaker-only spans, subject to provider verification. Retain provider IDs and private manifests, not recordings in Git.
5. Hear at least three novel synthesized Arabic utterances: an open question, a confirmation involving a number, and a correction. Confirm they are not enrollment replays and identify the provider/voice actually used.
6. Start the new personalized agent inside Raneen. Ask new situations and interrupt/correct it. No Vapi dashboard or transcript editor is required in the contributor journey.
7. Say a response-style correction. After a specific verbal confirmation, run behavior v2 with the same voice v1 and verify the contextual correction on a new example.
8. Leave and return; candidate versions and acknowledged data are present. Perform an operator-controlled restart and repeat access checks.
9. Exercise cancellation/revocation in a separate test enrollment or with explicit confirmation for the current one; observe local deny and truthful provider-cleanup state.

### Engineering pass / quality decision

Engineering requires every journey step to have observable live evidence and every safety-critical invariant to pass. A provider-verification-pending result is a legitimate incomplete enrollment, not an engineering Ready result. One successful run is preliminary owner acceptance, not a reliability study.

Provisional Arabic quality test: ten held-out dialogues covering numbers/negation, place names, buy/rent corrections, interruptions, language switching, uncertainty and new domain choices. Use the same voice and text model for baseline versus personalized behavior, varying only the approved profile/examples. Randomize presentation order. Judge voice identity separately from text behavior.

Suggested internal release targets, to be fixed before testing: contributor median rating at least 4/5 for identity, dialect/phrasing and question/pronunciation naturalness; no uncorrected critical-number/negation error or fabricated business action in the ten cases; clear recorded preference for the personalized condition in at least seven paired cases. These are small-sample product gates, not claims of statistical superiority. Include failures and any uncertainty. Add independent native listeners before external/employee claims.

Measure end-of-turn-to-first-audio p50/p95, early cutoff rate and interruption recovery on the defined test connection. Do not optimize latency by cutting off unfinished speech. Clone-processing duration is reported separately; do not claim a processing SLA based on a single enrollment.

## 4. Gate C — employee-ready release

Individual invitations and first-party sessions (no shared administrator token), per-person resource isolation and permissions, scoped commercial authorization, configurable retention, per-person and organization budgets, suitable private object storage/shared database for intended concurrency, separate recoverable workers as needed, tested backup/restore with deletion tombstones, incident diagnostics, provider artifact inventory/deletion and version promotion/rollback are required. Two simultaneous test contributors must not exchange chunks, evidence, voices, join capabilities or usage allowances. Representative native speakers must confirm performance beyond the original owner.

## 5. Handoff report format

Return one concise evidence record:

- Commit/branch and exact modules/migrations changed.
- Whole-suite baseline versus post-change results, command lines and real failures.
- A0/A1/B1/B2/C status for each capability; do not label an unrun live test passed.
- Provider contract references, pinned dependency/model choices and account capabilities still unverified.
- Browser paths covered, memory/storage limits tested, and recovery cases exercised.
- Remaining blockers and the smallest next implementation increment; preserve unfinished code safely.

Never mark PR #1 merged, production-ready, or the Arabic clone accepted merely because an agent has finished its coding run.
