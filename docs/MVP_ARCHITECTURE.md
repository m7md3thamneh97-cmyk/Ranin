# Raneen MVP Architecture

Status: canonical MVP contract  
Branch: `mvp/learning-engine-foundation`

## 1. Product contract

Raneen starts competent at conversation but generic about the trainer. A real-estate agent talks to Raneen naturally. Raneen asks increasingly targeted questions, tests what it thinks it has learned, accepts spoken corrections, and progressively mirrors the person's professional communication.

The MVP must make one loop real:

```
talk
  -> capture evidence
  -> infer a behavioral hypothesis
  -> test the hypothesis in a fresh scenario
  -> accept a spoken correction
  -> update a draft personal model
  -> retry
  -> improve
```

Voice cloning runs alongside that loop:

```
clean trainer speech
  -> eligible sample pool
  -> voice candidate
  -> preview
  -> approved voice version
  -> simulation using that voice
```

The UX should feel continuous. The implementation must not blindly mutate the agent after every sentence.

## 2. Non-goals for this MVP

Do not build these yet:

- live property-market access
- Property Finder/Bayut integrations
- CRM integrations
- telephony routing
- outbound calling
- customer production calls
- autonomous booking or payments
- model fine-tuning
- generic CRM dashboards
- broad analytics
- production employee administration

The architecture must leave clean extension points for business knowledge and tools later.

## 3. Preserve the existing Studio as Layer 0: evidence

The current repository already has valuable primitives and they remain authoritative for raw human evidence:

- users and contributor profiles
- explicit consent and withdrawal
- original PCM WAV recordings
- exact transcripts
- demonstrated examples
- preference comparisons
- human review states
- audit trail
- train/holdout separation
- behavior and voice exports

Do not rewrite this foundation into a new incompatible system. Extend it.

Layer 0 answers: **what did the human actually say or demonstrate?**

Everything learned by the AI must be traceable back to Layer 0 evidence.

## 4. Architectural principle: fast conversation path vs learning path

Raneen has two paths running together.

### A. Fast path — conversation

Latency-sensitive.

```
Browser
  -> realtime voice runtime
  -> transcript events
  -> conversation orchestrator
  -> assistant response
  -> speech
```

This path must stay responsive and cannot wait for deep profile extraction.

### B. Learning path — understanding the trainer

Can complete immediately after each turn or short exchange.

```
completed human turn
  -> evidence record
  -> structured observation extraction
  -> hypothesis update
  -> confidence/provenance update
  -> next-question planner
  -> optional evaluation
```

The fast path consumes the latest approved/usable personal model. The learning path proposes changes to that model.

## 5. MVP service boundaries

Keep one deployable application for now, but enforce modules as if they were services.

```
studio/
  evidence/               # existing recording, consent, review
  sessions/               # live teaching session lifecycle
  conversation/           # realtime runtime adapter + turn model
  learning/               # observations, hypotheses, corrections
  profiles/               # projected personal model + versions
  voice/                  # sample eligibility, clone versions, preview
  simulation/             # role reversal: Raneen acts as trainer
  evaluation/             # deterministic regression scenarios
  providers/              # Vapi / ElevenLabs / LLM adapters
  knowledge/              # future domain/market knowledge boundary
```

Do not split these into microservices yet.

## 6. Concrete provider strategy for MVP

### Realtime conversation: Vapi Web SDK

Use Vapi for browser realtime voice conversation and turn/transcript events. It gives the MVP barge-in, realtime speech and later a direct path to phone calls without making Vapi the source of truth.

Vapi is an adapter, not the Raneen brain.

### Voice cloning and TTS: ElevenLabs

Use ElevenLabs Instant Voice Cloning for the first voice candidate.

Do not regenerate a clone after every sentence. Maintain an eligible sample pool and create a new voice version at deliberate checkpoints.

Keep the raw source audio in Raneen storage so the voice can be recreated or moved later.

### Learning model

Put all LLM calls behind `LearningModelProvider`.

The first implementation may use one provider/model, but no database schema should contain provider-specific prompt formats.

## 7. Browser architecture

The current vanilla JS client can remain for the MVP. Do not migrate frameworks merely to add realtime behavior.

Add one primary experience: **Conversation**.

States:

```
idle
connecting
teaching
learning
voice_candidate_ready
simulating
correcting
retrying
ended
error
```

The user should remain on one screen.

A technical reconnect at the voice-clone boundary is acceptable in the MVP if the UI keeps the same session and makes the transition feel continuous.

## 8. Session model

Add a durable teaching session.

### teaching_sessions

- id
- profile_id
- status
- mode: teaching | simulation
- started_at
- ended_at
- realtime_provider
- provider_call_id
- active_profile_version_id
- active_voice_version_id
- created_at

### conversation_turns

