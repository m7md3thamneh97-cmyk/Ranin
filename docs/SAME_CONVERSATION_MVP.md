# Raneen single-conversation MVP

The new public entry at `/` and `/create` asks for explicit own-voice consent,
then starts one bounded, private Vapi web conversation using the existing Raneen
assistant template. There is no registration or Studio prerequisite. `/enroll`,
`/studio` and their owner authorization remain available.

## Implementation

- `studio/becoming.py`, `studio/becoming_providers.py` and additive SQL migrations:
  scoped session access, durable operations, isolated PCM evidence, provider
  adapters, synthesis validation, same-call handoff and deletion recovery.
- `studio/static/become*`: Arabic/English page, microphone-only AudioWorklet
  capture, bounded upload queue, one Daily connection and truthful server states.
- `studio/app.py`, `studio/runtime.py`, `studio/teaching.py`: narrow public routing,
  capability authorization before body reads, page security policy and integration.
- `tests/test_becoming*.py`, `tests/becoming_browser.mjs`, `tests/becoming_dom.py`
  and `.github/workflows/becoming.yml`: backend/security, media lifecycle and
  actual browser regression with explicitly synthetic providers and microphone.

## Runtime behavior and evidence

Eligible time comes from mono 24 kHz PCM admitted during provider-reported user
speech relayed by the server, with overlap/assistant-tail exclusion and
energy/clipping checks. Vapi client messages are disabled: the browser receives
only an allowlisted stream of authenticated events, never inline assistant
configuration, callback authentication headers or private call-control URLs.
A separate transient remote-playback analyser vetoes collection while the
assistant is audible; it never enters the microphone capture graph. The
browser never routes remote assistant audio into the capture graph. These gates
reduce contamination; they do not establish speaker identity, transcription
accuracy or a clean physical microphone environment. The sample is capped at
125 seconds and 8 MiB. Default eligibility is 30 seconds, configurable to 30–125.
A documented provider rejection of invalid audio can raise the threshold to
45 and 60 seconds; unknown outcomes and access/credit failures never trigger
another clone create.

A provider voice ID alone is insufficient. ElevenLabs verification requirements
block use; a fresh synthesized sample must decode successfully before readiness.
Handoff dispatch preserves the actual Vapi call ID and uses full conversation
context. Dispatch acceptance remains SWITCHING. CLONED_ACTIVE requires an
authenticated provider speech event identifying that same call and cloned voice.
The destination has an empty greeting and instructions to continue the exchange.

Observed user wording, language, code switching and turn length inform limited
style updates during the call. These are evidence-backed prompt adaptations,
not weight training, verified personality or complete professional judgment.
Session timing records distinguish eligibility, clone creation, synthesis,
handoff dispatch and provider-confirmed cloned speech. No elapsed-time guarantee
or ten-second cloning claim is made.

Normal End closes the call and preserves the voice/sample for the configured
retention period. Delete withdraws consent, blocks local access immediately,
purges local audio/transcript evidence and attempts external call/voice cleanup.
External deletion failures remain visible and retryable. A scoped HttpOnly,
Secure, SameSite=Strict cookie restores only this browser's private session;
the browser stores the session ID, never a bearer token, in localStorage.
The default retention is seven days, with automatic cleanup. Losing the cookie
or clearing browser data loses this anonymous access; there is no account-based
recovery in this prototype.

## Configuration and deployment

Reuse the existing Render Docker service and persistent disk. Credentials stay
in Render secret storage. Merge configuration updates; never replace secret
values with blanks. Enable explicitly:

```dotenv
RANEEN_BECOMING_ENABLED=1
RANEEN_BECOMING_MIN_SPEECH_SECONDS=30
RANEEN_VAPI_TEMPLATE_ID=<existing accessible Raneen/Sura assistant UUID>
RANEEN_BECOMING_RETENTION_DAYS=7
RANEEN_VAPI_VOICE_MODEL=eleven_multilingual_v2
ELEVENLABS_TTS_MODEL=eleven_multilingual_v2
```

Existing Vapi and ElevenLabs accounts must be connected to each other for Vapi
to render a private cloned voice. `/readyz` reports configuration presence and
revision, not entitlement. A consent-scoped `/preflight` checks read access to
the configured template and ElevenLabs account without a paid create. Provider
errors expose only sanitized allowlisted diagnostics.

Admission defaults bound three sessions per client per hour, twenty per day,
two active calls, and ten minutes per call. Operations are claimed atomically;
concurrent processors and browser refresh cannot create a duplicate call/clone.
An interrupted or timed-out create becomes outcome_unknown and is not blindly
retried. The existing production assistant and phone routing are untouched.

## Verification and acceptance

Fresh pre-change baseline: 292 Python tests passed and PCM encoder checks passed.
Full Python regression: 325 passed in 125.46 seconds before the final cancellation
regressions. Existing deprecation warnings remain. The expanded focused
backend/hosted set passes 36 tests, including concurrent-create, unknown-result
recovery, retention, secret-free relay and cancellation before provider dispatch.
All 20 new JavaScript checks, 37 existing capture checks and the PCM encoder
checks pass. Actual browser regressions run in CI with synthetic media/providers.
Automated fixtures use synthetic speech and mocked providers, including actual
ffmpeg decoding. They cannot prove live account entitlement, voice quality,
real handoff audio, or physical Arabic microphone acceptance.

The release record must include final test counts, browser/CI results, deployed
revision, live preflight results and any real measured operation timings. If no
real microphone clone trial was performed, those timings and same-call audible
acceptance remain unmeasured. Do not mark that acceptance gate complete.
