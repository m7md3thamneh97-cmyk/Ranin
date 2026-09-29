# Guided teaching v0.2 — implementation and activation

## What changes

The default website is now a guided Teach -> Review -> Test flow. The original interface remains at `/advanced`. The current hosted launcher composes the existing authorization/storage guards with `studio.teaching`; existing accounts, recordings, tables and credentials are not replaced. New SQLite tables are additive. The Render plan, disk, location, Blueprint and original Vapi assistant are unchanged.

A teaching session uses authored scenarios: the contributor gives a response, explains the relevant cue, describes when the answer would change, and verifies their words. It is not yet an autonomous AI interviewer. The owner approves examples separately.

## Exact adaptation mechanism

Build a behavior pack from one consenting profile's approved, verified training examples and approved comparisons. The pack is a versioned snapshot of dialect/style and context -> demonstrated response/action/cue/change-condition examples. Its system prompt is inspectable. Held-out scenarios, pending/rejected examples, withdrawn material and recordings are not inserted into it.

At inference, a text model reads that system prompt plus the new caller message. This is in-context example adaptation, NOT fine-tuning or model-weight training. The initial implementation supports at most 32 reviewed items and 28,000 serialized characters per pack; larger libraries need retrieval. It does not silently truncate. There is no evidence of improved behavior until new cases are actually tested by native speakers.

Rejecting an included example, withdrawing consent or changing source style invalidates subsequent local use of that pack. Previously copied prompts and Vapi assistants cannot be recalled automatically: remove them separately. No commercial voice licence is inferred from prototype consent.

## Provider activation (server-side only)

Set these in this existing Render service's Environment, save and redeploy, then refresh the studio. Never enter keys into Git, URL parameters, browser source, teaching responses or ChatGPT.

- `OPENAI_API_KEY`: optional automatic transcription and two-answer text comparisons. Requires a separately funded API account. A transcript is a draft until the contributor verifies it.
- `VAPI_API_KEY`: private server-side Vapi API key.
- `RANEEN_VAPI_TEMPLATE_ID`: UUID of an existing assistant with a TTS voice the owner is permitted to use. No access token belongs in this value.
- Optional `RANEEN_TRANSCRIBE_MODEL` (default `gpt-transcribe`) and `RANEEN_TEXT_MODEL` (default `gpt-4.1-mini`). Model access must be verified against the actual account.
- Optional `RANEEN_AI_DAILY_CALL_LIMIT` (default 40 provider operations per UTC day; 0 disables). This is a local request limit, NOT a dollar cap, and does not cover Vapi calls started outside the studio. Configure provider billing controls separately.

No external call occurs just because a key is configured. Per-operation permission and a deliberate button click are required. Fixed provider hosts, bounded timeouts, no redirects, sanitized error messages and no automatic retries are used. Failed attempted operations count against the daily limit.

## What happens in Test

1. Build the reviewed pack. No provider receives it yet.
2. Inspect the exact prompt and source example IDs.
3. With explicit approval, compare a new message using the same model/style with and without the examples. This is a single-turn text comparison, not a voice or conversation benchmark.
4. With separate approval, create a NEW Vapi assistant using the compiled prompt and a whitelist of the template's existing voice/transcriber settings. The original Sura is not patched. No business tools, server URLs, transfer destinations or phone numbers are copied. No call is placed. Test recordings are disabled in its artifact plan. Use Vapi's dashboard to test audio; the studio does not yet embed live calls.

The Vapi path requires a modular TTS voice, not a realtime speech-only template. Creation is recorded once per pack; uncertain outcomes are blocked from automatic retry to avoid duplicates. A created assistant is not proof that its voice is natural, understands Arabic reliably, or follows the pack in practice.

## Not complete

Employee access, live AI interviewing, multi-turn scoring, voice cloning, acoustic/dialect fine-tuning, retrieval for a large library, production inventory/CRM integrations and production promotion/rollback are not enabled. The voice dataset remains available through the older advanced export flow. Do not collect employee/customer production data yet. This release preserves the owner-only deployment boundary.

## Verification record — 29 September 2026

74 local backend/staging tests passed (48 original archive tests plus 26 new teaching tests), together with the WAV encoder check and an offline Chromium DOM/API workflow covering onboarding, typed teaching, review, pack creation, missing-key gates and mobile layout. Vendor responses in backend tests were mocked. The previously committed Render migration tests remain unchanged in the repository; they were not included in this local 74-test run.

The execution environment blocked browser navigation and live microphone testing. Real OpenAI/Vapi calls, authenticated hosted workflows and persistence across a hosted restart have not been validated in this release. Deployment status must be verified separately in Render; this document does not assert a live deployment.

## Source references used for implementation

- OpenAI speech-to-text: https://developers.openai.com/api/docs/guides/speech-to-text
- OpenAI text model: https://developers.openai.com/api/docs/models/gpt-4.1-mini
- Vapi assistant creation: https://docs.vapi.ai/api-reference/assistants/create