- id
- session_id
- turn_index
- role: trainer | raneen
- transcript
- transcript_state: partial | final | verified
- audio_id nullable
- started_at
- ended_at
- provider_metadata_json
- created_at

The ordered conversation is now first-class data. Existing `examples` remain curated demonstrations, not substitutes for raw turns.

## 9. Learning model

### observations

One atomic inference grounded in evidence.

- id
- profile_id
- source_turn_id or source_example_id
- category
- key
- value_json
- confidence
- state: observed | confirmed | locked | rejected
- created_at
- superseded_at nullable

Examples:

```
category=language
key=code_switching
value={"pattern":"Arabic by default; English for project names"}
```

```
category=behavior
key=price_objection.first_move
value={"action":"explore_comparison"}
```

### corrections

Corrections are stronger evidence than ordinary observations.

- id
- profile_id
- session_id
- target_turn_id
- correction_transcript
- normalized_rule_json
- created_at

### hypotheses

A hypothesis groups repeated compatible observations.

- id
- profile_id
- key
- value_json
- confidence
- evidence_count
- state: tentative | confirmed | locked | rejected
- last_tested_at
- created_at
- updated_at

### Evidence precedence

```
locked explicit rule
  > explicit correction
  > explicit confirmation
  > repeated demonstrations
  > repeated observations
  > single observation
  > model inference
```

Never treat model inference as equivalent to human confirmation.

## 10. Personal model projection

Do not store one giant generated prompt as the profile.

Create versioned structured projections.

### agent_profile_versions

- id
- profile_id
- version_number
- status: draft | approved | published | archived
- parent_version_id
- snapshot_json
- created_at
- approved_at nullable

Snapshot sections:

```json
{
  "language": {},
  "delivery": {},
  "conversation": {},
  "real_estate_behavior": {},
  "confirmed_rules": [],
  "avoid": [],
  "representative_examples": []
}
```

A runtime prompt/config is compiled from the structured snapshot. It is a build artifact, not the source of truth.

## 11. Curiosity / next-question planner

The interviewer must not ask a fixed questionnaire forever.

After each useful exchange, compute gaps using:

1. scenario coverage
2. hypothesis confidence
3. contradictions
4. high-value real-estate skills
5. recent corrections

The planner returns one of:

- ask a natural follow-up
- ask why the trainer chose an action
- introduce a variation
- test an inferred rule
- challenge a contradiction
- role-reverse into simulation
- continue listening without asking

Example:

```
known:
  price objection -> explore comparison first

unknown:
  what happens if customer already supplied a comparable project?

next probe:
  "طيب إذا هو أصلاً قال لك إنه يقارنها بمشروع ثاني، شو بتسأله بعدها؟"
```

The planner should prefer information gain, not completion percentages.

## 12. Voice pipeline

### voice_samples

Do not duplicate audio bytes; point to existing `audio` records.

- id
- profile_id
- audio_id
- source_turn_id
- eligibility: pending | eligible | rejected
- rejection_reason nullable
- quality_json
- created_at

Eligibility initially uses deterministic checks plus human/provider constraints:

- speaker owns/consented to voice
- trainer-only audio
- no obvious clipping/silence
- transcript available
- no known second speaker
- desired speaking style
- not a holdout-only restricted sample

### voice_versions

- id
- profile_id
- version_number
- provider
- provider_voice_id
- source_manifest_json
- status: building | ready | approved | rejected | archived
- created_at
- approved_at nullable

### MVP voice lifecycle

```
~1-2 minutes eligible clean speech
  -> create IVC candidate
  -> synthesize fixed preview sentence(s)
  -> trainer hears preview
  -> approve/reject
  -> use approved voice in simulation
```

Do not silently replace the approved voice.

Later, more data can generate a higher-fidelity version, but voice upgrades remain explicit versions.

## 13. Simulation / role reversal

Simulation is how Raneen proves it learned.

The trainer becomes the customer. Raneen receives:

1. shared Raneen domain policy (small authored MVP set)
2. current draft personal profile
3. relevant approved demonstrations
4. scenario state
5. invariant safety/truthfulness rules

The trainer can interrupt naturally.

After each Raneen turn, the user can:

- continue as customer
- say it was wrong
- explain what was wrong
- demonstrate the preferred response
- retry immediately

A correction creates new evidence and a new draft profile version. It does not mutate a published version in place.

## 14. Evaluation

Every confirmed behavioral rule should be testable against a small scenario set.

MVP evaluations are not subjective scores like "83% personality match".

Use pass/fail or inspectable criteria:

- preserves confirmed budget after interruption
- asks purpose before recommendation when that rule is confirmed
- does not invent availability
- does not guarantee ROI
- respects explicit language switch
- applies latest explicit correction
- does not pull holdout examples into training context

Evaluation results:

