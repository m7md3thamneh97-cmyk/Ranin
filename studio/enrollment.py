"""Voice-only enrollment vertical slice.

This module is default-disabled and owner-only in hosted staging. It provides:
- explicit enrollment consent separate from the legacy research consent;
- bounded, resumable microphone-only chunks;
- OpenAI Realtime browser session secrets and server-owned interviewer tools;
- confirmed behavior evidence/versioning;
- ElevenLabs IVC creation + fresh Arabic preview synthesis;
- isolated Vapi preview assistants bound to the cloned voice.

No provider call is made unless the feature flag is enabled, the relevant server
credential exists, and the enrollment carries the disclosed scope.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from urllib.parse import urlparse

import httpx
from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .app import now, token_hash, uid
from .enrollment_journey import journey_summary, recovery_summary
from .vapi_errors import rejection_hint
from .enrollment_provider_errors import (
    KNOWN_REJECTIONS, provider_failure, response_diagnostics, retryable_clone, sanitize_diagnostics, stored_failure,
)

FEATURE = "RANEEN_VOICE_ENROLLMENT_ENABLED"
CONSENT_VERSION = "voice-enrollment-v1"
CONSENT_TEXT = (
    "I am the speaker and this is my own voice. For this private internal test I allow "
    "Raneen to record and store my microphone audio, send the required audio/text to "
    "configured AI providers, create one private synthetic voice clone, and create a "
    "private browser test agent using that clone. I understand the agent is AI and is "
    "not me. This permission does not authorize public/commercial impersonation or "
    "customer calls. I can revoke local use; provider cleanup may remain pending."
)
CHUNK_MAX = 256 * 1024
SESSION_MAX = 220 * 1024 * 1024
MIN_CLONE_MS = 60_000
MIN_CLONE_ACTIVE_MS = 30_000
MAX_CLONE_MS = 125_000
MAX_CLONE_ATTEMPTS = 3
MAX_EVIDENCE = 40
OPENAI_BASE = "https://api.openai.com/v1"
ELEVEN_BASE = "https://api.elevenlabs.io/v1"
VAPI_BASE = "https://api.vapi.ai"

PREVIEW_TEXT = {
    "question": "طيب، شو المنطقة اللي في بالك؟",
    "number": "بس أتأكد، ميزانيتك مليون ونص درهم، صح؟",
    "correction": "تمام، فهمت عليك. قلت للشراء، مش للإيجار.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS enrollment_sessions(
 id TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL REFERENCES users(id),
 state TEXT NOT NULL,
 consent_version TEXT NOT NULL,
 consent_json TEXT NOT NULL,
 total_bytes INTEGER NOT NULL DEFAULT 0,
 clean_ms INTEGER NOT NULL DEFAULT 0,
 voice_id TEXT,
 voice_state TEXT NOT NULL DEFAULT 'not_started',
 active_behavior_id TEXT,
 assistant_id TEXT,
 revoked_at TEXT,
 created TEXT NOT NULL,
 updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollment_chunks(
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL,
 sha256 TEXT NOT NULL,
 byte_count INTEGER NOT NULL,
 duration_ms INTEGER NOT NULL,
 mime TEXT NOT NULL,
 role TEXT NOT NULL,
 path TEXT NOT NULL,
 created TEXT NOT NULL,
 PRIMARY KEY(session_id, seq)
);
CREATE TABLE IF NOT EXISTS enrollment_transcripts(
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 item_id TEXT NOT NULL,
 transcript TEXT NOT NULL,
 created TEXT NOT NULL,
 PRIMARY KEY(session_id, item_id)
);
CREATE TABLE IF NOT EXISTS enrollment_evidence(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 source_item_id TEXT,
 kind TEXT NOT NULL,
 payload TEXT NOT NULL,
 status TEXT NOT NULL,
 confirmation_transcript TEXT,
 created TEXT NOT NULL,
 confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS enrollment_behavior_versions(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 payload TEXT NOT NULL,
 digest TEXT NOT NULL,
 created TEXT NOT NULL,
 UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS enrollment_voice_versions(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 provider TEXT NOT NULL,
 provider_voice_id TEXT,
 state TEXT NOT NULL,
 sample_manifest TEXT NOT NULL,
 manifest_digest TEXT NOT NULL,
 created TEXT NOT NULL,
 updated TEXT NOT NULL,
 UNIQUE(session_id, version),
 UNIQUE(session_id, manifest_digest)
);
CREATE TABLE IF NOT EXISTS enrollment_operations(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL,
 op_key TEXT NOT NULL,
 state TEXT NOT NULL,
 provider_id TEXT,
 detail TEXT NOT NULL DEFAULT '{}',
 created TEXT NOT NULL,
 updated TEXT NOT NULL,
 UNIQUE(session_id, kind, op_key)
);
CREATE TABLE IF NOT EXISTS enrollment_realtime_calls(
 session_id TEXT PRIMARY KEY REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
 call_id TEXT NOT NULL,
 state TEXT NOT NULL,
 started TEXT NOT NULL,
 updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollment_schema_versions(version INTEGER PRIMARY KEY, applied TEXT NOT NULL);
INSERT OR IGNORE INTO enrollment_schema_versions VALUES(2, CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS enrollment_voice_approvals(
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id),
 voice_id TEXT NOT NULL,
 approved TEXT NOT NULL,
 PRIMARY KEY(session_id,voice_id)
);
CREATE TABLE IF NOT EXISTS enrollment_preview_calls(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES enrollment_sessions(id),
 assistant_id TEXT NOT NULL,
 state TEXT NOT NULL,
 provider_id TEXT,
 control_url TEXT,
 created TEXT NOT NULL,
 updated TEXT NOT NULL
);
"""

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class StartEnrollment(Strict):
    recording: bool
    external_processing: bool
    voice_cloning: bool
    private_preview: bool
    self_attestation: bool

class TranscriptEvent(Strict):
    item_id: str = Field(min_length=2, max_length=160)
    transcript: str = Field(min_length=1, max_length=6000)

class ToolEvent(Strict):
    call_id: str = Field(min_length=2, max_length=180)
    name: Literal["propose_evidence", "confirm_evidence"]
    arguments: dict
    source_item_id: str | None = Field(default=None, max_length=160)

class ExternalApproval(Strict):
    approve: bool = False

class CloneRequest(ExternalApproval):
    final_seq: int = Field(ge=0,le=10000)
    retry_failed: bool = False

class PreviewRequest(Strict):
    approve: bool = False
    kind: Literal["question", "number", "correction"]
    retry_failed: bool = False

class AssistantRequest(Strict):
    approve: bool = False
    behavior_id: str = Field(min_length=8, max_length=64)

class RevokeRequest(Strict):
    confirm: bool = False


def _enabled() -> bool:
    return os.environ.get(FEATURE, "0").strip() == "1"


def _require_key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HTTPException(503, f"{name} is not configured in hosting secrets.")
    return value


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_public_key() -> str:
    value = os.environ.get("VAPI_PUBLIC_API_KEY", "").strip()
    if not value:
        raise HTTPException(503, "VAPI_PUBLIC_API_KEY is not configured.")
    if len(value) > 512 or any(c.isspace() for c in value):
        raise HTTPException(503, "VAPI_PUBLIC_API_KEY has an invalid format.")
    return value


