# Render migration — owner-only staging

Prepared 2026-09-29. Source base: `a5e36e35eccd7da62ab0019a13e6c8c6cdec8ad1` in `m7md3thamneh97-cmyk/Ranin`.

## Scope and current status

This is a migration candidate, not a live service. The hosting connection and any account-required billing/repository authorization must complete before resource creation. No Render service, disk, deployment, provider job or hosting charge was created while preparing this change. Railway and `main` are left unchanged. No employee or customer data are involved.

## Provisioning contract

Use only one provisioning route. Prefer authenticated Render tools after connection; the README's Deploy to Render button is an alternative preconfigured review flow. Check the workspace for an existing matching service before creating a new one. Do not change a paid workspace plan or provision a second service accidentally.

- Repository: private `m7md3thamneh97-cmyk/Ranin`.
- Branch: `deploy/render-staging`; Dockerfile `./Dockerfile`; build context `.`.
- Service: `raneen-teaching-studio-staging`; Docker web service; Frankfurt; one instance.
- Compute: `starter` (legacy alias of `0.5c-512mb`), $7/month at preparation.
- Disk: `raneen-studio-data`, 1 GB at `/var/data`, $0.25/month at preparation.
- No paid workspace upgrade. Taxes and additional usage are excluded from that baseline.
- `PORT=8080`, `RANEEN_DATA_DIR=/var/data`, `RANEEN_VOLUME_MOUNT_PATH=/var/data`.
- `RANEEN_ADMIN_TOKEN`: let Render generate it via `generateValue: true`. Never commit or print it. For an API-only deployment, generate at least 32 bytes using a cryptographically secure generator and set it through the authorized secrets interface.
- `RENDER_EXTERNAL_URL`: supplied by Render; do not invent an app hostname. Used as the canonical origin unless an explicit reviewed `RANEEN_PUBLIC_ORIGIN` is set.
- Health check `/healthz`; automatic deployments off; previews off. Runtime initialization only; disks are not available at build/pre-deploy time.

The declared mount-path variable is application configuration, not proof of a disk. The launcher checks the actual exact mounted filesystem and rejects memory/overlay mounts on Linux. Verify the attached persistent disk in Render as well. Missing disk configuration is a hard failure; do not remove this check to make a deployment green.

Render's generated secrets use standard base64, including `+`, `/`, and padding. The launcher accepts this format without altering the value, as well as existing URL-safe credentials. It stores only the credential hash in the database. Format checks do not measure entropy; use generated secrets, not a password chosen by a person.

## Validation actually executed

- Original baseline: `python -m pytest -q` — 48 passed.
- Render migration: same command — 89 passed, using the pinned runtime/dev versions already available in the execution environment.
- `node tests/wav_encoder.mjs` — passed; synthetic PCM only, no live microphone.
- Original archive's `tests/dom_smoke.py` — passed (login, profile, consent, text demonstration, review, preference coaching, mobile-width layout, no JS exceptions).
- Original archive's `tests/browser_smoke.py` — blocked at browser navigation by `net::ERR_BLOCKED_BY_ADMINISTRATOR`; does NOT establish a microphone/application failure or a successful full browser test.
- Docker CLI unavailable in the execution environment: container build not run locally.
- Official Blueprint schema download was attempted but blocked by container DNS. YAML structure and the required resource constraints were checked locally; authenticated Render semantic validation is still required.

Changed `serve.py` covers Render origin resolution, standard-base64 generated secrets and portable mounted-disk checks. New tests cover invalid origins, conflicting/unmounted paths, bind mounts, ephemeral/read-only mounts, ports, secrets, owner-only access, cross-origin rejection and restart persistence using temporary test data. All original tests continue to pass.

## Required hosted acceptance checks

1. Read back the correct branch/commit, paid-compute size, single instance, attached disk, healthcheck and environment variable names without exposing secrets.
2. Observe an actual successful/live deployment and inspect bounded build/runtime logs. A queued/triggered response is not success.
3. GET `/healthz` returns 200 and `{"status":"ok"}`; root loads over HTTPS; unauthenticated `/api/me` returns 401. No data is public.
4. Sign in privately, create a test profile/consent, save a short test recording, play it back and export an approved test item. Verify microphone behavior on a real device/browser.
5. Restart/redeploy the same service, confirm the test record/audio remain and no new owner token was silently created. Remove the test data afterward.
6. No employee invitations or production recordings until access/retention/backup/restore and consent handling are reviewed.

Only after these checks may a live app link be reported as working. If billing, GitHub access or connector permission is required, report the exact blocker without weakening security or making the repository public.

## Official references checked during preparation

- https://render.com/docs/blueprint-spec
- https://render.com/docs/environment-variables
- https://render.com/docs/configure-environment-variables
- https://render.com/docs/disks
- https://render.com/docs/health-checks
- https://render.com/docs/compute-plans
- https://render.com/docs/deploy-to-render
- https://render.com/pricing
