# Raneen Teaching Studio

Private, owner-only staging prototype for collecting demonstrated judgment, dialect, wording, accent and delivery. This is a collection/review application, **not a trained replica**.

## Render staging

The `deploy/render-staging` branch contains the Render migration. The Railway-linked `main` branch is unchanged. **A committed Blueprint is not a live deployment.**

[Deploy the configured staging app to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fm7md3thamneh97-cmyk%2FRanin%2Ftree%2Fdeploy%2Frender-staging)

The button opens Render's authenticated review/provisioning flow; it is not the app URL. Use it once, or use the connected Render operator tools, not both. Review existing services before creating anything. Your Render account must have GitHub access to this private repository. Keep repository access scoped to `Ranin`.

`render.yaml` declares one Docker web service in Frankfurt, one 1 GB persistent disk at `/var/data`, port 8080, `/healthz`, and a Render-generated owner credential. Automatic deployments and previews are off. The intended baseline is $7/month compute plus $0.25/month disk, excluding taxes and additional usage. No paid workspace upgrade is needed for this configuration. The legacy `starter` compute identifier corresponds to `0.5c-512mb`.

The owner signs in with `RANEEN_ADMIN_TOKEN` from Render's Environment panel. Never put it in Git, a URL, logs, browser source or chat. Render provides `RENDER_EXTERNAL_URL`; the app uses that exact HTTPS origin. Startup refuses an unmounted data path, an invalid origin, or an invalid owner-secret format. See [deployment and test record](docs/RENDER_DEPLOYMENT.md).

## Available

- Sixteen authored situations with recorded or typed responses, decision cues, alternatives and change conditions.
- Consent-scoped profiles, mono PCM WAV, manually verified transcripts and basic signal checks.
- Human review, comparison coaching, separate behavior/voice exports and held-out scenario families.
- Owner-only hosted authentication and private audio access; no external AI provider calls.

AI interviewing, automatic transcription, voice cloning, model adaptation and Vapi integration are **not connected**. Hosted employee access remains blocked. Test only with fictional scenarios and the owner's own material; do not collect production employee or customer recordings.

## Local use and tests

```sh
python -m venv .venv
# Activate the virtual environment for your operating system.
python -m pip install -r requirements-dev.txt
python -m pytest -q
node tests/wav_encoder.mjs
python run.py
```

`run.py` is loopback-only at localhost:8765 and prints a newly generated local admin token on first launch. Hosted containers run `serve.py` through the existing Docker entrypoint, drop root privileges before serving HTTP, and do not print the owner credential.

Migration verification: 89 backend/staging tests and the WAV encoder passed locally. Offline browser/API integration passed using the smoke helper in the original staging archive. The live browser microphone test was blocked by this execution environment. Docker building and hosted persistence have not yet been verified on Render.

## Storage and operational limits

SQLite and WAV files share the dedicated disk. Use exactly one process/instance. This small disk is for owner testing, not a production speech corpus. Persistence is not a substitute for a tested backup/restore procedure. Deleting local records cannot recall exports, provider copies or backups. A public login page is expected; recording and data APIs require the private credential.

Railway compatibility remains in `serve.py` for existing deployments: it recognizes the platform-provided `RAILWAY_VOLUME_MOUNT_PATH` and `RAILWAY_PUBLIC_DOMAIN`. This migration does not modify Railway resources or deploy this branch there.
