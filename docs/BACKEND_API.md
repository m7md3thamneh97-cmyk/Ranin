# Conversation frontend integration contract

Version: `raneen-backend-v1`. All existing Studio routes remain. The backend preserves the original consent, recording, review and export layer. It does not replace the current frontend.

## Composed enrollment runtime

`run.py` and `serve.py` use `studio.runtime.create_app`, joining the Conversation frontend, trusted voice enrollment and this backend. Enrollment `/journey` and `/workflow` responses include a server-authorized `learning` binding with `contract`, `profile_id`, and `session_id`. The frontend uses these IDs for owner-only learning-state and event-cursor reads.

Linked enrollment records accept mutations only from the authenticated provider-side voice bridge. Legacy turn, correction, confirmation, consent, typed-example and profile-mutation APIs reject writes to them. Exact spoken confirmation and original enrollment permission remain authoritative. Revocation blocks generic learning reads as well.

The spoken interviewer supports `start_simulation`, `propose_correction`, `retry_simulation`, and `continue_teaching` on the trusted sideband, with durable replay receipts. Interview practice uses the interviewer voice and says so. The separately approved private agent preview uses the consented clone and a version-bound behavior pack.

In the composed app, generic `/api/sessions/{sid}/transport` and `/api/profiles/{pid}/voice/candidate` return `410`; use the bounded enrollment clone and private preview-room routes. A reusable Vapi public key is never returned by the composed preview flow. The following sections describe standalone evidence/development contracts and read surfaces, not an alternative enrollment consent path. New remote adapters require `RANEEN_LEARNING_STUDIO_ENABLED=1`; enrollment retains its separate default-off feature flag.

## Access and provider modes

Every contributor API requires `Authorization: Bearer <private Studio token>`. Tokens never belong in URLs. Same-origin JSON requests use `Content-Type: application/json`; empty mutations send `{}`. Hosted deployments remain owner-only.

`GET /api/backend/status` reports provider configuration and limitations. Default `local_rules` offers finite offline extraction and scripted replies; it does not reproduce a person's general reasoning or vocal identity. Remote adapters need keys and separate contributor authorization. `live_verified:false` means configuration has not proved a successful live call.

`GET /api/backend/schema` exposes the full generated OpenAPI contract after authentication. Raw personal snapshots are owner-only. Administrators retain the existing reviewed-evidence workflow; current consent does not restore access to withdrawn source evidence.

## One continuous trainer experience

1. Select/create a profile and record the existing collection consent.
2. `POST /api/sessions` with `profile_id`, optional `scenario_id` and a stable `idempotency_key`. Keep the returned session ID throughout teaching, simulation and technical reconnects.
3. Capture trainer-only PCM WAV with the existing recorder. Create completed turns with `POST /api/sessions/{sid}/turns`. Partial transcripts stay in the interface; completed raw turns are immutable. Provide a stable `external_id` for request retries. `turn_index` is optional; if supplied, it must equal the next server index, starting at zero.
4. Final trainer turns automatically queue analysis. Poll `GET /api/sessions/{sid}/learning-jobs` and cursor-based `GET /api/sessions/{sid}/events?after=0`. A failed analysis retains the original evidence and can be retried. `analyze:false` explicitly defers learning. Holdout and customer-role simulation speech never automatically become personal behavior evidence.
5. `POST /api/sessions/{sid}/respond` generates a fast conversational reply using the latest usable model and curiosity planner. Analysis is a separate path. `GET /api/sessions/{sid}/next-question` returns the next probe without generating a turn.
6. `POST /api/sessions/{sid}/simulation` starts role reversal with a training scenario. The response includes `id`, `turn_id`, `response_text`, `profile_version_id` and `voice_version_id`. The session switches into simulation. Original teaching turns retain their capture mode and remain valid evidence.
7. Capture the trainer's spoken correction as another trainer turn. `POST /api/turns/{target_raneen_turn_id}/correct` with only `source_turn_id`. The backend normalizes the spoken correction, stores its exact source words, creates stronger evidence and projects a new model. A reviewed structured `rule:{category,key,value}` may be supplied instead. `lock:true` requires an explicit structured rule.
8. `POST /api/simulations/{simulation_id}/retry` uses the latest model. The original attempt remains unchanged. Use `GET /api/simulations/{id}` to inspect it and `GET /api/simulations/{id}/audio` for speech from its approved voice version.
9. `POST /api/sessions/{sid}/resume-teaching` returns to the interviewer. `POST /api/sessions/{sid}/end` explicitly ends the whole session.

Example completed turn:

```json
{
  "role": "trainer",
  "transcript": "When someone says a property is expensive, I ask what they are comparing it with.",
  "transcript_state": "verified",
  "audio_id": "<original-wav-id>",
  "external_id": "browser-turn-001"
}
```

The server adds immutable `provider_metadata.capture_mode` and `split`. Client values cannot override those fields. Caller speech in simulation stays raw evidence but cannot be inferred as the trainer's professional response.

Example spoken correction request:

```json
{"source_turn_id": "<completed-trainer-correction-turn-id>"}
```

The source must be a later trainer turn in the same training session. If no reliable rule can be extracted, the API returns `422` and the interface should ask for a clearer preferred response. Never show a match percentage.

## Progressive voice

