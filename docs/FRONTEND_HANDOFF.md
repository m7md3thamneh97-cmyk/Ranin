# RAN-FE-01 — Conversation studio frontend

Branch: `work/conversation-studio-frontend`.
Owner scope: browser presentation, session UI, semantic event adapter, integration reads, browser tests.

## What is implemented

- One consistent conversation workspace surrounding teaching, voice review, practice, and spoken corrections. The existing consent and microphone-check steps stay explicit.
- A primary conversation action; secondary history and workspace settings in accessible dialogs; advanced review remains available.
- Arabic / English layouts, real RTL placement, narrow-phone layouts, keyboard focus and reduced-motion support.
- A bounded, escaped conversation transcript. Final provider turns replace partial turns; duplicate final events cannot overwrite them. Reconnects retain display history and namespace provider item IDs.
- Speaking/listening/thinking indication driven by actual realtime events. The decorative orb and voice illustration are not acoustic measurements.
- Server-confirmed learning counts, an optional structured learning panel, immutable model-version display, explicit voice preview/approval, and a keep-teaching action that leaves the voice unapproved.
- Current audio queue, provider outcome recovery, microphone release, consent withdrawal, Vapi/Daily frame isolation and three-sample approval remain intact.
- Tokens remain memory-only in the enrollment app. No provider key, transcript or audio is persisted in browser storage.

## Critical source reconciliation

`main` and `mvp/learning-engine-foundation` were behind the working deployment. The frontend includes current `deploy/render-staging` baseline `ca2fc3382a26c114e2783b3dc00e805854599254` plus the architecture document. Its recent saved-audio and provider-upload fixes must not be discarded.

The active RAN-BE-01 checkout on `mvp/learning-engine-backend` was inspected read-only. It adds `studio/backend.py` and a profile-based teaching engine but does not yet contain the current enrollment runtime. Composition is required before this can be called an integrated M1 release. Do not replace the working voice journey with the older collection-only app.

No backend Python files were edited in this frontend change. No deployed branch or service was changed. No real provider call was made.

## Exact API alignment

`studio/static/learning-api.js` was checked against the active backend's actual `studio/backend.py` and `LearningEngine.learning_state`, using a temporary synthetic database:

| Read | Expected response |
| --- | --- |
| `GET /api/profiles/{profile_id}/learning-state` | `profile_id`, `hypotheses[]`, `versions[]`, optional `profile_version` |
| `GET /api/sessions/{session_id}/events?after={cursor}&limit=100` | `items[]`, integer `cursor` |

Hypotheses use `state`, `category`, `key`, `value`, `evidence_count`, and `conflict`. Tentative or conflicting observations are never rendered as human-confirmed facts. Provider transcripts are display signals only; they do not create learning or confirmations from the browser.

### Remaining backend join

The enrollment session and teaching session are different identities. **The frontend must never guess they share an ID**, fabricate consent, create an unbound profile, or replay provider transcripts as human-verified evidence.

After composing the two runtimes and bridging the existing trusted audio evidence on the server, return the following additive field from the enrollment `GET /api/enrollment/sessions/{id}/journey` (preferred) or `/workflow` response:

```json
{
  "learning": {
    "contract": "raneen-backend-v1",
    "profile_id": "actual-authorized-contributor-profile-id",
    "session_id": "actual-linked-teaching-session-id"
  }
}
```

Those IDs must resolve to the same authenticated, consenting owner and remain stable across reconnects. The frontend automatically enables its learning reads when this binding is present. Without it, the current working voice flow remains available and only actual enrollment counts are shown. No invented hypotheses or percentage scores are substituted.

The adapter deduplicates overlapping polls, validates profile identity and monotonic event cursors, and drops late results after a binding/session change. Withdrawing consent or signing out clears the connection.

Backend ownership remains:

1. Compose `install_backend` with `studio/runtime.py` / current enrollment modules.
2. Bind enrollment sessions to contributor profiles and teaching sessions with appropriate explicit consent scopes.
3. Bridge trusted provider-side audio turns and confirmed evidence with stable source IDs; do not learn from simulated customer turns.
4. Connect the new question planner, simulation, correction and retry engine to the voice runtime. The frontend preserves the existing spoken-correction/voice-reuse path, but the new M1 simulation engine is not claimed connected until this is done.
5. Keep existing enrollment voice IDs separate from the new `voice_versions` IDs until the backend explicitly reconciles them. The frontend never calls new approval/rejection APIs with an old enrollment ID.

This binding is a requested integration contract, not a claim that the backend already emits it. The backend task was notified in issue #11.

## Verification

Commands:

```sh
python -m pytest -q
node tests/wav_encoder.mjs
node tests/enrollment_capture.mjs
node tests/conversation_events.mjs
python tests/enrollment_dom.py
python tests/learning_backend_contract.py --backend-root /path/to/RAN-BE-01-checkout
```

The synthetic browser suite exercises the actual FastAPI routes and CSP with fake media and strict provider doubles. It covers Arabic/English desktop/mobile, sign-in, consent, microphone denial/cancellation/recovery, saved chunks, escaped partial/final transcripts, active-speaker display, history/settings dialogs, voice approval deferral, three playable samples, approval, bounded Vapi frame calls, spoken correction reusing the voice, provider rejections, and feature-off withdrawal.

Screenshots in `docs/frontend/` are synthetic UI evidence, not a live service, real voice-clone result, or native-listener acceptance.

Current provider event documentation checked: https://developers.openai.com/api/docs/guides/realtime-conversations and https://developers.openai.com/api/docs/guides/realtime-vad . The current WebRTC transport stays unchanged; server-side VAD handles interruption.

## Release boundary

This PR is reviewable frontend source. `AGENTS.md` requires separate operator approval for a deployment or release-branch change. The existing service stays unchanged. Full M1 acceptance and real microphone/provider quality testing remain integration/acceptance work; mocked tests do not establish them.
