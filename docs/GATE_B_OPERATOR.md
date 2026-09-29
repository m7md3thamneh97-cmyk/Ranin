# Gate B — controlled real-owner activation

Status: operator checklist. Completing this file does not prove Arabic quality.

## Before deployment

- Merge only reviewed Gate A code into the existing Render staging branch.
- Keep RANEEN_VOICE_ENROLLMENT_ENABLED=0 for the first deployment.
- Verify /healthz, the existing Teaching Studio, the persistent /var/data disk, and owner login after deployment.
- Keep one Render instance. Do not add workers/services for this owner-only test.
- The original Sura assistant, phone numbers, webhooks, tools and CRM integrations must remain unchanged.

## Private Render environment values

Add values in Render Environment; never commit or paste secrets into Git, Codex, URLs, browser source, or chat.

Required:

- OPENAI_API_KEY: project key with access to the selected Realtime model.
- ELEVENLABS_API_KEY: account key allowed to create an Instant Voice Clone and synthesize it.
- VAPI_API_KEY: private server key for reading the template and creating the isolated test assistant.
- VAPI_PUBLIC_API_KEY: a dedicated staging public key, restricted in Vapi to the exact Render origin. It is intentionally browser-visible. For Gate B, rotate/delete it after testing if it cannot also be restricted to the dynamically created test assistant.
- RANEEN_VAPI_TEMPLATE_ID: UUID of an existing safe template assistant. Only its model provider/model and a small transcriber allowlist are copied; tools, phone bindings, server URLs and production instructions are not copied.

Vapi must also have access to the same ElevenLabs account/private voice. If the cloned voiceId cannot be used by the Vapi organization, that is a Gate-B integration failure; never publish the clone or fall back to a stock voice to make the test pass.

Optional bounded configuration:

- RANEEN_REALTIME_MODEL=gpt-realtime-2.1
- RANEEN_LIVE_TRANSCRIBE_MODEL=gpt-live-transcribe
- RANEEN_INTERVIEWER_VOICE=marin
- RANEEN_INTERVIEW_MAX_SECONDS=2100 (35-minute server hard stop; code clamps 60–3600 seconds)
- RANEEN_CLONE_MIN_MS=60000
- ELEVENLABS_TTS_MODEL=eleven_multilingual_v2
- RANEEN_VAPI_VOICE_MODEL=eleven_multilingual_v2

Leave RANEEN_VOICE_ENROLLMENT_ENABLED=0 until the smoke checks below pass.

## Provider/account checks

These are account-specific and cannot be inferred from code:

1. OpenAI key can create a Realtime WebRTC session with the configured model.
2. ElevenLabs plan allows IVC creation and fresh Arabic synthesis; any required speaker verification is completed by the speaker, not bypassed.
3. Vapi private key can read the configured template and create a separate assistant.
4. Vapi public key is restricted to the staging Render origin.
5. Vapi's ElevenLabs integration can use the private clone returned by the same ElevenLabs account.
6. Provider billing/usage limits are set separately. Local duration/operation limits are safety controls, not guaranteed dollar caps.

## Enable and smoke test

After the default-off release is healthy:

1. Set RANEEN_VOICE_ENROLLMENT_ENABLED=1 and redeploy the existing build.
2. Sign in as the owner and open /enroll.
3. Confirm that no provider call starts before the explicit enrollment consent.
4. Use headphones.
5. Run a short developer interview first (not acceptance evidence):
   - natural Arabic conversation;
   - one role-play;
   - one counterfactual;
   - one spoken confirmation/correction;
   - pause/resume once.
6. Confirm microphone chunks survive pause/resume and no interviewer audio is intentionally submitted as clone input.
7. Create one clone only. On timeout/unknown state, stop and reconcile; do not retry blindly.
8. Listen to all three fresh Arabic previews (question, number confirmation, correction).
9. Create the isolated Vapi assistant and talk to it through the Raneen preview.
10. Return to enrollment, make one spoken behavior correction, create behavior v2, and confirm the same voice ID is reused.

## Gate-B owner acceptance

Then run the full target 20–30-minute session using only the consenting owner's voice. Record privately:

- exact Git commit/deployment;
- provider artifact IDs (not credentials);
- elapsed interview time and acknowledged microphone capture time;
- clone processing/verification outcome;
- fresh preview results;
- held-out behavior cases;
- interruption and critical-number/negation failures;
- any provider costs/usage available after finalization.

Do not call the product ready because a provider returned an ID. The owner must hear novel cloned speech and complete a browser conversation with the cloned-voice agent.

## Stop conditions

Stop without retries when:

- clone/assistant creation is outcome_unknown;
- speaker verification is required and incomplete;
- microphone capture contains obvious speaker/interviewer contamination;
- Vapi cannot access the private ElevenLabs clone;
- critical numbers/negation are repeatedly lost;
- provider spend exceeds the pre-agreed test allowance;
- revocation is requested.

Provider cleanup after revocation is tracked separately; never claim universal deletion of backups, exported copies or provider artifacts until each applicable system confirms it.
