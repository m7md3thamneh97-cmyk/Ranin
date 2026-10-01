# Raneen platform build plan

Owner: Mohammad. Updated 30 September 2026.

## Product outcome

A contributor signs in, understands and gives consent, checks the microphone,
and teaches Raneen through a guided conversation. Raneen then prepares an
evidence-backed response profile and a private voice clone. The contributor
listens to newly synthesized speech, tests an identified AI agent, and corrects
it by speaking. Ordinary teaching never requires typing a prompt, editing a
transcript, exporting audio, or obtaining provider credentials.

The first release serves one owner. Employee invitations and commercial use
follow separate acceptance gates. A 20–30 minute interview is a target, not a
promise of voice quality, complete judgment capture, or processing time.

## Source and evidence

This increment starts from published commit
`f41d394536d4636f35557e475fea3afdfa1600b3`, a descendant of the Render code
baseline `8c62b29b351da1531b2f82aef06f72fcef098e8c`.

Work branch: `work/rve-guided-platform-v1`.

The previously reported remediation commit `294576e79c27aa51842daa18feeedfa2960c3d51`
and its task-local patch were not available for inspection. This branch contains
new, independently reviewable changes. It does not incorporate or validate that
missing patch. If recovered, compare the patch against this branch before
applying any part of it.

The fresh pre-change baseline was 123 passing Python tests, 173 deprecation
warnings, and passing Node WAV encoder assertions. Those tests used synthetic
fixtures and mocked providers, not a microphone or a real clone.

## Journey and interface rules

1. **Welcome or resume.** State the outcome, show the saved interview, and offer
   one obvious next action. Restoring a saved session does not ask for the same
   consent again.
2. **Consent.** Explain recording, external processing, private voice cloning,
   and private AI testing in plain language. Keep required scopes explicit.
3. **Microphone check.** Request access only after a click. Check the microphone
   locally, recommend headphones, and explain how to recover from denial.
4. **Interview.** Ask one question at a time. Show microphone state and saved
   progress. Put transcripts and diagnostics behind optional details. Pause
   must stop local capture promptly, including during a slow upload.
5. **Prepare and review.** Show actual server-owned job states. Ask for more
   speech when necessary. Obtain spoken confirmation of learned patterns and
   human approval of fresh synthesized samples.
6. **Test and improve.** Start one bounded AI preview. Let the contributor teach
   a correction by voice, confirm the change, and retest using the same voice.

Arabic is the initial interface language, with a persistent English choice.
Use correct right-to-left layout, visible keyboard focus, meaningful labels,
at least 44-pixel action targets, adequate contrast, and reduced-motion support.
Do not infer a speaker's dialect merely from the interface language.

Proposed usability acceptance: zero typed training fields; a newcomer reaches
the first interview utterance within two minutes of sign-in on the normal
consent/permission path; pause and resume need no engineering assistance; each
error gives a concrete recovery action. These are targets to measure with
people, not results already achieved.

## Engineering sequence

| Milestone | Deliverable | Acceptance before promotion |
| --- | --- | --- |
| M1 Guided owner interview | Arabic/English journey, owned-session recovery, microphone check, explicit saved/unsaved status, retained retry queue, prompt local stop | Auth/ownership regressions, deterministic recording and network-failure tests, browser/keyboard/mobile review; physical-microphone acceptance remains separate |
| M2 Reliable processing foundation | Additive versioned migrations; durable jobs, leases, operation and provider-artifact ledger; reservations before external calls; consent grant epochs | Concurrent requests admit one operation; restarts reconcile; timeouts retain unknown outcomes; revocation blocks all later use and schedules all-version cleanup |
| M3 Trustworthy audio and learning | Decoded audio and bounded private storage; final acknowledged sequence; clean sample selection; server-trusted transcript events; spoken confirmation and supersession | Invalid audio cannot count as clean speech; gaps prevent finalization; unsupported evidence cannot enter a behavior version; corrections supersede prior confirmed evidence |
| M4 Voice and agent preparation | Versioned voice, behavior and agent bindings; fresh decoded synthesis; contributor approval; bounded server-created preview | No ready state from a provider ID alone; verification remains enforced; one preview admission, duration and budget bounds; original Sura untouched |
| M5 Spoken improvement loop | Corrections captured in context, confirmed, compiled to a new behavior version, and retested using the existing voice | Old versions remain traceable; no unnecessary clone creation; same held-out cases can compare before and after |
| M6 Controlled owner acceptance | Separately authorized funded staging test with consented owner audio | Real 20–30 minute Arabic interview, restart/resume, novel synthesis, agent test, spoken correction, interruption and revocation evidence |
| M7 Employee platform | Individual accounts and invitations, tenant/role isolation, reviewed consent and retention, quotas, backup/restore, operator tools | Cross-user isolation and deletion tested; operational recovery demonstrated; native-listener acceptance for each supported dialect |

