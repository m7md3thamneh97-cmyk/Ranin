# Raneen

An owner-only voice teaching studio with an Arabic/English conversation interface, trusted spoken evidence, versioned learning, practice/correction/retry, and a private personalized voice preview. The application retains FastAPI, browser JavaScript, SQLite and the existing Render/Docker launchers.

This development branch composes `work/conversation-studio-frontend` with the learning backend. It preserves the existing enrollment, isolated microphone chunks, provider recovery, and private Vapi call flow. It is a review branch; it has not been deployed or accepted in a real Arabic microphone conversation.

## Run locally

```sh
python -m venv .venv
# Activate the virtual environment for your operating system.
python -m pip install -r requirements-dev.txt
python run.py
```

Open localhost:8765. The launcher creates a local owner credential on first launch. Hosted containers use `serve.py` and hosting secret storage. `/enroll` is the guided conversation; `/studio` retains the evidence/review interface.

Both provider feature flags default to zero: `RANEEN_VOICE_ENROLLMENT_ENABLED` controls enrollment and `RANEEN_LEARNING_STUDIO_ENABLED` controls the new learning-provider adapters. Offline `local_rules` is a finite development engine. It demonstrates correction/version behavior without claiming general reasoning, voice cloning, or model weight training. Missing provider configuration never produces a Ready clone.

Operator setup and separately approved live acceptance: [Gate B operator guide](docs/GATE_B_OPERATOR.md). Configuration names without secrets: [.env.example](.env.example). Hosted employee access remains disabled. Use one server process and the existing persistent disk; this branch creates no hosting resources.

## Connected behavior

- Disclosed enrollment consent, resumable checksum-acknowledged microphone chunks and trusted provider-side transcript/confirmation evidence.
- Stable server-side enrollment/learning binding; browser text submissions cannot impersonate trusted spoken evidence.
- Structured observations, tentative hypotheses, explicit corrections, targeted next questions and immutable profile versions with source provenance.
- Practice and retry using the current learned context, while original attempts and snapshots remain unchanged. Customer practice input is excluded from teaching evidence.
- Provider voice verification, fresh private previews, explicit voice approval and behavior-only updates that reuse the approved clone.
- Durable provider operations, uncertain-outcome reconciliation, revocation and scoped external cleanup. The composed app uses bounded server-created preview rooms and does not expose a reusable Vapi public key.

No CRM, market feeds, production phone routing, employee rollout or psychological identity replication is included. Fictional business examples are separate from current business facts and higher-priority policy.

## Verify

```sh
python -m pytest -q
node tests/wav_encoder.mjs
node tests/enrollment_capture.mjs
node tests/conversation_events.mjs
python tests/learning_backend_contract.py --backend-root .
python -m compileall -q studio
```

For the synthetic browser journey, install `requirements-browser.txt`, run `python -m playwright install chromium --only-shell`, then `python tests/enrollment_dom.py`. The browser harness uses synthetic media and mocked providers. It does not establish physical microphone, live provider or Arabic listener acceptance.

See [backend API](docs/BACKEND_API.md), [delivery evidence and limitations](docs/BACKEND_DELIVERY.md), [voice backend](docs/VOICE_BACKEND.md), and [provider contracts](docs/PROVIDERS.md). Schema upgrades are additive and checksummed. Backups and restoration still require operator verification before live use.
