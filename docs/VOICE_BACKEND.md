# Progressive voice backend

The integrated runtime keeps **voice-only enrollment as the primary voice path**: enrollment captures microphone chunks, records its own explicit consent, creates the clone, synthesizes three fresh previews, collects approval, and reuses that voice in the private Vapi agent. Generic research candidate creation is unavailable through the integrated runtime. The curated Studio engine below preserves existing records and provides a validated read adapter; it does not silently promote research consent or clone the enrolled speaker again.

`VoiceEngine` implements the M2 sample → candidate → fixed preview → explicit approval loop. A clone is created only through the configured ElevenLabs adapter. Without a key, a candidate fails visibly; no local voice or synthetic imitation is presented as a clone.

## Evidence and eligibility

Completed trainer turns are ingested automatically. A sample points to the original Studio WAV and its immutable conversation turn. For a server transcript followed by browser audio, the engine uses the append-only `turn_audio_attachments` record without changing the turn.

Automatic ingestion proposes a **pending** sample. Signal duration and clipping/silence checks do not identify the speaker, establish intelligibility, or prove that the sound is speech. The profile owner must separately confirm:

- the recording contains only their trainer voice;
- it is clean speech;
- it represents the desired speaking style.

Captured teaching/split metadata remains authoritative after a session switches to simulation. Trainer speech captured while playing the customer in simulation cannot enter cloning. Held-out turns, held-out examples, and exact WAV copies of either remain excluded. Identical audio is counted once, even if it has different upload IDs.

A candidate requires at least 60 seconds of eligible reviewed signal duration. `VoiceEngine(..., minimum_seconds=...)` can raise that threshold, but cannot lower it. Longer recordings do not imply better cloning quality.

At each checkpoint the engine selects the newest reviewed eligible samples toward a two-minute target, bounded to 30 recordings and 20 MB. This keeps longer teaching sessions within the provider's upload limits and lets later versions use newer speech. Source authorization is evaluated independently for every selected recording.

## Consent and external processing

Before provider upload, approval, cached preview playback, or simulation speech, the engine verifies current collection/voice scope, every original source consent, and separate active self-attested ElevenLabs `voice_clone` authorizations. A new consent cannot restore withdrawn recordings or their old voice versions. An internal `voice_export` scope alone never authorizes provider upload.

Each immutable candidate manifest records original audio IDs/checksums, source turns, reviewed sample IDs, consent IDs, authorization IDs and duration estimates. Original WAV checksums are verified before external use. Withdrawal or source disqualification blocks future use of already-created versions, including locally cached previews.

## Version lifecycle and retry

Candidates are versioned as `building`, `ready`, `approved`, `rejected`, or `failed`. They become ready only after the provider returns a real voice ID and synthesizes the fixed Arabic preview defined by `PREVIEW_TEXT`. The preview discloses AI identity. Approval is explicit and restricted to the profile owner. A new candidate, rejected candidate or failed upgrade leaves the previously approved voice unchanged.

The `idempotency_key` is scoped to the profile. Repeating a successful request returns its existing candidate. If cloning succeeded but preview synthesis failed, retry reuses that provider ID. An explicit confirmed HTTP rejection or a failure before dispatch can permit a bounded retry. A timeout, malformed successful creation response, generic creation failure, or interrupted dispatch leaves the result uncertain: **a stale build or missing returned ID never permits another creation**. Provider exception bodies and credentials are never saved in errors or audit details.

Migration `007_voice_operations.sql` adds durable provider receipts with a unique operation-tagged resource name. States distinguish prepared requests, dispatched requests, known rejections, successful creation, uncertain outcomes and explicit reconciliation. A startup call to `recover_pending_clones()` converts interrupted dispatches into uncertain outcomes. One unsettled creation blocks other candidate keys for that profile. Receipts survive local deletion so an unknown external resource remains discoverable.

The owner-only reconciliation endpoint is `POST /api/voice/{id}/reconcile`. A found resource requires a read-only provider check matching its exact operation name, cloned category, account ownership and speaker-verification status. The checked clone is reused and still requires its fixed preview and approval. Alternatively, an operator who has checked the provider account may explicitly record that no resource was created, with evidence notes, before requesting another attempt. The engine never infers that absence from a timeout. Known rejected attempts are capped at three per candidate.