### evaluation_runs

- id
- profile_version_id
- suite_version
- results_json
- status
- created_at

A profile can still be tested manually even when automated evaluation is partial.

## 15. Realtime event contract

Frontend and backend must share these semantic events regardless of vendor:

```
session.started
session.ended

trainer.speech_started
trainer.turn_partial
trainer.turn_completed

raneen.thinking
raneen.turn_started
raneen.turn_completed
raneen.interrupted

learning.observation_created
learning.hypothesis_updated
learning.correction_recorded
learning.profile_version_created

voice.sample_accepted
voice.sample_rejected
voice.candidate_building
voice.candidate_ready
voice.version_approved

simulation.started
simulation.retry_started
simulation.ended

error
```

Provider events are translated into these events before entering application state.

## 16. API contract for the first vertical slice

Existing endpoints remain.

Add:

```
POST /api/sessions
GET  /api/sessions/{id}
POST /api/sessions/{id}/end

POST /api/sessions/{id}/turns
GET  /api/sessions/{id}/turns

GET  /api/profiles/{id}/learning-state
GET  /api/profiles/{id}/versions

POST /api/turns/{id}/analyze
POST /api/turns/{id}/correct

POST /api/profiles/{id}/voice/candidate
POST /api/voice/{id}/approve
POST /api/voice/{id}/reject

POST /api/sessions/{id}/simulation
POST /api/simulations/{id}/retry
```

Realtime vendor webhooks get a separate authenticated provider endpoint and cannot write arbitrary application state directly.

## 17. Knowledge boundary for later market data

Do not mix property facts into personal learning.

Create an interface now:

```
KnowledgeProvider
  search(query, constraints)
  get_entity(id)
  get_current_fact(entity, field)
  provenance(result)
```

Future implementations may read approved agency documents, CRM, inventory, Property Finder/Bayut or other sources.

The runtime hierarchy will be:

```
compliance / hard policy
  > verified current business facts
  > shared real-estate domain behavior
  > personal confirmed behavior
  > personal language / delivery
```

Personalization can change expression and preferred process but cannot rewrite verified facts.

## 18. Data ownership and traceability invariant

For every learned personal behavior, Raneen must be able to answer internally:

- which human evidence produced this?
- was it inferred, demonstrated, confirmed or corrected?
- who owns that evidence?
- is consent still active?
- which profile version first used it?

If this provenance is lost, the learning system is wrong even if the output sounds good.

## 19. First engineering milestone: M1 — Learning Loop

M1 is done when one contributor can:

1. start a conversational teaching session
2. speak naturally to Raneen
3. have completed turns persisted with transcript and trainer audio
4. receive a context-aware follow-up question
5. produce at least one structured behavioral hypothesis
6. see/hear Raneen test that hypothesis in a fresh scenario
7. verbally correct Raneen
8. have that correction stored with stronger precedence
9. retry the scenario using the updated draft profile
10. observe a materially changed response

No voice clone is required to pass M1.

## 20. Second milestone: M2 — Progressive Voice

M2 is done when:

1. eligible trainer speech accumulates automatically
2. a clone candidate can be created from consented eligible samples
3. the trainer hears a fixed preview
4. the candidate can be approved/rejected
5. approved voice is used in simulation
6. a later candidate creates a new version rather than replacing the old one

## 21. Third milestone: M3 — Expert Corpus

M3 is done when multiple expert contributors can:

- teach the same scenario families independently
- have examples reviewed
- promote selected examples/rules to shared domain intelligence
- preserve individual profile differences
- expose disagreement rather than averaging it away
- keep held-out families isolated from training/retrieval

## 22. Deployment strategy

For this internal MVP:

- retain FastAPI
- retain one deployable app
- retain SQLite + persistent volume while concurrency is small
- retain the existing audit and consent model
- add migrations before schema changes become numerous
- add provider keys only as environment secrets
- never expose private provider keys to the browser
- use a restricted Vapi public key for browser calls
- keep raw voice source files under Raneen custody

Move to Postgres/object storage only when team concurrency, corpus size or operational reliability justifies it.

## 23. Required test classes

Every backend mission must add tests for:

- consent gates
- contributor ownership
- turn ordering/idempotency
- evidence provenance
- hypothesis precedence
- correction supersession
- profile version immutability
- holdout isolation
- voice sample eligibility
- provider failure without corrupting state

Frontend tests/smoke checks must cover:

- reconnect without losing Raneen session identity
- interruption
- transcript finalization
- correction -> retry
- clone preview approval/rejection
- loss of provider connection

## 24. Product rule

The user experience may say:

> "I'm starting to understand how you handle investors."

The system must never internally translate that into:

> "Overwrite the agent prompt with whatever was just said."

Raneen should feel continuously alive while remaining versioned, inspectable and reversible.
