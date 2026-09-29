# Raneen Teaching Studio

Private, owner-only staging prototype for collecting demonstrated judgment, dialect, wording, accent and delivery. This is a data-collection and review application, **not a trained replica**.

## Available

- Sixteen authored situations, natural recorded or typed responses, decision cues, alternatives and change conditions.
- Consent-scoped profiles, original mono PCM WAV, exact manually verified transcripts, basic signal checks.
- Human approval/rejection, comparison coaching, separate behavior/voice exports and held-out scenario families.
- Hosted owner authentication, canonical HTTPS origin, dedicated persistent storage, no external AI providers.

AI interviewing, automatic transcription, voice cloning, model adaptation and Vapi integration are not connected. Hosted employee account creation/authentication is blocked. Use fictional situations and the owner's own test material only; do not upload employee or customer production data.

## Local use

```sh
python -m venv .venv
# Activate the virtual environment for your operating system.
python -m pip install -r requirements-dev.txt
python run.py
```

The local launcher listens only on localhost:8765 and prints a first-run owner token in your local terminal. Keep it private. Hosted deployments use the separate `serve.py` launcher through the Docker entrypoint and never print the owner credential.

## Tests

```sh
python -m pytest -q
node tests/wav_encoder.mjs
```

The source passed 48 backend/staging checks and the PCM encoder check before upload. All 16 runtime/configuration blobs were verified byte-for-byte against the prepared v0.1.1 source. This does not verify an actual microphone, live hosting, backups or employee readiness.

## Railway staging

Use the repository-root Dockerfile and railway.json. Attach a dedicated persistent volume at `/data`, set PORT=8080 and RANEEN_DATA_DIR=/data, generate a domain routed to 8080, and set RANEEN_PUBLIC_ORIGIN to its exact HTTPS origin. Generate RANEEN_ADMIN_TOKEN from at least 32 random bytes using URL-safe encoding in the hosting secrets interface. No default credential is provided. Railway must supply RAILWAY_VOLUME_MOUNT_PATH for the real mounted volume; do not spoof it. Startup refuses absent storage, weak credentials or an invalid origin.

The owner signs in using RANEEN_ADMIN_TOKEN from the service's Variables panel. Never put it in a URL, GitHub, logs, public messages or browser source. One replica only. Data are stored in SQLite and WAV files on the mounted volume. The initial small volume is for limited owner testing, not a production corpus. Keep separate backups and validate restore procedures before collecting valuable data.

Source provenance: Raneen_Teaching_Studio_v0.1.1_Staging.zip, SHA-256 `89fe321d5e4a6ac9c6d68b8046d834cd9960a272a5c3a6d2c680e46a8d2c314e`. Additional original planning documents and browser smoke helpers remain in that downloadable archive.

A Git commit or configured domain is not deployment proof. Verify Railway's terminal SUCCESS, HTTPS page, healthcheck, access restrictions, owner workflow and persistence before reporting readiness.