Provider verification requirements remain failed candidates until an adapter independently confirms readiness through `verify_ready`; their remote IDs are kept privately for cleanup. If the adapter lacks a readiness verifier, retry remains blocked. TTS success alone does not bypass speaker verification. No failed or unverified candidate can be approved or used in simulation.

## Public engine contract

| Method | Result |
|---|---|
| `ingest_turn(turn_id, actor_id=None)` | Pending/rejected sample, existing sample on replay, or `None` when no completed trainer audio exists |
| `list_samples(profile_id)` | Samples with parsed `quality` |
| `review_sample(id, actor_id, trainer_only=..., clean_speech=..., desired_style=True, notes='')` | Owner-reviewed sample |
| `create_candidate(profile_id, actor_id, idempotency_key=None)` | Persisted ready/failed candidate with sanitized error |
| `get_version(id)` / `list_versions(profile_id)` | Version metadata with parsed `source_manifest`, `error`, and `preview_available` |
| `approve(id, actor_id)` / `reject(id, actor_id, notes='')` | Explicit candidate decision |
| `preview(id)` | Cached fixed-preview MP3 bytes after rechecking authorization |
| `active_voice(profile_id)` | Most recently approved usable version, or `None` before any approval |
| `synthesize(approved_id, text)` | Provider MP3 speech for an approved, currently authorized voice |
| `delete_external_for_profile(profile_id)` | Provider cleanup results and local preview removal |
| `retry_external_cleanup(profile_id)` | Retry pending provider cleanup, including after local profile deletion |
| `delete_local_previews(profile_id)` | Remove cached MP3 and temporary preview files before deleting version rows |
| `list_clone_operations(profile_id)` | Durable creation receipts; HTTP handlers enforce profile ownership |
| `recover_pending_clones()` | Startup recovery in the single-process deployment; block unknown outcomes |
| `reconcile_clone(id, actor_id, provider_voice_id=..., confirmed_not_created=..., notes=...)` | Explicit checked provider-resource or checked-absence reconciliation |
| `resolve_approved_enrollment_voice(enrollment_id, actor_id)` | Read-only descriptor for an explicitly bound, approved enrollment voice |

Mutation methods enforce the profile owner themselves. HTTP route handlers must enforce profile access for list/get/preview/synthesis methods, as they do for existing Studio evidence. `HTTPException` represents deterministic ownership/consent/evidence conflicts. Provider candidate failures return a persisted `failed` version so the UI can show the candidate and resume it.

## Storage and deletion

Raw WAVs stay in the existing private audio directory; previews are stored in the private data root under `voice-previews`. Neither directory is mounted as static content.

The cleanup ledger keeps remote voice custody immediately when a provider returns an ID. Its records intentionally survive local profile deletion. A successful provider deletion clears the stored version's remote ID; a provider outage leaves a pending retry record and an explicit `external_deletion_failed` result. Cleanup errors are never reported as successful external deletion. A consent change during cloning blocks preview synthesis and queues/removes the remote voice.

`tests/test_voice.py` covers consent/source scopes, explicit provider authorization, owner review, holdout copies, attachments, duration/deduplication, immutable capture mode, fixed previews, approval/versioning, failure/resume, unconfigured providers, provider verification custody, original checksum changes, withdrawal during cloning, and remote cleanup after local deletion.

`tests/test_voice_operations.py` covers ambiguous timeout without duplicate creation, restart cuts, legacy-schema upgrades, explicit checked absence, checked resource adoption, stock/mismatched voice rejection, pending reconciliation after local deletion, and validated enrollment voice resolution. All fixtures are synthetic and provider calls are mocked; they establish code behavior rather than live Arabic voice quality.

## Enrollment resolution

`resolve_approved_enrollment_voice` requires the enrollment feature flag, exact account ownership, current enrollment scopes, a ready matching voice version, an intact source manifest, a completed clone receipt without pending verification, explicit approval, and all three original synthesized preview artifacts/checksums. It verifies original source chunks still belong to that enrollment's contributor and have their original checksums. Withdrawal, scheduled cleanup, pending creation, missing artifacts and conflicting voice IDs block resolution.

The returned descriptor identifies its source as `enrollment`; its ID is an enrollment voice-version ID, **not** a research `voice_versions` ID. The caller must explicitly establish the profile/enrollment binding and recheck this descriptor before use. Resolution does not copy audio, create research samples, create a new clone or automatically change the active Studio voice. Existing enrollment playback and provider controls remain authoritative.
