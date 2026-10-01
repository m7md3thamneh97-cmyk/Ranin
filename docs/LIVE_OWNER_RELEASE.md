# Live owner application

The owner has requested deployment of the integrated Raneen application so it can
be tried through one link and improved from direct use. This release reuses the
existing Render web service and persistent disk. It does not change the existing
Sura assistant, phone routing, business integrations or employee access.

## Entry and access

Open the existing Render application at `/enroll`. With `RANEEN_PLATFORM_HOME=1`,
the root URL opens that same guided application. The previous collection screen
remains at `/studio`; existing records and owner access are preserved.

Use the existing private owner access code from the Render environment. Never
put the code in a URL, repository, chat, screenshot or provider request. Enrollment
access stays in page memory; reopening the page requires signing in again.

## Connected owner journey

1. Give the disclosed private-enrollment consent and check the microphone.
2. Speak to the interviewer. Pause and resume while keeping acknowledged chunks.
3. Finish the interview and prepare the voice from server-decoded audio.
4. Listen to fresh synthesized samples and approve the voice before agent use.
5. Try the isolated AI agent in the application, using a bounded server-created call.
6. Return to the interviewer to teach a spoken correction and prepare the next
   response version, reusing the existing voice.

The UI must display unavailable provider configuration, verification requirements,
uncertain operations and insufficient usable audio as actionable states. A
provider voice ID, elapsed interview time, or green synthetic test does not prove
accepted voice quality. Energy screening is not speaker verification or speech
recognition. Real Arabic and physical-microphone acceptance are separate.

## Hosting configuration

Keep one existing Docker instance, the attached `/var/data` disk, automatic
rollouts off, and HTTPS owner-only access. `RANEEN_VOICE_ENROLLMENT_ENABLED=1`
enables the owner workflow; individual provider operations still require their
server-side credentials, consent, state and admission checks.

Required private credentials belong only in Render secret storage:
`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, and `VAPI_API_KEY`. Private cloned voice
access also requires the same ElevenLabs account to be linked in Vapi's provider
integration. A key being present does not prove credit, model access or eligibility.
No reusable Vapi public key is needed by the browser. Never replace existing
secrets with empty values when changing feature flags.

`/readyz` exposes only nonsecret configuration-presence, decoder availability,
coarse disk availability and the deployed revision. It never proves provider
entitlements or makes a provider call. `/healthz` verifies basic application and
database health. The real operation states remain authenticated.

## Data and release recovery

Before a hosted revision changes database structure, the launcher creates a
private SQLite backup under `release-backups/<revision>.sqlite3` on the persistent
disk and checks its integrity. Schema additions retain existing tables and data.
This backup is for database recovery; it is not an offsite backup or a copy of all
audio. Audio remains in its existing private directories.

A rollback must preserve evidence collected since deployment. Do not restore an
old database over newer owner recordings simply to change the interface. Prefer a
compatible code rollback with the additive schema retained. Keep provider cleanup
and authenticated stop/revoke available when new enrollment is disabled.

## Verification record

Fresh baseline before this release: 145 Python tests passed, 261 deprecation
warnings, and PCM WAV assertions passed. Final source revision, full regression
results, CI browser evidence and observed Render deployment belong in the release
pull request. No physical microphone or paid provider operation is exercised by
the automated deployment checks. The owner performs the actual voice-quality trial.