class ProviderError(Exception):
    def __init__(self, message: str, *, uncertain: bool = False, diagnostics: dict | None = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.diagnostics = sanitize_diagnostics(diagnostics)


class Providers:
    async def openai_realtime_secret(self, api_key: str, safety_id: str, session: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
                r = await client.post(
                    OPENAI_BASE + "/realtime/client_secrets",
                    headers={
                        "Authorization": "Bearer " + api_key,
                        "Content-Type": "application/json",
                        "OpenAI-Safety-Identifier": safety_id,
                    },
                    json={"session": session},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("OpenAI Realtime session creation failed.", uncertain=False) from exc
        if r.status_code >= 300:
            raise ProviderError(f"OpenAI Realtime returned HTTP {r.status_code}.", uncertain=False)
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError("OpenAI Realtime returned invalid JSON.") from exc
        if not isinstance(data.get("value"), str) or not data["value"].startswith("ek_"):
            raise ProviderError("OpenAI Realtime did not return a usable client secret.")
        return {"value": data["value"], "expires_at": data.get("expires_at")}


    async def openai_create_call(self, api_key: str, safety_id: str, sdp: str, session: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10), follow_redirects=False) as client:
                r = await client.post(
                    OPENAI_BASE + "/realtime/calls",
                    headers={
                        "Authorization": "Bearer " + api_key,
                        "OpenAI-Safety-Identifier": safety_id,
                    },
                    files={
                        "sdp": (None, sdp),
                        "session": (None, _json(session)),
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError("OpenAI Realtime WebRTC creation had an uncertain outcome.", uncertain=True) from exc
        if r.status_code >= 300:
            raise ProviderError(f"OpenAI Realtime returned HTTP {r.status_code}.", uncertain=r.status_code >= 500)
        location = r.headers.get("location", "")
        call_id = location.rstrip("/").split("/")[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", call_id):
            raise ProviderError("OpenAI Realtime did not return a usable call ID.", uncertain=True)
        answer = r.text
        if not answer.startswith("v=") or len(answer) > 200_000:
            raise ProviderError("OpenAI Realtime returned an invalid SDP answer.", uncertain=True)
        return {"call_id": call_id, "sdp": answer}

    async def openai_hangup(self, api_key: str, call_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=False) as client:
                r = await client.post(
                    OPENAI_BASE + "/realtime/calls/" + quote(call_id, safe="") + "/hangup",
                    headers={"Authorization": "Bearer " + api_key},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("OpenAI Realtime hangup failed.", uncertain=True) from exc
        # Hanging up a known call is idempotent. A remotely ended call may no
        # longer exist; do not strand its saved interview behind close_unknown.
        if r.status_code in {404, 410}:
            return
        if r.status_code >= 300:
            raise ProviderError(f"OpenAI Realtime hangup returned HTTP {r.status_code}.", uncertain=True)

    async def eleven_clone(self, api_key: str, name: str, files: list[tuple[str, Path, str]]) -> dict:
        opened = []
        try:
            multipart = []
            for filename, path, mime in files:
                handle = path.open("rb")
                opened.append(handle)
                multipart.append(("files", (filename, handle, mime)))
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=False) as client:
                r = await client.post(
                    ELEVEN_BASE + "/voices/add",
                    headers={"xi-api-key": api_key},
                    data={
                        "name": name,
                        "description": "Private Raneen enrollment test voice",
                        "remove_background_noise": "false",
                    },
                    files=multipart,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError("ElevenLabs clone request had an uncertain network outcome.", uncertain=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("ElevenLabs clone request had an uncertain network outcome.", uncertain=True) from exc
        finally:
            for handle in opened:
                handle.close()
        if r.status_code >= 500:
            raise ProviderError(f"ElevenLabs clone returned HTTP {r.status_code}.", uncertain=True,
                                diagnostics=response_diagnostics(r))
        if r.status_code >= 300:
            raise ProviderError(f"ElevenLabs clone returned HTTP {r.status_code}.", uncertain=r.status_code not in KNOWN_REJECTIONS,
                                diagnostics=response_diagnostics(r))
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError("ElevenLabs clone returned invalid JSON.", uncertain=True) from exc
        if not isinstance(data, dict):
            raise ProviderError('ElevenLabs clone returned an invalid result object.', uncertain=True)
        voice_id = data.get("voice_id")
        if not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", voice_id):
            raise ProviderError("ElevenLabs clone result did not contain a valid voice ID.", uncertain=True)
        if type(data.get('requires_verification')) is not bool:
            raise ProviderError('ElevenLabs clone did not return a usable verification state.', uncertain=True)
        return {"voice_id": voice_id, "requires_verification": data['requires_verification']}

    async def eleven_delete_voice(self, api_key: str, voice_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
                r = await client.delete(
                    ELEVEN_BASE + "/voices/" + quote(voice_id, safe=""),
                    headers={"xi-api-key": api_key},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("ElevenLabs voice deletion could not be confirmed.", uncertain=True) from exc
        if r.status_code == 404:
            return
        if r.status_code >= 300:
            raise ProviderError(f"ElevenLabs voice deletion returned HTTP {r.status_code}.", uncertain=True)

    async def eleven_speech(self, api_key: str, voice_id: str, text: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
                r = await client.post(
                    ELEVEN_BASE + "/text-to-speech/" + quote(voice_id, safe=""),
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "text": text,
                        "model_id": os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"),
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError("ElevenLabs speech synthesis had an uncertain network outcome.", uncertain=True) from exc
        if r.status_code >= 300:
            raise ProviderError(f"ElevenLabs speech synthesis returned HTTP {r.status_code}.",
                                uncertain=r.status_code not in KNOWN_REJECTIONS, diagnostics=response_diagnostics(r))
        if len(r.content) < 1000 or len(r.content) > 8 * 1024 * 1024:
            raise ProviderError("ElevenLabs speech synthesis returned unusable audio.")
        return bytes(r.content)

    async def vapi_json(self, api_key: str, method: str, path: str, *, json_body=None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), follow_redirects=False) as client:
                r = await client.request(
                    method,
                    VAPI_BASE + path,
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json=json_body,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError("Vapi request had an uncertain network outcome.", uncertain=method != "GET") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Vapi request failed.") from exc
        if r.status_code >= 500:
            raise ProviderError(f"Vapi returned HTTP {r.status_code}.", uncertain=method != "GET")
        if r.status_code >= 300:
            raise ProviderError(f"Vapi returned HTTP {r.status_code}." + rejection_hint(r))
        try:
            return r.json()
        except ValueError as exc:
            raise ProviderError("Vapi returned invalid JSON.", uncertain=method != "GET") from exc

    async def vapi_delete_assistant(self, api_key: str, assistant_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
                r = await client.delete(
                    VAPI_BASE + "/assistant/" + quote(assistant_id, safe=""),
                    headers={"Authorization": "Bearer " + api_key},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("Vapi assistant deletion could not be confirmed.", uncertain=True) from exc
        if r.status_code == 404:
            return
        if r.status_code >= 300:
            raise ProviderError(f"Vapi assistant deletion returned HTTP {r.status_code}.", uncertain=True)

    async def vapi_end_call(self, control_url: str) -> None:
        parsed = urlparse(control_url)
        if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.vapi.ai') or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ProviderError('Vapi returned an invalid call control URL.', uncertain=True)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(control_url, json={'type':'end-call'})
            if response.status_code >= 300 and response.status_code != 404:
                raise ProviderError('Vapi call closure could not be confirmed.', uncertain=True)
        except httpx.HTTPError as exc:
            raise ProviderError('Vapi call closure could not be confirmed.', uncertain=True) from exc


def install(app):
    store = app.state.store
    with store.db() as db:
        db.executescript(SCHEMA)
    app.state.enrollment_provider = Providers()
    root = store.root / "enrollments"
    root.mkdir(exist_ok=True)
    from .enrollment_evidence import EvidenceService, RealtimeEvidenceBridge
    app.state.enrollment_evidence = EvidenceService(store)
    async def sideband_failed(session_id, call_id):
        await close_realtime_later(session_id,call_id,0)
    app.state.enrollment_sideband = RealtimeEvidenceBridge(store,app.state.enrollment_evidence,on_failure=sideband_failed)


    def actor(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 520:
            raise HTTPException(401, "Sign in first.")
        user = store.one(
            "SELECT id,name,role FROM users WHERE token_hash=?",
            (token_hash(authorization[7:]),),
        )
        if not user:
            raise HTTPException(401, "Invalid access token.")
        return user

    def admin(user=Depends(actor)):
        if user["role"] != "admin":
            raise HTTPException(403, "Owner access required for Gate A.")
        return user

    def gate():
        if not _enabled():
            raise HTTPException(404, "Voice enrollment is not enabled on this release.")

    def own_session(ident: str, user, *, require_active=True):
        row = store.one("SELECT * FROM enrollment_sessions WHERE id=?", (ident,))
        if not row:
            raise HTTPException(404, "Enrollment not found.")
        if row["owner_id"] != user["id"]:
            raise HTTPException(403, "This enrollment belongs to another account.")
        if require_active and row["revoked_at"]:
            raise HTTPException(410, "This enrollment was revoked.")
        return row

    def consent(row: dict, scope: str):
        data = json.loads(row["consent_json"])
        if row["revoked_at"] or not data.get(scope):
            raise HTTPException(409, f"Enrollment does not authorize {scope.replace('_', ' ')}.")

    def op_existing(session_id: str, kind: str, op_key: str):
        return store.one(
            "SELECT * FROM enrollment_operations WHERE session_id=? AND kind=? AND op_key=?",
            (session_id, kind, op_key),
        )

    def op_start(session_id: str, kind: str, op_key: str):
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT revoked_at,voice_id FROM enrollment_sessions WHERE id=?", (session_id,)).fetchone()
            if not session or (session['revoked_at'] and not kind.startswith('cleanup_')):
                raise HTTPException(410, "Enrollment consent was withdrawn.")
            old = db.execute("SELECT * FROM enrollment_operations WHERE session_id=? AND kind=? AND op_key=?", (session_id,kind,op_key)).fetchone()
            if old:
                return dict(old), False
            if kind=='voice_clone' and session['voice_id']:
                raise HTTPException(409,'This enrollment already has its private voice; reuse it.')
            if kind=='vapi_assistant' and db.execute("SELECT COUNT(*) AS n FROM enrollment_operations WHERE session_id=? AND kind='vapi_assistant'",(session_id,)).fetchone()['n']>=6:
                raise HTTPException(429,'The six-version private agent allowance is used.')
            if kind in {'voice_clone','vapi_assistant','voice_preview'}:
                busy = db.execute("SELECT id FROM enrollment_operations WHERE session_id=? AND kind=? AND state IN ('dispatching','outcome_unknown')", (session_id,kind)).fetchone()
                if busy:
                    raise HTTPException(409, "A provider operation is pending or uncertain; reconcile it before starting another.")
            ident = uid()
            db.execute(
                "INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,'{}',?,?)",
                (ident, session_id, kind, op_key, "dispatching", now(), now()),
            )
        return op_existing(session_id, kind, op_key), True

    def op_update(ident: str, state: str, *, provider_id=None, detail=None):
        store.execute(
            "UPDATE enrollment_operations SET state=?,provider_id=COALESCE(?,provider_id),detail=?,updated=? WHERE id=?",
            (state, provider_id, _json(detail or {}), now(), ident),
        )

    def cleanup_summary(session_id: str) -> str:
        unknown = store.one(
            "SELECT COUNT(*) AS n FROM enrollment_operations "
            "WHERE session_id=? AND state IN ('dispatching','outcome_unknown') AND kind IN ('voice_clone','vapi_assistant')",
            (session_id,),
        )["n"]
        rows = store.all(
            "SELECT state FROM enrollment_operations WHERE session_id=? AND kind IN ('cleanup_voice','cleanup_assistant')",
            (session_id,),
        )
        if unknown:
            return "manual_reconciliation"
        if not rows:
            return "none"
        return "complete" if all(x["state"] == "succeeded" for x in rows) else "pending"

    async def cleanup_provider_artifacts(session_id: str) -> str:
        row = store.one("SELECT voice_id,assistant_id,revoked_at FROM enrollment_sessions WHERE id=?", (session_id,))
        if not row or not row["revoked_at"]:
            return "not_revoked"
        # Every historical version and late provider completion is tracked, not
        # merely the latest pointer on the session.
        assistants = {x['provider_id'] for x in store.all("SELECT provider_id FROM enrollment_operations WHERE session_id=? AND kind='vapi_assistant' AND provider_id IS NOT NULL",(session_id,))}
        voices = {x['provider_voice_id'] for x in store.all("SELECT provider_voice_id FROM enrollment_voice_versions WHERE session_id=? AND provider_voice_id IS NOT NULL",(session_id,))}
        assistants.add(row['assistant_id']); voices.add(row['voice_id'])
        specs = [("cleanup_assistant", x, "VAPI_API_KEY", "vapi") for x in assistants if x]
        specs += [("cleanup_voice", x, "ELEVENLABS_API_KEY", "elevenlabs") for x in voices if x]
        for kind, provider_id, key_name, provider in specs:
            if not provider_id:
                continue
            operation, _ = op_start(session_id, kind, provider_id)
            if operation["state"] == "succeeded":
                continue
            key = os.environ.get(key_name, "").strip()
            if not key:
                op_update(
                    operation["id"],
                    "pending_credentials",
                    provider_id=provider_id,
                    detail={"provider": provider, "reason": "credential unavailable"},
                )
                continue
            op_update(operation["id"], "dispatching", provider_id=provider_id, detail={"provider": provider})
            try:
                if provider == "vapi":
                    await app.state.enrollment_provider.vapi_delete_assistant(key, provider_id)
                else:
                    await app.state.enrollment_provider.eleven_delete_voice(key, provider_id)
            except ProviderError as exc:
                # Provider deletion is idempotent; a later retry is safe. Never call it
                # complete unless the provider confirmed deletion or returned not-found.
                op_update(
                    operation["id"],
                    "retryable",
                    provider_id=provider_id,
                    detail={"provider": provider, "error": str(exc)},
                )
                continue
            op_update(operation["id"], "succeeded", provider_id=provider_id, detail={"provider": provider})
        return cleanup_summary(session_id)

    @app.get('/api/enrollment/readiness')
    def public_readiness():
        from .enrollment_audio import decoder_available
        return {'enabled':_enabled(),'audio_decoder':decoder_available(),
                'interview_configured':bool(os.environ.get('OPENAI_API_KEY')),
                'voice_configured':bool(os.environ.get('ELEVENLABS_API_KEY')),
                'agent_configured':bool(os.environ.get('VAPI_API_KEY')),
                'provider_access_verified':False,'owner_only':True}

    @app.get("/api/enrollment/status")
    def status(user=Depends(actor)):
        return {
            "version": "1.0-a0",
            "enabled": _enabled(),
            "owner_only": True,
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "vapi_private_configured": bool(os.environ.get("VAPI_API_KEY")),
            "vapi_public_configured": bool(os.environ.get("VAPI_PUBLIC_API_KEY")),
            "template_configured": bool(os.environ.get("RANEEN_VAPI_TEMPLATE_ID")),
            "live_provider_evidence": False,
        }

    @app.get("/api/enrollment/consent")
    def enrollment_consent(user=Depends(admin)):
        gate()
        return {"version": CONSENT_VERSION, "text": CONSENT_TEXT}

    @app.post("/api/enrollment/sessions", status_code=201)
    def create_session(body: StartEnrollment, user=Depends(admin)):
        gate()
        if not (
            body.self_attestation
            and body.recording
            and body.external_processing
            and body.voice_cloning
            and body.private_preview
        ):
            raise HTTPException(422, "All disclosed Gate A scopes are required for the cloned-agent test.")
        payload = body.model_dump() | {"text": CONSENT_TEXT}
        stamp = now()
        # Serialize admission across threads/processes sharing this SQLite database.
        # The previous check then insert could create two active enrollments after
        # concurrent clicks. This needs no schema change or destructive migration.
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM enrollment_sessions WHERE owner_id=? AND revoked_at IS NULL "
                "AND state NOT IN ('complete','failed') ORDER BY created DESC LIMIT 1",
                (user["id"],),
            ).fetchone()
            if active:
                old_id = active['id']
                prior = db.execute("SELECT detail FROM enrollment_operations WHERE session_id=? AND kind='realtime_call'", (old_id,)).fetchall()
                exhausted = len(prior) >= 30 or sum(json.loads(item['detail']).get('consumed_seconds', 0) for item in prior) > 1790
                live = db.execute('SELECT state FROM enrollment_realtime_calls WHERE session_id=?', (old_id,)).fetchone()
                unknown = db.execute("SELECT id FROM enrollment_operations WHERE session_id=? AND kind IN ('voice_clone','vapi_assistant') AND state IN ('dispatching','outcome_unknown') LIMIT 1", (old_id,)).fetchone()
                preview = db.execute("SELECT state FROM enrollment_preview_calls WHERE session_id=? AND state IN ('open','dispatching','close_unknown','outcome_unknown') LIMIT 1", (old_id,)).fetchone()
                # A new explicit consent submission can start another interview
                # after a spent allowance. Preserve the prior saved session.
                if exhausted and not unknown and not preview and (not live or live['state'] in {'closed','failed'}):
                    active = None
            ident = active["id"] if active else uid()
            if not active:
                db.execute(
                    "INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated) VALUES(?,?,?,?,?,?,?)",
                    (ident, user["id"], "collecting", CONSENT_VERSION, _json(payload), stamp, stamp),
                )
        (root / ident / "chunks").mkdir(parents=True, exist_ok=True)
        (root / ident / "previews").mkdir(parents=True, exist_ok=True)
        if not active:
            store.audit(user["id"], "enrollment_started", ident, {"version": CONSENT_VERSION})
        return {"id": ident, "resumed": bool(active)}

    summary_select = """
        SELECT s.id,s.state,s.revoked_at,s.created,s.updated,s.voice_state,s.active_behavior_id,
          (SELECT COALESCE(SUM(duration_ms),0) FROM enrollment_chunks WHERE session_id=s.id AND role='contributor') AS saved_audio_ms,
          (SELECT COUNT(*) FROM enrollment_chunks WHERE session_id=s.id) AS chunk_count,
          (SELECT COALESCE(MAX(seq)+1,0) FROM enrollment_chunks WHERE session_id=s.id) AS next_seq,
          (SELECT COUNT(*) FROM enrollment_evidence e JOIN enrollment_evidence_provenance p ON p.evidence_id=e.id WHERE e.session_id=s.id AND e.status='confirmed' AND p.confirmed_method='trusted_audio_exact_phrase') AS confirmed_patterns,
          (SELECT COUNT(*) FROM enrollment_evidence WHERE session_id=s.id AND status='pending') AS pending_patterns
        FROM enrollment_sessions s
    """

    @app.get("/api/enrollment/sessions")
    def list_sessions(limit: int = Query(default=20, ge=1, le=50), user=Depends(admin)):
        # Recovery and consent withdrawal stay available if new enrollment is off.
        rows = store.all(
            summary_select + " WHERE s.owner_id=? ORDER BY s.updated DESC,s.id DESC LIMIT ?",
            (user["id"], limit),
        )
        return {"sessions": [recovery_summary(row) for row in rows], "enabled": _enabled()}

    @app.get("/api/enrollment/sessions/{ident}/journey")
    def get_journey(ident: str, user=Depends(admin)):
        own_session(ident, user, require_active=False)
        row = store.one(summary_select + " WHERE s.id=? AND s.owner_id=?", (ident, user["id"]))
        pending = store.one(
            "SELECT id FROM enrollment_operations WHERE session_id=? "
            "AND kind IN ('voice_clone','vapi_assistant') AND state IN ('dispatching','outcome_unknown') LIMIT 1",
            (ident,),
        )
        live = store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?", (ident,))
        provider_pending = bool(pending or (live and live["state"] in {"close_unknown","dispatching","outcome_unknown"}))
        result = journey_summary(
            row, enabled=_enabled(), provider_pending=provider_pending,
            cleanup_state=cleanup_summary(ident) if row["revoked_at"] else "not_revoked",
            interview_call_state=live["state"] if live else "none",
        )
        calls = store.all("SELECT detail FROM enrollment_operations WHERE session_id=? AND kind='realtime_call'", (ident,))
        result['interview_attempts_left'] = max(0, 30 - len(calls))
        result['interview_seconds_left'] = max(0, 1800 - sum(json.loads(call['detail']).get('consumed_seconds', 0) for call in calls))
        result['resume_limit'] = 'connections' if not result['interview_attempts_left'] else 'time' if result['interview_seconds_left'] < 10 else None
        if result['resume_limit']:
            result['can_resume'] = False
        return result

    @app.get("/api/enrollment/sessions/{ident}")
    def get_session(ident: str, user=Depends(admin)):
        row = own_session(ident, user, require_active=False)
        chunks = store.all(
            "SELECT seq,sha256,byte_count,duration_ms,mime,role FROM enrollment_chunks WHERE session_id=? ORDER BY seq",
            (ident,),
        )
        evidence = store.all(
            "SELECT id,kind,status,source_item_id,created,confirmed_at FROM enrollment_evidence WHERE session_id=? ORDER BY created",
            (ident,),
        )
        return {
            "id": ident,
            "state": row["state"],
            "total_bytes": row["total_bytes"],
            "saved_audio_ms": row["clean_ms"],
            "clean_ms": None,
            "voice_state": row["voice_state"],
            "voice_ready": row["voice_state"] == "ready",
            "behavior_id": row["active_behavior_id"],
            "assistant_id": row["assistant_id"],
            "revoked": bool(row["revoked_at"]),
            "chunks": chunks,
            "evidence": evidence,
        }

    @app.put("/api/enrollment/sessions/{ident}/chunks/{seq}")
    async def upload_chunk(ident: str, seq: int, request: Request, user=Depends(admin)):
        gate()
        if seq < 0 or seq > 10000:
            raise HTTPException(422, "Invalid chunk sequence.")
        row = own_session(ident, user)
        consent(row, "recording")
        role = request.headers.get("x-speaker-role", "")
        if role != "contributor":
            raise HTTPException(422, "Only contributor microphone audio can enter enrollment storage.")
        mime = request.headers.get("content-type", "").split(";")[0]
        if mime not in {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav"}:
            raise HTTPException(415, "Unsupported enrollment audio format.")
        expected = request.headers.get("x-chunk-sha256", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise HTTPException(422, "Missing or invalid chunk checksum.")
        try:
            duration_ms = int(request.headers.get("x-duration-ms", "0"))
        except ValueError:
            raise HTTPException(422, "Invalid chunk duration.") from None
        if duration_ms < 250 or duration_ms > 15_000:
            raise HTTPException(422, "Chunk duration must be between 250 ms and 15 seconds.")
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > CHUNK_MAX:
                raise HTTPException(413, "Enrollment chunk is too large.")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise HTTPException(409, "Chunk checksum mismatch.")
        old = store.one(
            "SELECT sha256,byte_count,duration_ms FROM enrollment_chunks WHERE session_id=? AND seq=?",
            (ident, seq),
        )
        if old:
            if old["sha256"] == actual and old["byte_count"] == len(data) and old["duration_ms"] == duration_ms:
                return {"seq": seq, "deduplicated": True}
            raise HTTPException(409, "This chunk sequence already contains different audio.")

        session_dir = root / ident / "chunks"
        session_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".webm" if mime == "audio/webm" else ".ogg" if mime == "audio/ogg" else ".wav"
        final_path = session_dir / f"{seq:06d}{suffix}"
        fd, tmp_name = tempfile.mkstemp(prefix="chunk-", dir=session_dir)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            # Finalization and consent check share one immediate write transaction.
            with store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute(
                    "SELECT revoked_at,consent_json,total_bytes,clean_ms FROM enrollment_sessions WHERE id=? AND owner_id=?",
                    (ident, user["id"]),
                ).fetchone()
                if not fresh or fresh["revoked_at"] or not json.loads(fresh["consent_json"]).get("recording"):
                    raise HTTPException(409, "Enrollment consent was withdrawn before this chunk finalized.")
                preparing=db.execute("SELECT id FROM enrollment_operations WHERE session_id=? AND kind='voice_clone' AND state IN ('dispatching','outcome_unknown')",(ident,)).fetchone()
                if preparing: raise HTTPException(409,'Voice preparation is in progress; the recording sample is frozen.')
                if fresh["total_bytes"] + len(data) > SESSION_MAX:
                    raise HTTPException(507, "Enrollment storage allowance reached.")
                conflict = db.execute(
                    "SELECT sha256 FROM enrollment_chunks WHERE session_id=? AND seq=?", (ident, seq)
                ).fetchone()
                if conflict:
                    if conflict["sha256"] == actual:
                        return {"seq": seq, "deduplicated": True}
                    raise HTTPException(409, "This chunk sequence already contains different audio.")
                os.replace(tmp_path, final_path)
                db.execute(
                    "INSERT INTO enrollment_chunks VALUES(?,?,?,?,?,?,?,?,?)",
                    (ident, seq, actual, len(data), duration_ms, mime, role, str(final_path), now()),
                )
                db.execute(
                    "UPDATE enrollment_sessions SET total_bytes=total_bytes+?,clean_ms=clean_ms+?,updated=? WHERE id=?",
                    (len(data), duration_ms, now(), ident),
                )
        finally:
            tmp_path.unlink(missing_ok=True)
        return {"seq": seq, "deduplicated": False}

    @app.post("/api/enrollment/sessions/{ident}/transcripts")
    def save_transcript(ident: str, body: TranscriptEvent, user=Depends(admin)):
        own_session(ident,user)
        raise HTTPException(410,'Interview evidence is captured through the trusted provider connection.')

    @app.post("/api/enrollment/sessions/{ident}/tool")
    def tool_event(ident: str, body: ToolEvent, user=Depends(admin)):
        own_session(ident,user)
        raise HTTPException(410,'Interview tools are handled through the trusted provider connection.')

    def realtime_session_config(ident: str) -> dict:
        from .enrollment_evidence import interviewer_tools, interviewer_instructions
        config = {
            "type": "realtime",
            "model": os.environ.get("RANEEN_REALTIME_MODEL", "gpt-realtime-2.1"),
            "output_modalities": ["audio"],
            "instructions": f"""
You are the Raneen voice-enrollment interviewer. The speaker is teaching an AI how
they naturally speak and handle customer situations. Converse naturally in the
speaker's own language/dialect; do not force Emirati Arabic or formal Arabic.
Aim for a first preview after about five minutes, then let the person keep teaching.

Alternate between natural conversation, realistic customer role-play, asking what
mattered in a response, and changing one important fact to learn when their answer
changes. Keep your turns short. Never dictate an ideal answer before collecting the
speaker's answer. Preserve corrections, numbers, negation, names, and code-switching.

When you learn a reusable response pattern, call propose_evidence. After the tool
returns an evidence_id, ask one specific spoken confirmation. On the speaker's next
answer call confirm_evidence with that evidence_id. If they reject it, summarize the
correction, propose a replacement, and confirm again. Do not claim model-weight
training and do not claim to be the contributor. Session id: {ident}
""",
            "audio": {
                "output": {"voice": os.environ.get("RANEEN_INTERVIEWER_VOICE", "marin")},
                "input": {
                    "transcription": {
                        "model": os.environ.get("RANEEN_LIVE_TRANSCRIBE_MODEL", "gpt-transcribe"),
                        "prompt": "Transcribe verbatim. Preserve colloquial Arabic, English words, numbers, negation, and names. Do not formalize.",
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
            },
            "tools": [
                {
                    "type": "function",
                    "name": "propose_evidence",
                    "description": "Propose one reusable response, decision, or style pattern learned from the contributor. Spoken confirmation is required.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["response_pattern", "decision_rule", "style_preference"]},
                            "situation": {"type": "string"},
                            "interpretation": {"type": "string"},
                            "change_condition": {"type": "string"},
                        },
                        "required": ["kind", "situation", "interpretation", "change_condition"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "confirm_evidence",
                    "description": "Record the contributor's spoken acceptance or rejection of a proposed evidence item.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "accepted": {"type": "boolean"},
                            "correction": {"type": "string"},
                        },
                        "required": ["evidence_id", "accepted", "correction"],
                        "additionalProperties": False,
                    },
                },
            ],
            "tool_choice": "auto",
        }
        config['tools'] = interviewer_tools()
        config['instructions'] = interviewer_instructions(ident,app.state.enrollment_evidence.confirmed_context(ident))
        return config

    def finish_interview_usage(session_id, call_id):
        op = store.one("SELECT id,detail,created FROM enrollment_operations WHERE session_id=? AND kind='realtime_call' AND provider_id=?",(session_id,call_id))
        if not op: return
        detail=json.loads(op['detail'])
        if 'consumed_seconds' in detail: return
        try: elapsed=max(1,int((datetime.now(timezone.utc)-datetime.fromisoformat(op['created'])).total_seconds()))
        except (ValueError,TypeError): elapsed=detail.get('reserved_seconds',1800)
        detail['consumed_seconds']=min(elapsed,detail.get('reserved_seconds',1800))
        store.execute('UPDATE enrollment_operations SET detail=?,updated=? WHERE id=?',(_json(detail),now(),op['id']))

    async def close_realtime_later(session_id: str, call_id: str, delay: int):
        await asyncio.sleep(max(0, delay))
        await app.state.enrollment_sideband.close(session_id,call_id)
        live = store.one(
            "SELECT state,call_id FROM enrollment_realtime_calls WHERE session_id=?",
            (session_id,),
        )
        if not live or live["state"] != "open" or live["call_id"] != call_id:
            return
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            store.execute(
                "UPDATE enrollment_realtime_calls SET state='close_unknown',updated=? WHERE session_id=? AND call_id=?",
                (now(), session_id,call_id),
            )
            return
        try:
            await app.state.enrollment_provider.openai_hangup(key, call_id)
        except ProviderError:
            store.execute(
                "UPDATE enrollment_realtime_calls SET state='close_unknown',updated=? WHERE session_id=? AND call_id=?",
                (now(), session_id,call_id),
            )
            return
        finish_interview_usage(session_id,call_id)
        store.execute(
            "UPDATE enrollment_realtime_calls SET state='closed',updated=? WHERE session_id=? AND call_id=?",
            (now(), session_id,call_id),
        )

    @app.on_event("shutdown")
    async def stop_trusted_connections():
        await app.state.enrollment_sideband.stop_all()

    @app.on_event("startup")
    async def recover_interrupted_operations():
        store.execute("UPDATE enrollment_operations SET state='outcome_unknown',updated=? WHERE state='dispatching' AND kind NOT LIKE 'cleanup_%'",(now(),))
        store.execute("UPDATE enrollment_realtime_calls SET state='outcome_unknown',updated=? WHERE state='dispatching'",(now(),))
        store.execute("UPDATE enrollment_preview_calls SET state='outcome_unknown',updated=? WHERE state='dispatching'",(now(),))
        for call in store.all("SELECT DISTINCT session_id FROM enrollment_preview_calls WHERE state IN ('open','close_unknown')"):
            await stop_preview_calls(call['session_id'])

    @app.on_event("startup")
    async def recover_realtime_calls():
        # A server restart invalidates our local control lease. Fail closed by ending
        # any call the previous process still considered open.
        for live in store.all("SELECT session_id,call_id FROM enrollment_realtime_calls WHERE state='open'"):
            asyncio.create_task(close_realtime_later(live["session_id"], live["call_id"], 0))

    @app.on_event("startup")
    async def recover_revoked_cleanup():
        for revoked in store.all("SELECT id FROM enrollment_sessions WHERE revoked_at IS NOT NULL"):
            asyncio.create_task(cleanup_provider_artifacts(revoked["id"]))

    @app.post("/api/enrollment/sessions/{ident}/webrtc")
    async def create_webrtc(ident: str, request: Request, user=Depends(admin)):
        gate()
        row = own_session(ident, user)
        consent(row, "external_processing")
        if request.headers.get("content-type", "").split(";")[0] not in {"application/sdp", "text/plain"}:
            raise HTTPException(415, "Expected an SDP offer.")
        raw = await request.body()
        if not raw or len(raw) > 200_000:
            raise HTTPException(422, "Invalid SDP offer.")
        try:
            offer = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "SDP offer must be UTF-8.") from None
        api_key = _require_key("OPENAI_API_KEY")
        reservation = uid()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT revoked_at FROM enrollment_sessions WHERE id=?',(ident,)).fetchone()
            if active['revoked_at']: raise HTTPException(410,'Enrollment was revoked.')
            preview = db.execute("SELECT id FROM enrollment_preview_calls WHERE session_id=? AND state IN ('open','dispatching','outcome_unknown','close_unknown')",(ident,)).fetchone()
            if preview: raise HTTPException(409,'End the agent test before resuming the interview.')
            current = db.execute('SELECT state FROM enrollment_realtime_calls WHERE session_id=?',(ident,)).fetchone()
            if current and current['state'] in {'open','dispatching','close_unknown','outcome_unknown'}:
                raise HTTPException(409,'An interview is active or its previous outcome needs reconciliation.')
            attempts = db.execute("SELECT COUNT(*) AS n FROM enrollment_operations WHERE session_id=? AND kind='realtime_call'",(ident,)).fetchone()['n']
            if attempts >= 30: raise HTTPException(429,'The private interview connection allowance is used.')
            previous = db.execute("SELECT detail FROM enrollment_operations WHERE session_id=? AND kind='realtime_call'",(ident,)).fetchall()
            used_seconds = sum(json.loads(x['detail']).get('consumed_seconds',0) for x in previous)
            maximum = max(0,1800-used_seconds)
            if maximum < 10: raise HTTPException(429,'The 30-minute interview allowance is used.')
            db.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET call_id=excluded.call_id,state=excluded.state,started=excluded.started,updated=excluded.updated",(ident,reservation,'dispatching',now(),now()))
            db.execute("INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,'{}',?,?)",(reservation,ident,'realtime_call',reservation,'dispatching',now(),now()))
        safety_id = hashlib.sha256(("raneen:" + user["id"]).encode()).hexdigest()[:64]
        try:
            result = await app.state.enrollment_provider.openai_create_call(api_key, safety_id, offer, realtime_session_config(ident))
        except ProviderError as exc:
            state='outcome_unknown' if exc.uncertain else 'failed'
            op_update(reservation,state,detail={'error':str(exc)})
            store.execute('UPDATE enrollment_realtime_calls SET state=?,updated=? WHERE session_id=? AND call_id=?',(state,now(),ident,reservation))
            raise HTTPException(502,str(exc)) from None
        op_update(reservation,'succeeded',provider_id=result['call_id'],detail={'reserved_seconds':maximum})
        store.execute("UPDATE enrollment_realtime_calls SET call_id=?,state='open',updated=? WHERE session_id=? AND call_id=?",(result['call_id'],now(),ident,reservation))
        if own_session(ident,user,require_active=False)['revoked_at']:
            await close_webrtc(ident,user)
            raise HTTPException(410,'Consent was withdrawn while connecting the interview.')
        try:
            await app.state.enrollment_sideband.attach(ident,result['call_id'],api_key)
        except Exception:
            await close_webrtc(ident,user)
            raise HTTPException(503,'The trusted interview connection could not be established. Please try again.') from None
        asyncio.create_task(close_realtime_later(ident, result["call_id"], maximum))
        return Response(result["sdp"], media_type="application/sdp", headers={"X-Raneen-Interview-Limit": str(maximum)})

    @app.post("/api/enrollment/sessions/{ident}/webrtc-close")
    async def close_webrtc(ident: str, user=Depends(admin)):
        own_session(ident, user, require_active=False)
        await app.state.enrollment_sideband.close(ident)
        live = store.one(
            "SELECT call_id,state FROM enrollment_realtime_calls WHERE session_id=?",
            (ident,),
        )
        if not live or live["state"] not in {"open", "close_unknown"}:
            return {"state": live["state"] if live else "none"}
        try:
            await app.state.enrollment_provider.openai_hangup(_require_key("OPENAI_API_KEY"), live["call_id"])
        except ProviderError as exc:
            store.execute(
                "UPDATE enrollment_realtime_calls SET state='close_unknown',updated=? WHERE session_id=? AND call_id=?",
                (now(), ident, live["call_id"]),
            )
            raise HTTPException(502, str(exc)) from None
        finish_interview_usage(ident,live["call_id"])
        store.execute(
            "UPDATE enrollment_realtime_calls SET state='closed',updated=? WHERE session_id=? AND call_id=?",
            (now(), ident, live["call_id"]),
        )
        return {"state": "closed"}

    @app.post("/api/enrollment/sessions/{ident}/behavior", status_code=201)
    def build_behavior(ident: str, body: ExternalApproval, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve compiling the confirmed spoken evidence.")
        row = own_session(ident, user)
        confirmed = app.state.enrollment_evidence.confirmed_rows(ident)
        if not confirmed:
            raise HTTPException(409, "No confirmed spoken evidence is available yet.")
        payload = {
            "evidence_origin": "trusted_audio_v1",
            "language_policy": "Preserve the contributor's demonstrated dialect and wording; do not imitate unsupported traits.",
            "evidence": [
                {
                    "id": x["id"],
                    "kind": x["kind"],
                    "source_item_id": x["source_item_id"],
                    "demonstration": json.loads(x["payload"]),
                    "confirmation_transcript": x["confirmation_transcript"],
                }
                for x in confirmed
            ],
        }
        serial = _json(payload)
        if len(serial) > 28_000:
            raise HTTPException(409, "Confirmed evidence is too large for the first preview behavior pack.")
        digest = hashlib.sha256(serial.encode()).hexdigest()
        old = store.one(
            "SELECT id,version FROM enrollment_behavior_versions WHERE session_id=? AND digest=?",
            (ident, digest),
        )
        if old:
            return {"id": old["id"], "version": old["version"], "reused": True}
        current = store.one(
            "SELECT COALESCE(MAX(version),0) AS v FROM enrollment_behavior_versions WHERE session_id=?",
            (ident,),
        )["v"]
        behavior_id = uid()
        store.execute(
            "INSERT INTO enrollment_behavior_versions VALUES(?,?,?,?,?,?)",
            (behavior_id, ident, current + 1, serial, digest, now()),
        )
        store.execute(
            "UPDATE enrollment_sessions SET active_behavior_id=?,updated=? WHERE id=?",
            (behavior_id, now(), ident),
        )
        return {"id": behavior_id, "version": current + 1, "reused": False}

    def select_clone_chunks(session_id: str, final_seq: int) -> tuple[list[dict], dict]:
        from .enrollment_audio import prepare_clone_sample, AudioValidationError
        chunks = store.all("SELECT * FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,))
        try:
            return prepare_clone_sample(
                chunks, root / session_id / 'samples', min_sample_ms=MIN_CLONE_MS,
                min_active_ms=MIN_CLONE_ACTIVE_MS, max_sample_ms=MAX_CLONE_MS, final_seq=final_seq,
            )
        except AudioValidationError as exc:
            detail = {'code': exc.code, 'message': str(exc)}
            if getattr(exc, 'details', None) is not None:
                detail['details'] = exc.details
            raise HTTPException(409, detail) from None

    def latest_voice_version(session_id: str):
        return store.one(
            "SELECT * FROM enrollment_voice_versions WHERE session_id=? ORDER BY version DESC LIMIT 1",
            (session_id,),
        )

    def clone_operations(session_id: str, db=None):
        sql = "SELECT * FROM enrollment_operations WHERE session_id=? AND kind='voice_clone' ORDER BY created,rowid"
        return [dict(x) for x in db.execute(sql, (session_id,)).fetchall()] if db else store.all(sql, (session_id,))

    def clone_failure(operation: dict, attempts: int, *, allowed: bool = False):
        failure = stored_failure(operation)
        failure['details'] = failure.get('details', {}) | {
            'reason': failure.get('details', {}).get('reason', 'provider'),
            'retry_allowed': allowed, 'attempts': attempts, 'attempt_limit': MAX_CLONE_ATTEMPTS,
        }
        return failure

    def check_clone_attempt(operations: list[dict], retry_failed: bool):
        """Read-only preflight and the same checks under the atomic create claim."""
        busy = next((x for x in reversed(operations) if x['state'] in {'dispatching', 'outcome_unknown'}), None)
        if busy:
            raise HTTPException(409, clone_failure(busy, len(operations)))
        if len(operations) >= MAX_CLONE_ATTEMPTS:
            raise HTTPException(429, {
                'code': 'clone_retry_limit', 'message': 'The three-attempt private voice allowance is used.',
                'details': {'reason': 'provider', 'retry_allowed': False,
                            'attempts': len(operations), 'attempt_limit': MAX_CLONE_ATTEMPTS},
            })
        if operations:
            old = operations[-1]
            allowed = retryable_clone(old)
            if not retry_failed or not allowed:
                raise HTTPException(409, clone_failure(old, len(operations), allowed=allowed))

    def claim_clone(session_id: str, manifest: dict, retry_failed: bool):
        """Allocate each clone attempt and its immutable voice version together."""
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute('SELECT * FROM enrollment_sessions WHERE id=?', (session_id,)).fetchone()
            if not session or session['revoked_at']:
                raise HTTPException(410, 'Enrollment consent was withdrawn.')
            consent_data = json.loads(session['consent_json'])
            if not consent_data.get('voice_cloning') or not consent_data.get('external_processing'):
                raise HTTPException(409, 'Enrollment does not authorize voice cloning and external processing.')
            operations = clone_operations(session_id, db)
            if session['voice_id']:
                raise HTTPException(409, 'This enrollment already has its private voice; reuse it.')
            check_clone_attempt(operations, retry_failed)
            attempt = len(operations) + 1
            # Source spans and their selection algorithm stay unchanged. The
            # separate provider-attempt metadata makes the immutable audit version
            # unique when a rejected request is explicitly repeated with same audio.
            attempt_manifest = manifest if attempt == 1 else manifest | {'provider_attempt': attempt}
            manifest_serial = _json(attempt_manifest)
            digest = hashlib.sha256(manifest_serial.encode()).hexdigest()
            next_version = db.execute(
                'SELECT COALESCE(MAX(version),0)+1 AS v FROM enrollment_voice_versions WHERE session_id=?',
                (session_id,),
            ).fetchone()['v']
            operation_id, version_id, stamp = uid(), uid(), now()
            operation_detail = {'voice_version_id': version_id, 'manifest_digest': digest, 'attempt': attempt}
            db.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,?,?,?)',
                       (operation_id, session_id, 'voice_clone', 'ivc-v1:' + digest, 'dispatching',
                        _json(operation_detail), stamp, stamp))
            db.execute('INSERT INTO enrollment_voice_versions VALUES(?,?,?,?,NULL,?,?,?,?,?)',
                       (version_id, session_id, next_version, 'elevenlabs', 'creating',
                        manifest_serial, digest, stamp, stamp))
            db.execute("UPDATE enrollment_sessions SET voice_state='creating',updated=? WHERE id=?", (stamp, session_id))
        return {'id': operation_id, 'attempt': attempt}, version_id, next_version, digest

    def preview_attempts(operations: list[dict], voice_id: str, kind: str):
        base = f'{voice_id}:{kind}'
        return [x for x in operations if x['kind'] == 'voice_preview' and
                (x['op_key'] == base or x['op_key'].startswith(base + ':attempt:'))]

    def preview_retry(operation: dict | None):
        return bool(operation and operation['state'] == 'failed' and not operation['provider_id']
                    and stored_failure(operation).get('details', {}).get('rejected'))

    def preview_failure(operation: dict, attempts: int, *, allowed=False):
        failure = stored_failure(operation)
        failure['details'] = failure.get('details', {}) | {
            'reason': failure.get('details', {}).get('reason', 'provider'),
            'retry_allowed': allowed, 'attempts': attempts, 'attempt_limit': MAX_CLONE_ATTEMPTS,
        }
        return failure

    def claim_preview(session_id: str, voice_id: str, kind: str, retry_failed: bool):
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute('SELECT * FROM enrollment_sessions WHERE id=?', (session_id,)).fetchone()
            if not session or session['revoked_at']:
                raise HTTPException(410, 'Enrollment consent was withdrawn.')
            if session['voice_id'] != voice_id or session['voice_state'] not in {'sample_required', 'ready'}:
                raise HTTPException(409, 'Voice changed or requires provider verification before synthesis.')
            consent_data = json.loads(session['consent_json'])
            if not consent_data.get('voice_cloning') or not consent_data.get('external_processing'):
                raise HTTPException(409, 'Enrollment does not authorize voice synthesis and external processing.')
            operations = [dict(x) for x in db.execute(
                "SELECT * FROM enrollment_operations WHERE session_id=? AND kind='voice_preview' ORDER BY created,rowid",
                (session_id,),
            ).fetchall()]
            attempts = preview_attempts(operations, voice_id, kind)
            if attempts and attempts[-1]['state'] == 'succeeded':
                return attempts[-1], False
            busy = next((x for x in reversed(operations) if x['state'] in {'dispatching', 'outcome_unknown'}), None)
            if busy:
                raise HTTPException(409, preview_failure(busy, len(attempts)))
            if len(attempts) >= MAX_CLONE_ATTEMPTS:
                raise HTTPException(429, {
                    'code': 'voice_preview_retry_limit', 'message': 'The three-attempt allowance for this voice sample is used.',
                    'details': {'reason': 'provider', 'retry_allowed': False,
                                'attempts': len(attempts), 'attempt_limit': MAX_CLONE_ATTEMPTS},
                })
            if attempts:
                allowed = preview_retry(attempts[-1])
                if not retry_failed or not allowed:
                    raise HTTPException(409, preview_failure(attempts[-1], len(attempts), allowed=allowed))
            attempt = len(attempts) + 1
            base = f'{voice_id}:{kind}'
            key = base if attempt == 1 else base + ':attempt:' + str(attempt)
            operation_id, stamp = uid(), now()
            db.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,?,?,?)',
                       (operation_id, session_id, 'voice_preview', key, 'dispatching',
                        _json({'preview_kind': kind, 'voice_id': voice_id, 'attempt': attempt}), stamp, stamp))
        return {'id': operation_id, 'attempt': attempt}, True

    @app.post("/api/enrollment/sessions/{ident}/clone")
    async def create_clone(ident: str, body: CloneRequest, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve creating the private voice clone.")
        row = own_session(ident, user)
        consent(row, "voice_cloning")
        consent(row, "external_processing")
        current_voice = latest_voice_version(ident)
        if row["voice_state"] in {"sample_required", "ready"} and row["voice_id"]:
            return {
                "state": row["voice_state"],
                "voice_id": row["voice_id"],
                "voice_version_id": current_voice["id"] if current_voice else None,
                "reused": True,
            }
        if row["voice_state"] in {"verification_required", "outcome_unknown"}:
            return {
                "state": row["voice_state"],
                "voice_id": row["voice_id"],
                "voice_version_id": current_voice["id"] if current_voice else None,
                "reused": True,
            }
        api_key = _require_key("ELEVENLABS_API_KEY")
        live=store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?",(ident,))
        if live and live['state'] in {'open','dispatching','close_unknown','outcome_unknown'}:
            raise HTTPException(409,'Pause and finish saving the interview before preparing the voice.')
        check_clone_attempt(clone_operations(ident), body.retry_failed)
        chosen, manifest = await asyncio.to_thread(select_clone_chunks, ident,body.final_seq)
        try:
            consent(own_session(ident,user), "voice_cloning")
            minimum = MIN_CLONE_MS
            if manifest["total_ms"] < minimum:
                raise HTTPException(
                    409,
                    {'code': 'insufficient_audio', 'message': f"The selected sample is shorter than the required recording duration ({manifest['total_ms']//1000}s selected)."},
                )
            operation, voice_version_id, next_version, manifest_digest = claim_clone(ident, manifest, body.retry_failed)
            api_key = _require_key("ELEVENLABS_API_KEY")
            files = []
            for chunk in chosen:
                path = Path(chunk["path"])
                suffix = path.suffix or ".webm"
                files.append((f"sample-{chunk['seq']:04d}{suffix}", path, chunk["mime"]))
            try:
                result = await app.state.enrollment_provider.eleven_clone(
                    api_key, "Raneen private enrollment " + ident[:8] + " voice-v" + str(next_version), files
                )
                if (not isinstance(result, dict) or not isinstance(result.get('voice_id'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', result['voice_id'])
                        or type(result.get('requires_verification')) is not bool):
                    raise ProviderError('The voice provider returned an unverified clone result.', uncertain=True)
            except ProviderError as exc:
                state = "outcome_unknown" if exc.uncertain else "failed"
                failure = provider_failure(exc, 'voice_clone')
                if exc.diagnostics:
                    failure['details'] = failure['details'] | {
                        'retry_allowed': not exc.uncertain and exc.diagnostics['rejected'] and operation['attempt'] < MAX_CLONE_ATTEMPTS,
                        'attempts': operation['attempt'], 'attempt_limit': MAX_CLONE_ATTEMPTS,
                    }
                stamp = now()
                with store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE enrollment_operations SET state=?,detail=?,updated=? WHERE id=?',
                               (state, _json({'failure': failure, 'voice_version_id': voice_version_id,
                                              'manifest_digest': manifest_digest, 'attempt': operation['attempt']}),
                                stamp, operation['id']))
                    db.execute('UPDATE enrollment_voice_versions SET state=?,updated=? WHERE id=?',
                               (state, stamp, voice_version_id))
                    db.execute('UPDATE enrollment_sessions SET voice_state=?,updated=? WHERE id=?',
                               (state, stamp, ident))
                raise HTTPException(502 if exc.uncertain else 422, failure) from None
            state = "verification_required" if result["requires_verification"] else "sample_required"
            stamp = now()
            with store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE enrollment_operations SET state='succeeded',provider_id=?,detail=?,updated=? WHERE id=?",
                           (result['voice_id'], _json({'requires_verification': result['requires_verification'],
                                                       'voice_version_id': voice_version_id,
                                                       'manifest_digest': manifest_digest,
                                                       'attempt': operation['attempt']}), stamp, operation['id']))
                db.execute('UPDATE enrollment_voice_versions SET provider_voice_id=?,state=?,updated=? WHERE id=?',
                           (result['voice_id'], state, stamp, voice_version_id))
                db.execute('UPDATE enrollment_sessions SET voice_id=?,voice_state=?,updated=? WHERE id=?',
                           (result['voice_id'], state, stamp, ident))
            if own_session(ident, user, require_active=False)["revoked_at"]:
                await cleanup_provider_artifacts(ident)
                raise HTTPException(410, "Consent was withdrawn during voice preparation; cleanup was scheduled.")
            return {
                "state": state,
                "voice_id": result["voice_id"],
                "voice_version_id": voice_version_id,
                "reused": False,
            }
        finally:
            for sample in chosen:
                Path(sample['path']).unlink(missing_ok=True)

    @app.post("/api/enrollment/sessions/{ident}/preview")
    async def synth_preview(ident: str, body: PreviewRequest, user=Depends(admin)):
        from .enrollment_audio import validate_synthesized_audio, AudioValidationError
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve generating fresh synthetic preview audio.")
        row = own_session(ident, user)
        consent(row, "voice_cloning")
        if row["voice_state"] not in {"sample_required", "ready"} or not row["voice_id"]:
            raise HTTPException(409, "Voice requires provider verification before synthesis.")
        api_key = _require_key("ELEVENLABS_API_KEY")
        operation, fresh = claim_preview(ident, row['voice_id'], body.kind, body.retry_failed)
        preview_path = root / ident / "previews" / (row['voice_id'] + '-' + body.kind + ".mp3")
        if not fresh:
            if operation["state"] == "succeeded" and preview_path.is_file():
                return FileResponse(preview_path, media_type="audio/mpeg")
            raise HTTPException(409, {
                'code': 'outcome_unknown' if operation['state'] in {'dispatching', 'outcome_unknown'} else 'voice_preview_failed',
                'message': f"Preview operation is {operation['state']}; reconcile it before retrying.",
            })
        try:
            audio = await app.state.enrollment_provider.eleven_speech(api_key, row["voice_id"], PREVIEW_TEXT[body.kind])
            quality = await asyncio.to_thread(validate_synthesized_audio, audio)
        except (ProviderError, AudioValidationError) as exc:
            if getattr(exc, 'uncertain', False):
                code = 'outcome_unknown'
            elif isinstance(exc, AudioValidationError):
                code = 'decoder_unavailable' if exc.code == 'decoder_unavailable' else 'invalid_voice_preview'
            else:
                code = 'voice_preview_failed'
            detail = provider_failure(exc, 'voice_preview') if isinstance(exc, ProviderError) else {'code': code, 'message': str(exc)}
            if isinstance(exc, ProviderError) and exc.diagnostics:
                detail['details'] = detail['details'] | {
                    'retry_allowed': not exc.uncertain and exc.diagnostics['rejected'] and operation['attempt'] < MAX_CLONE_ATTEMPTS,
                    'attempts': operation['attempt'], 'attempt_limit': MAX_CLONE_ATTEMPTS,
                }
            elif getattr(exc, 'details', None) is not None:
                detail['details'] = exc.details
            op_update(operation['id'], 'outcome_unknown' if getattr(exc, 'uncertain', False) else 'failed',
                      detail={'failure': detail, 'preview_kind': body.kind,
                              'voice_id': row['voice_id'], 'attempt': operation['attempt']})
            raise HTTPException(502, detail) from None
        # Provider completion is recorded before checking consent again. A withdrawn
        # enrollment never receives the late sample or reactivates playback.
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT revoked_at,voice_id FROM enrollment_sessions WHERE id=?', (ident,)).fetchone()
            if active['revoked_at'] or active['voice_id'] != row['voice_id']:
                db.execute("UPDATE enrollment_operations SET state='cancelled',updated=? WHERE id=?",(now(),operation['id']))
                raise HTTPException(410, 'Consent was withdrawn during speech preparation.')
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            preview_path.write_bytes(audio)
            db.execute("UPDATE enrollment_operations SET state='succeeded',provider_id=?,detail=?,updated=? WHERE id=?",(row['voice_id'],_json({'kind':body.kind,'sha256':hashlib.sha256(audio).hexdigest(),'quality':quality}),now(),operation['id']))
            db.execute("UPDATE enrollment_sessions SET voice_state='ready',updated=? WHERE id=?",(now(),ident))
            db.execute("UPDATE enrollment_voice_versions SET state='ready',updated=? WHERE session_id=? AND provider_voice_id=?",(now(),ident,row['voice_id']))
        return FileResponse(preview_path, media_type="audio/mpeg")

    @app.post('/api/enrollment/sessions/{ident}/voice-approval')
    def approve_voice(ident: str, body: ExternalApproval, user=Depends(admin)):
        gate()
        row = own_session(ident,user)
        consent(row,'voice_cloning')
        if not body.approve:
            raise HTTPException(403,'Listen to the fresh samples and explicitly approve your voice.')
        previews = store.one("SELECT COUNT(*) AS n FROM enrollment_operations WHERE session_id=? AND kind='voice_preview' AND provider_id=? AND state='succeeded'",(ident,row['voice_id']))['n']
        if row['voice_state'] != 'ready' or previews < 3:
            raise HTTPException(409,'Listen to all three freshly generated samples before approving your voice.')
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT revoked_at FROM enrollment_sessions WHERE id=?',(ident,)).fetchone()
            if active['revoked_at']:
                raise HTTPException(410,'Enrollment was revoked.')
            db.execute('INSERT OR IGNORE INTO enrollment_voice_approvals VALUES(?,?,?)',(ident,row['voice_id'],now()))
        return {'voice_approved': True}

    def behavior_prompt(behavior: dict) -> str:
        return (
            "You are a private Raneen test agent. Always disclose that you are AI using "
            "a consented synthetic voice; never claim to be the human contributor. "
            "Respond naturally and briefly. Preserve caller corrections, numbers, "
            "negation and intent. Do not invent live property inventory, prices, returns, "
            "bookings, transfers or completed actions. The following confirmed evidence "
            "describes demonstrated response style and decision patterns, not business "
            "facts or universal personality traits:\n" + behavior["payload"]
        )

    @app.post("/api/enrollment/sessions/{ident}/assistant")
    async def create_assistant(ident: str, body: AssistantRequest, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve creating the isolated Vapi preview assistant.")
        row = own_session(ident, user)
        consent(row, "private_preview")
        if row["voice_state"] != "ready" or not row["voice_id"]:
            raise HTTPException(409, "Voice clone is not ready.")
        behavior = store.one(
            "SELECT * FROM enrollment_behavior_versions WHERE id=? AND session_id=?",
            (body.behavior_id, ident),
        )
        if not behavior:
            raise HTTPException(404, "Behavior version not found.")
        if json.loads(behavior["payload"]).get("evidence_origin") != "trusted_audio_v1":
            raise HTTPException(409,"Rebuild the response profile from confirmed spoken evidence first.")
        if not store.one('SELECT approved FROM enrollment_voice_approvals WHERE session_id=? AND voice_id=?',(ident,row['voice_id'])):
            raise HTTPException(409,'Approve the fresh voice samples before preparing the agent.')
        api_key = _require_key("VAPI_API_KEY")
        template_id = os.environ.get("RANEEN_VAPI_TEMPLATE_ID", "").strip()
        if template_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", template_id):
            raise HTTPException(503,'The test agent template is not configured correctly.')
        op_key = row["voice_id"] + ":" + behavior["digest"]
        operation, fresh = op_start(ident, "vapi_assistant", op_key)
        if not fresh:
            if operation["state"] == "succeeded":
                return {"assistant_id": operation["provider_id"], "reused": True}
            raise HTTPException(409, f"Assistant creation is {operation['state']}; reconcile it before retrying.")
        api_key = _require_key("VAPI_API_KEY")
        template_id = os.environ.get("RANEEN_VAPI_TEMPLATE_ID", "").strip()
        if template_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", template_id):
            op_update(operation["id"], "failed", detail={"error": "invalid template id"})
            raise HTTPException(503, "RANEEN_VAPI_TEMPLATE_ID must be a Vapi assistant UUID.")
        try:
            source = await app.state.enrollment_provider.vapi_json(api_key,"GET","/assistant/"+template_id) if template_id else {
                'model':{'provider':'openai','model':'gpt-4.1-mini'},
                'transcriber':{'provider':'openai','model':'gpt-4o-transcribe','language':'ar'}
            }
            if not isinstance(source,dict): raise ProviderError('Template response was invalid.')
            source_model = source.get("model") if isinstance(source.get("model"), dict) else {}
            provider = source_model.get("provider")
            model = source_model.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise ProviderError("Template assistant does not expose a reusable model provider/model.")
            config = {
                "name": "Raneen CLONED-VOICE TEST " + ident[:8] + " v" + str(behavior["version"]),
                "firstMessage": "هلا، أنا مساعد ذكاء اصطناعي تجريبي، وأستخدم صوت اصطناعي مأذون. كيف أقدر أساعدك؟",
                "model": {
                    "provider": provider,
                    "model": model,
                    "messages": [{"role": "system", "content": behavior_prompt(behavior)}],
                },
                "voice": {
                    "provider": "11labs",
                    "model": os.environ.get("RANEEN_VAPI_VOICE_MODEL", "eleven_multilingual_v2"),
                    "voiceId": row["voice_id"],
                    "stability": 0.45,
                    "similarityBoost": 0.8,
                    "useSpeakerBoost": True,
                },
                "maxDurationSeconds": 180,
                "backgroundSound": "off",
                "artifactPlan": {"recordingEnabled": False},
            }
            if isinstance(source.get("transcriber"), dict):
                allowed = {}
                for key in ("provider", "model", "language"):
                    value = source["transcriber"].get(key)
                    if isinstance(value, (str, int, float, bool)):
                        allowed[key] = value
                if allowed.get("provider") and allowed.get("model"):
                    config["transcriber"] = allowed
            try:
                consent(own_session(ident,user),'private_preview')
            except HTTPException:
                op_update(operation['id'],'cancelled')
                raise
            result = await app.state.enrollment_provider.vapi_json(
                api_key, "POST", "/assistant", json_body=config
            )
        except ProviderError as exc:
            state = "outcome_unknown" if exc.uncertain else "failed"
            op_update(operation["id"], state, detail={"error": str(exc)})
            raise HTTPException(502 if exc.uncertain else 422, str(exc)) from None
        assistant_id = result.get("id")
        if not isinstance(assistant_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", assistant_id):
            op_update(operation["id"], "outcome_unknown", detail={"error": "unverified assistant id"})
            raise HTTPException(502, "Vapi assistant creation result could not be verified.")
        op_update(operation["id"], "succeeded", provider_id=assistant_id, detail={"behavior_id": behavior["id"]})
        store.execute(
            "UPDATE enrollment_sessions SET assistant_id=?,active_behavior_id=?,state=CASE WHEN revoked_at IS NULL THEN 'preview_ready' ELSE 'revoked' END,updated=? WHERE id=?",
            (assistant_id, behavior["id"], now(), ident),
        )
        if own_session(ident,user,require_active=False)['revoked_at']:
            await cleanup_provider_artifacts(ident)
            raise HTTPException(410,'Consent was withdrawn during agent preparation; cleanup was scheduled.')
        return {"assistant_id": assistant_id, "reused": False}

    @app.get("/api/enrollment/sessions/{ident}/preview-config")
    def preview_config(ident: str, user=Depends(admin)):
        own_session(ident,user)
        raise HTTPException(410,'Use the authenticated bounded preview-call endpoint.')

    def workflow(ident: str, user):
        row = own_session(ident,user,require_active=False)
        approved = bool(store.one('SELECT approved FROM enrollment_voice_approvals WHERE session_id=? AND voice_id=?',(ident,row['voice_id'])))
        operations = store.all("SELECT * FROM enrollment_operations WHERE session_id=? ORDER BY created,rowid",(ident,))
        matched = any(x['kind']=='vapi_assistant' and x['state']=='succeeded' and x['provider_id']==row['assistant_id'] and json.loads(x['detail']).get('behavior_id')==row['active_behavior_id'] for x in operations)
        active_behavior=store.one('SELECT payload FROM enrollment_behavior_versions WHERE session_id=? AND id=?',(ident,row['active_behavior_id']))
        trusted_behavior=bool(active_behavior and json.loads(active_behavior['payload']).get('evidence_origin')=='trusted_audio_v1')
        matched=matched and trusted_behavior
        pending = any(x['state'] in {'dispatching','outcome_unknown'} and not x['kind'].startswith('cleanup_') for x in operations)
        call = store.one("SELECT state FROM enrollment_preview_calls WHERE session_id=? ORDER BY created DESC LIMIT 1",(ident,))
        clones = [x for x in operations if x['kind'] == 'voice_clone']
        clone_busy = any(x['state'] in {'dispatching', 'outcome_unknown'} for x in clones)
        last_clone = clones[-1] if clones else None
        clone_retry_allowed = bool(_enabled() and not row['revoked_at'] and not row['voice_id'] and
                                   not clone_busy and len(clones) < MAX_CLONE_ATTEMPTS and retryable_clone(last_clone))
        preview_busy = any(x['kind'] == 'voice_preview' and x['state'] in {'dispatching', 'outcome_unknown'} for x in operations)
        preview_retry_allowed = {}
        voice_failures = []
        if last_clone and not row['voice_id'] and last_clone['state'] in {'failed', 'dispatching', 'outcome_unknown'}:
            voice_failures.append((last_clone['created'], clone_failure(last_clone, len(clones), allowed=clone_retry_allowed)))
        for kind in PREVIEW_TEXT:
            attempts = preview_attempts(operations, row['voice_id'] or '', kind)
            old = attempts[-1] if attempts else None
            allowed = bool(_enabled() and not row['revoked_at'] and row['voice_id'] and not preview_busy and
                           len(attempts) < MAX_CLONE_ATTEMPTS and preview_retry(old))
            preview_retry_allowed[kind] = allowed
            if old and old['state'] in {'failed', 'dispatching', 'outcome_unknown'}:
                voice_failures.append((old['created'], preview_failure(old, len(attempts), allowed=allowed)))
        voice_failure = max(voice_failures, key=lambda x: x[0])[1] if voice_failures else None
        failed_voice = bool(last_clone and not row['voice_id'] and last_clone['state'] == 'failed')
        stage = 'revoked' if row['revoked_at'] else 'blocked' if pending or row['voice_state']=='verification_required' else 'agent_ready' if matched and approved else 'voice_review' if row['voice_state'] in {'sample_required','ready'} else 'preparing' if failed_voice else 'collecting'
        config = {'interview':bool(os.environ.get('OPENAI_API_KEY')), 'voice':bool(os.environ.get('ELEVENLABS_API_KEY')), 'agent':bool(os.environ.get('VAPI_API_KEY'))}
        reason = 'Enrollment was revoked.' if row['revoked_at'] else 'A provider outcome needs reconciliation.' if pending else 'Provider voice verification is required.' if row['voice_state']=='verification_required' else None
        return {'stage':stage,'enabled':_enabled(),'config':config,'voice_state':row['voice_state'],'voice_approved':approved,'behavior_ready':trusted_behavior,'behavior_id':row['active_behavior_id'],'assistant_ready':matched,'assistant_matches_behavior':matched,'preview_allowed':_enabled() and not row['revoked_at'] and approved and matched and not pending and not (call and call['state'] in {'open','dispatching','close_unknown','outcome_unknown'}),'blocking_reason':reason,'operations':[{'kind':x['kind'],'state':x['state']} for x in operations], 'preview_call_state':call['state'] if call else 'none',
                'voice_failure': voice_failure, 'clone_retry_allowed': clone_retry_allowed,
                'clone_attempts': len(clones), 'clone_attempt_limit': MAX_CLONE_ATTEMPTS,
                'preview_retry_allowed': preview_retry_allowed}

    @app.get('/api/enrollment/sessions/{ident}/workflow')
    def get_workflow(ident: str, user=Depends(admin)):
        return workflow(ident,user)

    @app.get('/api/enrollment/sessions/{ident}/quality')
    async def audio_quality(ident: str, user=Depends(admin)):
        own_session(ident,user)
        last = store.one('SELECT MAX(seq) AS final_seq FROM enrollment_chunks WHERE session_id=?', (ident,))
        final_seq = last['final_seq'] if last['final_seq'] is not None else -1
        chosen = []
        error = None
        try:
            try:
                chosen, manifest = await asyncio.to_thread(select_clone_chunks, ident, final_seq)
                measurements = manifest.get('diagnostics', {})
            except HTTPException as exc:
                if exc.status_code != 409 or not isinstance(exc.detail, dict) or 'code' not in exc.detail:
                    raise
                error = exc.detail
                measurements = error.get('details') or {}
            own_session(ident,user)
            result = {
                **measurements,
                'decoded_ms': measurements.get('decoded_source_ms', 0),
                'active_ms': measurements.get('decoded_active_ms', 0),
                'minimum_ms': MIN_CLONE_MS,
                'minimum_sample_ms': MIN_CLONE_MS,
                'minimum_active_ms': MIN_CLONE_ACTIVE_MS,
                'rejected_chunks': measurements.get('rejected_chunks', 0),
                'inspected_chunks': measurements.get('inspected_chunks', 0),
                'partial': measurements.get('partial', error is not None),
                'ready_for_clone': error is None,
                'measurement': 'Selected recording duration and local acoustic activity are separate checks. Activity is not measured speech duration, speaker identity, or a provider quality guarantee.',
            }
            if error:
                result.update(error)
            return result
        finally:
            for sample in chosen:
                Path(sample['path']).unlink(missing_ok=True)

    async def stop_preview_calls(ident: str):
        calls = store.all("SELECT * FROM enrollment_preview_calls WHERE session_id=? AND state IN ('open','close_unknown')",(ident,))
        for call in calls:
            if not call['control_url']:
                store.execute("UPDATE enrollment_preview_calls SET state='close_unknown',updated=? WHERE id=?",(now(),call['id']))
                continue
            try:
                await app.state.enrollment_provider.vapi_end_call(call['control_url'])
                state = 'closed'
            except ProviderError: state = 'close_unknown'
            store.execute('UPDATE enrollment_preview_calls SET state=?,updated=? WHERE id=?',(state,now(),call['id']))
        remaining = store.one("SELECT id FROM enrollment_preview_calls WHERE session_id=? AND state IN ('dispatching','open','close_unknown','outcome_unknown')",(ident,))
        return {'state':'close_unknown' if remaining else 'closed'}

    @app.post('/api/enrollment/sessions/{ident}/preview-call/close')
    async def close_preview_call(ident: str,user=Depends(admin)):
        own_session(ident,user,require_active=False)
        return await stop_preview_calls(ident)

    @app.post('/api/enrollment/sessions/{ident}/preview-call')
    async def create_preview_call(ident: str,body: ExternalApproval,user=Depends(admin)):
        gate()
        row = own_session(ident,user)
        consent(row,'private_preview')
        if not body.approve or not workflow(ident,user)['preview_allowed']:
            raise HTTPException(409,'Approve the prepared voice and agent before starting a test.')
        api_key = _require_key('VAPI_API_KEY')
        call_key = uid()
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revoked_at FROM enrollment_sessions WHERE id=?',(ident,)).fetchone()
            if current['revoked_at']: raise HTTPException(410,'Enrollment was revoked.')
            interview = db.execute("SELECT state FROM enrollment_realtime_calls WHERE session_id=? AND state IN ('open','dispatching','outcome_unknown','close_unknown')",(ident,)).fetchone()
            if interview: raise HTTPException(409,'Pause the interview before starting the agent test.')
            busy = db.execute("SELECT id FROM enrollment_preview_calls WHERE session_id=? AND state IN ('dispatching','open','close_unknown','outcome_unknown')",(ident,)).fetchone()
            if busy: raise HTTPException(409,'Close or reconcile the previous test call first.')
            count = db.execute('SELECT COUNT(*) AS n FROM enrollment_preview_calls WHERE session_id=?',(ident,)).fetchone()['n']
            if count>=3: raise HTTPException(429,'The three-call private test allowance is used. Ask the owner to review usage.')
            db.execute('INSERT INTO enrollment_preview_calls VALUES(?,?,?,?,NULL,NULL,?,?)',(call_key,ident,row['assistant_id'],'dispatching',now(),now()))
        try:
            result = await app.state.enrollment_provider.vapi_json(api_key,'POST','/call',json_body={'assistantId':row['assistant_id'],'transport':{'provider':'daily','roomDeleteOnUserLeaveEnabled':True},'assistantOverrides':{'maxDurationSeconds':180,'monitorPlan':{'listenEnabled':False,'controlEnabled':True},'artifactPlan':{'recordingEnabled':False}}})
            if not isinstance(result,dict): raise ProviderError('Provider test-call response was invalid.',uncertain=True)
            provider_id=result.get('id')
            transport=result.get('transport') or {}; monitor=result.get('monitor') or {}
            if isinstance(provider_id,str) and re.fullmatch(r'[0-9a-fA-F-]{36}',provider_id):
                store.execute('UPDATE enrollment_preview_calls SET provider_id=?,updated=? WHERE id=?',(provider_id,now(),call_key))
            if not isinstance(transport,dict) or not isinstance(monitor,dict): raise ProviderError('Provider test-call response was invalid.',uncertain=True)
            room=result.get('webCallUrl') or transport.get('callUrl'); control=monitor.get('controlUrl')
            if isinstance(control,str):
                control_parts=urlparse(control)
                if control_parts.scheme=='https' and control_parts.hostname and control_parts.hostname.endswith('.vapi.ai') and not control_parts.username and not control_parts.password and control_parts.port in (None,443):
                    store.execute('UPDATE enrollment_preview_calls SET control_url=?,updated=? WHERE id=?',(control,now(),call_key))
                else: raise ProviderError('Provider returned an invalid stop control.',uncertain=True)
            if not isinstance(room,str): raise ProviderError('Provider test-call response lacks a room URL.',uncertain=True)
            parsed=urlparse(room or '')
            if not isinstance(provider_id,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',provider_id) or parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.daily.co') or parsed.username or parsed.password:
                raise ProviderError('Provider test-call response could not be verified.',uncertain=True)
            if not isinstance(control,str): raise ProviderError('Provider did not return a call stop control.',uncertain=True)
        except ProviderError as exc:
            store.execute('UPDATE enrollment_preview_calls SET state=?,updated=? WHERE id=?',('outcome_unknown' if exc.uncertain else 'failed',now(),call_key))
            raise HTTPException(502,str(exc)) from None
        store.execute("UPDATE enrollment_preview_calls SET state='open',provider_id=?,control_url=?,updated=? WHERE id=?",(provider_id,control,now(),call_key))
        if own_session(ident,user,require_active=False)['revoked_at']:
            await stop_preview_calls(ident)
            raise HTTPException(410,'Consent was withdrawn while creating this call.')
        return {'call_id':provider_id,'web_call_url':room,'call_token':transport.get('callToken'),'max_duration_seconds':180}

    @app.post("/api/enrollment/sessions/{ident}/revoke")
    async def revoke(ident: str, body: RevokeRequest, user=Depends(admin)):
        if not body.confirm:
            raise HTTPException(403, "Confirm revocation.")
        own_session(ident, user, require_active=False)
        stamp = now()
        store.execute(
            "UPDATE enrollment_sessions SET state='revoked',revoked_at=COALESCE(revoked_at,?),updated=? WHERE id=?",
            (stamp, stamp, ident),
        )
        store.audit(user["id"], "enrollment_revoked", ident, {"provider_cleanup": "started"})
        try:
            await close_webrtc(ident,user)
        except HTTPException:
            pass  # Known failed hangup remains close_unknown for retry.
        await stop_preview_calls(ident)
        cleanup = await cleanup_provider_artifacts(ident)
        return {
            "state": "revoked",
            "local_use_blocked": True,
            "provider_cleanup": cleanup,
            "note": (
                "Known external artifacts are deleted with tracked idempotent operations. "
                "Unknown create outcomes require operator reconciliation; universal deletion "
                "of backups or prior exports is never claimed."
            ),
        }

    @app.post("/api/enrollment/sessions/{ident}/cleanup")
    async def retry_cleanup(ident: str, user=Depends(admin)):
        row = own_session(ident, user, require_active=False)
        if not row["revoked_at"]:
            raise HTTPException(409, "Cleanup is only available after revocation.")
        return {"provider_cleanup": await cleanup_provider_artifacts(ident)}

    static = Path(__file__).parent / "static"

    @app.get("/enroll", include_in_schema=False)
    def enrollment_page():
        return FileResponse(static / "enroll.html")

    @app.get("/vapi-frame", include_in_schema=False)
    def vapi_frame():
        # Isolated call surface receives only the server-created, duration-bounded
        # Daily room credential. Reusable provider API keys are never sent here.
        return FileResponse(static / "vapi-frame.html")
