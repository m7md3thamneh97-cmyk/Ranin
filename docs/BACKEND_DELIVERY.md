# Integrated backend delivery

This implementation composes the learning backend requested in issue #11 with `work/conversation-studio-frontend` (`3057f01570e74a2347d15c68f99d066070b8968d`). That branch contains the newer Render/enrollment runtime; the supplied learning-foundation commit was older. Existing guided capture, enrollment recovery, clone verification and private-call controls are preserved.

## Implemented

Trusted enrollment evidence maps to an owner-authorized learning profile/session and a durable event cursor. Spoken demonstrations produce provenance-linked observations and hypotheses. Confirmed corrections take precedence in immutable profile versions. The targeted question planner, practice and retry run through the trusted voice sideband. Customer practice turns cannot teach the profile. Browser text endpoints cannot forge linked enrollment evidence, permissions or corrections.

The actual enrollment clone path retains provider verification, fresh synthesized Arabic previews, explicit approval, durable operation reconciliation and clone reuse after behavior-only updates. Generic development transport and clone endpoints are disabled in the composed runtime so they cannot bypass its bounded private preview calls. The standalone development backend also records uncertain transport creation and cleanup custody without persisting provider credentials or prompts.

Concrete offline M1: the first response asks which project the caller is comparing. The spoken correction “No. Ask their budget first before anything else” creates a new rule version; retry asks “What is your budget range?” The original attempt and snapshot stay unchanged. This establishes correction flow, not general judgment or vocal fidelity.

## Verification

The newer unmodified baseline passed all 292 Python tests and the PCM encoder before composition. The final composed suite passed all 507 Python tests in 148.21 seconds, with one dependency deprecation warning. All 44 Node capture/recovery/conversation tests passed, as did the PCM encoder, frontend learning-state/version/event-cursor contract, Python compilation and whitespace checks. Tests use synthetic fixtures and mocked network providers.

Browser prerequisites were inspected. Playwright is installed, but no Chromium executable exists here. Browser download attempts returned truncated archives. The actual DOM harness fails at browser launch; this is a verification limitation, not a passing browser result. CI includes Chromium installation and the synthetic browser journey.

## Live and release boundary

No hosting resource, deployment, credential, production assistant, phone assignment or real recording was changed. Repository documents identify the existing Render staging service and URL; its current live health and credentials were not verified by this implementation. Earlier Railway observations do not describe the newer Render application's state.

Provider integrations are off by default. Adapters and composed workflows are implemented and tested with mocks; real provider tests and a human-accepted 20–30-minute Arabic microphone session remain unverified. Follow `docs/GATE_B_OPERATOR.md` only after a separately approved staging run with consent and an agreed spending allowance. Do not mark a user Ready because mocked tests passed. Do not merge or deploy this review branch automatically.

Production telephony, CRM, live market feeds, invited employee accounts, scalable retrieval, backup/restore acceptance and commercial licensing are later work. New custom-LLM SSE produces a completed response chunk; incremental model-token streaming is not claimed.