M1 is the current implementation assignment. It is not a declaration that Gate
A or the whole product is complete. The guided UI intentionally keeps
preparation and agent testing unavailable while M2–M4 are incomplete. Legacy
provider endpoints are not made safe merely by hiding their buttons: keep the
enrollment feature off outside controlled development until those server-side
gates pass.

## Architecture and ownership

Keep the current FastAPI application, browser JavaScript, SQLite, private audio
directories, and single-process deployment for the first owner test. Separate
modules by responsibility before adding services. Introduce a versioned
migration ledger before schema growth. Use a small durable job runner on this
host first; do not provision infrastructure merely to complete a diagram.

The backend owns consent, session state, admission, evidence provenance, job
state, artifact ownership, billing reservations and access to playback. The
browser owns microphone permission, visible live state and a bounded retry
buffer. A browser status message cannot certify provider completion.

Store distinct immutable voice, behavior and agent versions. An agent binds a
specific voice, behavior profile, model configuration and policy version.
Confirmed demonstrations may guide behavior; current business facts and action
authorization belong to separate verified sources. Initial personalization
uses selected demonstrations and retrieval, not a claim of model-weight training.

Every external create has a durable operation record and reservation before
dispatch. Recheck consent after provider responses as well as before calls. A
late-created artifact after revocation enters cleanup, not the usable inventory.
An ambiguous timeout remains unknown until reconciled; do not assume a timeout
means nothing was created. Inventory every artifact version so revocation can
address earlier as well as currently active resources.

## Audio and recovery contract

Capture only the contributor microphone, separately from AI playback. Recorded
duration reported by the client is not validated clean speech. Decode and check
samples before making quality or cloning-readiness decisions in M3. Use bounded
uploads, stable sequence numbers, checksums and explicit acknowledgements.

M1 retains failed chunks only in the current page's memory, with an explicit
limit and automatic capture pause under backpressure. A retry uses the same
bytes and sequence. Reloading or closing the page can lose unacknowledged audio;
the interface must disclose this and protect navigation when possible. Do not
claim to recover an encoder tail that the browser never delivered. Saved server
chunks remain the source for reopening a session.

Stop local media before awaiting server hangup, upload completion or revocation.
Keep provider hangup uncertainty visible. Disabling enrollment must still allow
the authenticated owner to inspect, stop and revoke existing work.

## Validation and release

Run the following from the repository with synthetic fixtures and no production
provider credentials:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
node tests/wav_encoder.mjs
node tests/enrollment_capture.mjs
node --check studio/static/enroll.js
```

Local application: `python run.py`, then open `http://127.0.0.1:8765/enroll`.
The local launcher issues its temporary owner credential. Never commit or share
that credential. With enrollment disabled, the app must remain honest about
availability. Run the synthetic tests to exercise enabled paths without funded
provider calls. Enabling a live interview is a separate operator action.

Keep four evidence levels explicit: source implemented; mocked/synthetic tests
passed; real provider test passed; physical-microphone conversation accepted by
a human listener. Record exact source revision, commands, timestamps and
limitations for each. No paid calls are authorized by a green mock suite.

Before owner staging acceptance, agree on spending allowance, retention,
private evidence storage, supported browsers and held-out Arabic cases. Judge
voice identity, dialect/wording, question melody, numbers/negation, interruptions
and decision consistency separately. Compare personalized and untaught behavior
on the same cases. Do not use the training dialogue as the only evaluation set.

Releases require an exact tested revision and an explicit operator approval.
Keep auto-deploy off, preview creation off, and the feature off by default.
Take a restorable database/audio backup before migrations; test restoration
before collecting employee data. Roll back code and configuration compatibly
with additive schema changes; do not destroy newly collected evidence to roll
back a screen. Revocation and cleanup must remain usable while feature creation
is disabled.

## Immediate follow-up

Review M1, run its browser and real-device acceptance checks, and build M2's
durable operation and artifact foundation next. Do not expose cloning or agent
preview simply because the interview screen now looks finished. No deployment,
paid-resource change, production Sura mutation, PSTN or business integration is
part of this increment.