Upload original trainer-only PCM WAV via the existing `POST /api/profiles/{pid}/audio`. A turn may cite the audio at creation. If a server transcript arrives first, `POST /api/turns/{tid}/audio` with `{audio_id}` adds an immutable evidence attachment without rewriting the turn.

`GET /api/profiles/{pid}/voice/samples` returns samples and voice versions. New samples are pending; duration and signal heuristics do not establish clean speech or speaker identity. Owner review through `POST /api/voice/samples/{id}/review` requires explicit `trainer_only`, `clean_speech` and `desired_style` confirmation.

After at least 60 reviewed, unique, authorized seconds, the standalone `POST /api/profiles/{pid}/voice/candidate` with a stable `idempotency_key` builds a candidate from a bounded sample pool. A failed candidate is not success. Uncertain creation and interrupted dispatch cannot be repeated blindly. Owner `POST /api/voice/{id}/reconcile` accepts a verified provider resource ID, or an explicit checked-absence decision with notes. Cached synthesis retries reuse the original voice.

- `GET /api/voice/{id}/preview`: fixed cached MP3 preview; never autoplay an unapproved new voice as the active representative.
- `POST /api/voice/{id}/approve`: owner explicitly accepts the ready candidate.
- `POST /api/voice/{id}/reject`: rejects with optional `{notes}`.

The next simulation selects the most recently approved usable voice. Failed/rejected upgrades preserve the previous approval. A later voice checkpoint may require stopping the browser call and reconnecting; keep the same Raneen session. No clone is created after every utterance.

## Separate external processing authorization

The existing internal collection/export consent is not permission to upload to providers. Use `POST /api/profiles/{pid}/provider-authorizations` after showing the contributor which provider will receive text/audio and obtaining voluntary authorization:

| Provider | Scope | Data/use |
|---|---|---|
| `openai` | `text_learning` | Structured learning, context and dialogue generation |
| `elevenlabs` | `voice_clone` | Approved original voice samples and synthesis |
| `vapi` | `realtime_voice` | Live audio/transcription and browser voice transport |

Request fields are `provider`, `scope`, `self_attestation:true`. Grants are tied to a consent epoch. A new grant does not retroactively authorize old evidence. To use an old epoch, the application needs its separate explicit grant; new sessions are the normal path. `POST /api/profiles/{pid}/provider-authorizations/{id}/withdraw` revokes the grant. Full profile withdrawal revokes grants and ends active sessions.

## Vapi browser transport

`POST /api/sessions/{sid}/transport` provisions a server-side assistant. It returns only the restricted browser public key, assistant ID and Raneen session ID; private model/webhook/provider credentials remain on the server. The legacy GET alias is deprecated.

Use the Vapi Web SDK with the returned assistant ID. The backend is Vapi's custom LLM and composes the latest usable personal context on every reply. The browser must bundle the SDK locally and configure the app's CSP/network policy for the actual Vapi/WebRTC connections. The existing evidence UI does not load that SDK.

Default server ingestion uses `conversation-update` timed cumulative history. Receipts use call, speaker and speech time rather than mutable artifact URLs. Browser transcript events serve the live interface and should not be posted again as new trainer turns. Capture original trainer audio separately, then attach it to the canonical backend turn ID from session events/turns.

Optional legacy transcript callbacks use stable event IDs/timestamps; a call commits to one ingestion format, preventing duplicate learning from mixed formats. Identity-free callbacks acknowledge `pending_timed_history` and await canonical history. Replayed history adds no duplicate turns. Provider callbacks use a separate bearer secret and call-to-session binding; contributor tokens do not authorize them.

A transport `status-update: ended` preserves the teaching session. After stopping the browser call, use `POST /api/sessions/{sid}/reconnect`, then provision/start a new browser call. Deliberate training completion uses `/end`.

The custom LLM offers OpenAI-compatible JSON and SSE. Current SSE sends a complete reply as one chunk after generation; token-by-token model streaming and measured voice latency are not claimed.

## Inspection and regression endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/profiles/{pid}/learning-state` | Usable evidence, hypotheses, current version and next question |
| `GET /api/profiles/{pid}/versions` | Immutable owner-visible snapshot history |
| `POST /api/hypotheses/{id}/confirm` | Confirmation linked to a real trainer source turn |
| `POST /api/profiles/{pid}/learning/import-examples` | Import eligible approved training demonstrations |
| `POST /api/profiles/{pid}/evaluations` | Inspectable authored regression suite; optional profile version ID |
| `GET /api/profiles/{pid}/evaluations` | Owner-visible evaluation reports |
| `GET /api/profiles/{pid}/runtime` | Provider-neutral compiled context and approved voice |
| `POST /api/learning-jobs/{id}/retry` | Resume failed inference without recapturing speech |

Holdout human demonstrations are excluded from prompts, learning, retrieval and voice cloning. Evaluation may use an authored held-out caller stimulus but never the held-out trainer answer. Reports distinguish `pass`, `fail` and `partial`; lexical checks do not measure personality, accent or comprehensive reasoning fidelity.

## Error handling

`401` means invalid credential; `403` ownership/provider authorization; `409` lifecycle, consent, version/partition conflict; `422` invalid or unclear evidence. Provider failures return sanitized `502`/`503`, or a voice candidate whose persisted status is `failed`. Display that distinction accurately. Session events use durable cursors so reconnects can catch up. No private credential or detailed provider response belongs in a browser error.
