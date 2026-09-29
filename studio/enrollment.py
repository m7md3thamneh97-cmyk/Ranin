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

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .app import now, token_hash, uid

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
CHUNK_MAX = 80 * 1024
SESSION_MAX = 220 * 1024 * 1024
MIN_CLONE_MS = 60_000
MAX_CLONE_MS = 125_000
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

class PreviewRequest(Strict):
    approve: bool = False
    kind: Literal["question", "number", "correction"]

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
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


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

    async def eleven_clone(self, api_key: str, name: str, files: list[tuple[str, Path, str]]) -> dict:
        opened = []
        try:
            multipart = []
            for filename, path, mime in files:
                handle = path.open("rb")
                opened.append(handle)
                multipart.append(("files[]", (filename, handle, mime)))
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
            raise ProviderError("ElevenLabs clone request failed.", uncertain=False) from exc
        finally:
            for handle in opened:
                handle.close()
        if r.status_code >= 500:
            raise ProviderError(f"ElevenLabs clone returned HTTP {r.status_code}.", uncertain=True)
        if r.status_code >= 300:
            raise ProviderError(f"ElevenLabs clone returned HTTP {r.status_code}.", uncertain=False)
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError("ElevenLabs clone returned invalid JSON.", uncertain=True) from exc
        voice_id = data.get("voice_id")
        if not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", voice_id):
            raise ProviderError("ElevenLabs clone result did not contain a valid voice ID.", uncertain=True)
        return {"voice_id": voice_id, "requires_verification": bool(data.get("requires_verification"))}

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
            raise ProviderError("ElevenLabs speech synthesis failed.") from exc
        if r.status_code >= 300:
            raise ProviderError(f"ElevenLabs speech synthesis returned HTTP {r.status_code}.")
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
            raise ProviderError(f"Vapi returned HTTP {r.status_code}.")
        try:
            return r.json()
        except ValueError as exc:
            raise ProviderError("Vapi returned invalid JSON.", uncertain=method != "GET") from exc


def install(app):
    store = app.state.store
    with store.db() as db:
        db.executescript(SCHEMA)
    app.state.enrollment_provider = Providers()
    root = store.root / "enrollments"
    root.mkdir(exist_ok=True)

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
        old = op_existing(session_id, kind, op_key)
        if old:
            return old, False
        ident = uid()
        try:
            store.execute(
                "INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,'{}',?,?,?)",
                (ident, session_id, kind, op_key, "dispatching", now(), now()),
            )
        except sqlite3.IntegrityError:
            return op_existing(session_id, kind, op_key), False
        return op_existing(session_id, kind, op_key), True

    def op_update(ident: str, state: str, *, provider_id=None, detail=None):
        store.execute(
            "UPDATE enrollment_operations SET state=?,provider_id=COALESCE(?,provider_id),detail=?,updated=? WHERE id=?",
            (state, provider_id, _json(detail or {}), now(), ident),
        )

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
        active = store.one(
            "SELECT id FROM enrollment_sessions WHERE owner_id=? AND revoked_at IS NULL AND state NOT IN ('complete','failed') ORDER BY created DESC LIMIT 1",
            (user["id"],),
        )
        if active:
            return {"id": active["id"], "resumed": True}
        ident = uid()
        payload = body.model_dump() | {"text": CONSENT_TEXT}
        stamp = now()
        store.execute(
            "INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated) VALUES(?,?,?,?,?,?,?)",
            (ident, user["id"], "collecting", CONSENT_VERSION, _json(payload), stamp, stamp),
        )
        (root / ident / "chunks").mkdir(parents=True, exist_ok=True)
        (root / ident / "previews").mkdir(parents=True, exist_ok=True)
        store.audit(user["id"], "enrollment_started", ident, {"version": CONSENT_VERSION})
        return {"id": ident, "resumed": False}

    @app.get("/api/enrollment/sessions/{ident}")
    def get_session(ident: str, user=Depends(admin)):
        gate()
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
            "clean_ms": row["clean_ms"],
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
        gate()
        row = own_session(ident, user)
        consent(row, "external_processing")
        store.execute(
            "INSERT INTO enrollment_transcripts(session_id,item_id,transcript,created) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id,item_id) DO UPDATE SET transcript=excluded.transcript",
            (ident, body.item_id, body.transcript, now()),
        )
        return {"saved": True}

    @app.post("/api/enrollment/sessions/{ident}/tool")
    def tool_event(ident: str, body: ToolEvent, user=Depends(admin)):
        gate()
        row = own_session(ident, user)
        consent(row, "external_processing")
        if len(_json(body.arguments)) > 8000:
            raise HTTPException(422, "Tool arguments are too large.")
        if body.name == "propose_evidence":
            existing = store.one("SELECT id,status FROM enrollment_evidence WHERE id=?", (body.call_id,))
            if existing:
                return {
                    "ok": True,
                    "evidence_id": existing["id"],
                    "status": existing["status"],
                    "instruction": "Continue from the stored proposal without duplicating it.",
                }
            count = store.one(
                "SELECT COUNT(*) AS n FROM enrollment_evidence WHERE session_id=?", (ident,)
            )["n"]
            if count >= MAX_EVIDENCE:
                raise HTTPException(409, "Evidence allowance reached for this enrollment.")
            transcript = None
            if body.source_item_id:
                transcript = store.one(
                    "SELECT transcript FROM enrollment_transcripts WHERE session_id=? AND item_id=?",
                    (ident, body.source_item_id),
                )
            allowed = {"kind", "situation", "interpretation", "change_condition"}
            payload = {k: body.arguments.get(k) for k in allowed}
            payload["source_transcript"] = transcript["transcript"] if transcript else None
            store.execute(
                "INSERT INTO enrollment_evidence VALUES(?,?,?,?,?,'pending',NULL,?,NULL)",
                (
                    body.call_id,
                    ident,
                    body.source_item_id,
                    str(body.arguments.get("kind", "response_pattern"))[:80],
                    _json(payload),
                    now(),
                ),
            )
            return {
                "ok": True,
                "evidence_id": body.call_id,
                "status": "pending",
                "instruction": "Ask the contributor one specific spoken confirmation of this interpretation before continuing.",
            }

        evidence_id = str(body.arguments.get("evidence_id", ""))
        ev = store.one(
            "SELECT * FROM enrollment_evidence WHERE id=? AND session_id=?", (evidence_id, ident)
        )
        if not ev:
            raise HTTPException(404, "Evidence proposal not found.")
        if ev["status"] == "confirmed":
            return {"ok": True, "evidence_id": evidence_id, "status": "confirmed"}
        accepted = body.arguments.get("accepted")
        if not isinstance(accepted, bool):
            raise HTTPException(422, "Confirmation must include accepted=true or false.")
        confirmation = None
        if body.source_item_id:
            confirmation = store.one(
                "SELECT transcript FROM enrollment_transcripts WHERE session_id=? AND item_id=?",
                (ident, body.source_item_id),
            )
        if accepted:
            store.execute(
                "UPDATE enrollment_evidence SET status='confirmed',confirmation_transcript=?,confirmed_at=? WHERE id=?",
                (confirmation["transcript"] if confirmation else None, now(), evidence_id),
            )
            return {"ok": True, "evidence_id": evidence_id, "status": "confirmed"}
        correction = str(body.arguments.get("correction", "")).strip()
        store.execute(
            "UPDATE enrollment_evidence SET status='rejected',confirmation_transcript=?,confirmed_at=? WHERE id=?",
            (confirmation["transcript"] if confirmation else correction[:6000], now(), evidence_id),
        )
        return {
            "ok": True,
            "evidence_id": evidence_id,
            "status": "rejected",
            "instruction": "Use the contributor's correction, then propose a new precise interpretation and confirm it again.",
        }

    @app.post("/api/enrollment/sessions/{ident}/realtime-secret")
    async def realtime_secret(ident: str, user=Depends(admin)):
        gate()
        row = own_session(ident, user)
        consent(row, "external_processing")
        api_key = _require_key("OPENAI_API_KEY")
        instructions = f"""
You are the Raneen voice-enrollment interviewer. The speaker is teaching an AI how
they naturally speak and handle customer situations. Converse naturally in the
speaker's own language/dialect; do not force Emirati Arabic or formal Arabic.
Your target session is 20-30 minutes, but adapt to the person.

Alternate between: natural conversation, realistic customer role-play, asking what
mattered in a response, and changing one important fact to learn when their answer
changes. Keep your turns short. Never dictate an ideal answer before collecting the
speaker's answer. Preserve corrections, numbers, negation, names, and code-switching.

When you have learned a reusable response pattern, call propose_evidence. After the
tool returns an evidence_id, ask ONE specific spoken confirmation such as "So when X
happens, you first do Y -- is that right?" On the speaker's next answer call
confirm_evidence with that evidence_id. If they reject it, summarize their correction,
propose a replacement, and confirm again. Do not claim that model weights are being
trained. Do not claim to be the contributor. This is an internal AI enrollment session.

Session id: {ident}
"""
        tools = [
            {
                "type": "function",
                "name": "propose_evidence",
                "description": "Propose one reusable response/decision/style pattern learned from the contributor. The app will require spoken confirmation.",
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
                "description": "Record the contributor's spoken acceptance or rejection of a previously proposed evidence item.",
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
        ]
        session = {
            "type": "realtime",
            "model": os.environ.get("RANEEN_REALTIME_MODEL", "gpt-realtime-2.1"),
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "output": {"voice": os.environ.get("RANEEN_INTERVIEWER_VOICE", "marin")},
                "input": {
                    "transcription": {
                        "model": os.environ.get("RANEEN_LIVE_TRANSCRIBE_MODEL", "gpt-live-transcribe"),
                        "prompt": "Transcribe the speaker verbatim. Preserve colloquial Arabic, English words, numbers, negation, and names. Do not formalize.",
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
            },
            "tools": tools,
            "tool_choice": "auto",
        }
        safety_id = hashlib.sha256(("raneen:" + user["id"]).encode()).hexdigest()[:64]
        try:
            result = await app.state.enrollment_provider.openai_realtime_secret(api_key, safety_id, session)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        return result

    @app.post("/api/enrollment/sessions/{ident}/behavior", status_code=201)
    def build_behavior(ident: str, body: ExternalApproval, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve compiling the confirmed spoken evidence.")
        row = own_session(ident, user)
        confirmed = store.all(
            "SELECT id,kind,payload,source_item_id,confirmation_transcript FROM enrollment_evidence "
            "WHERE session_id=? AND status='confirmed' ORDER BY created",
            (ident,),
        )
        if not confirmed:
            raise HTTPException(409, "No confirmed spoken evidence is available yet.")
        payload = {
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

    @app.post("/api/enrollment/sessions/{ident}/clone")
    async def create_clone(ident: str, body: ExternalApproval, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve creating the private voice clone.")
        row = own_session(ident, user)
        consent(row, "voice_cloning")
        consent(row, "external_processing")
        if row["voice_state"] == "ready" and row["voice_id"]:
            return {"state": "ready", "voice_id": row["voice_id"], "reused": True}
        if row["voice_state"] in {"verification_required", "outcome_unknown"}:
            return {"state": row["voice_state"], "voice_id": row["voice_id"], "reused": True}
        chunks = store.all(
            "SELECT seq,duration_ms,mime,path FROM enrollment_chunks WHERE session_id=? AND role='contributor' ORDER BY seq",
            (ident,),
        )
        chosen = []
        total = 0
        for chunk in chunks:
            if total >= MAX_CLONE_MS:
                break
            path = Path(chunk["path"])
            if not path.is_file() or path.is_symlink():
                continue
            chosen.append(chunk)
            total += chunk["duration_ms"]
        minimum = int(os.environ.get("RANEEN_CLONE_MIN_MS", str(MIN_CLONE_MS)))
        if total < minimum:
            raise HTTPException(409, f"Need more clean contributor speech before cloning ({total//1000}s captured).")
        operation, fresh = op_start(ident, "voice_clone", "ivc-v1")
        if not fresh:
            if operation["state"] == "succeeded":
                return {"state": "ready", "voice_id": operation["provider_id"], "reused": True}
            raise HTTPException(409, f"Voice clone operation is {operation['state']}; reconcile it before retrying.")
        api_key = _require_key("ELEVENLABS_API_KEY")
        files = []
        for chunk in chosen:
            path = Path(chunk["path"])
            suffix = path.suffix or ".webm"
            files.append((f"sample-{chunk['seq']:04d}{suffix}", path, chunk["mime"]))
        try:
            result = await app.state.enrollment_provider.eleven_clone(
                api_key, "Raneen private enrollment " + ident[:8], files
            )
        except ProviderError as exc:
            state = "outcome_unknown" if exc.uncertain else "failed"
            op_update(operation["id"], state, detail={"error": str(exc)})
            store.execute(
                "UPDATE enrollment_sessions SET voice_state=?,updated=? WHERE id=?",
                (state, now(), ident),
            )
            raise HTTPException(502 if exc.uncertain else 422, str(exc)) from None
        state = "verification_required" if result["requires_verification"] else "ready"
        op_update(operation["id"], "succeeded", provider_id=result["voice_id"], detail={"requires_verification": result["requires_verification"]})
        store.execute(
            "UPDATE enrollment_sessions SET voice_id=?,voice_state=?,updated=? WHERE id=?",
            (result["voice_id"], state, now(), ident),
        )
        return {"state": state, "voice_id": result["voice_id"], "reused": False}

    @app.post("/api/enrollment/sessions/{ident}/preview")
    async def synth_preview(ident: str, body: PreviewRequest, user=Depends(admin)):
        gate()
        if not body.approve:
            raise HTTPException(403, "Approve generating fresh synthetic preview audio.")
        row = own_session(ident, user)
        consent(row, "voice_cloning")
        if row["voice_state"] != "ready" or not row["voice_id"]:
            raise HTTPException(409, "Voice clone is not ready for synthesis.")
        key = f"{row['voice_id']}:{body.kind}"
        operation, fresh = op_start(ident, "voice_preview", key)
        preview_path = root / ident / "previews" / (body.kind + ".mp3")
        if not fresh:
            if operation["state"] == "succeeded" and preview_path.is_file():
                return FileResponse(preview_path, media_type="audio/mpeg")
            raise HTTPException(409, f"Preview operation is {operation['state']}; reconcile it before retrying.")
        try:
            audio = await app.state.enrollment_provider.eleven_speech(
                _require_key("ELEVENLABS_API_KEY"), row["voice_id"], PREVIEW_TEXT[body.kind]
            )
        except ProviderError as exc:
            op_update(operation["id"], "failed", detail={"error": str(exc)})
            raise HTTPException(502, str(exc)) from None
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(audio)
        op_update(operation["id"], "succeeded", provider_id=row["voice_id"], detail={"kind": body.kind})
        return FileResponse(preview_path, media_type="audio/mpeg")

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
        op_key = row["voice_id"] + ":" + behavior["digest"]
        operation, fresh = op_start(ident, "vapi_assistant", op_key)
        if not fresh:
            if operation["state"] == "succeeded":
                return {"assistant_id": operation["provider_id"], "reused": True}
            raise HTTPException(409, f"Assistant creation is {operation['state']}; reconcile it before retrying.")
        api_key = _require_key("VAPI_API_KEY")
        template_id = _require_key("RANEEN_VAPI_TEMPLATE_ID")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", template_id):
            op_update(operation["id"], "failed", detail={"error": "invalid template id"})
            raise HTTPException(503, "RANEEN_VAPI_TEMPLATE_ID must be a Vapi assistant UUID.")
        try:
            source = await app.state.enrollment_provider.vapi_json(
                api_key, "GET", "/assistant/" + template_id
            )
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
            "UPDATE enrollment_sessions SET assistant_id=?,active_behavior_id=?,state='preview_ready',updated=? WHERE id=?",
            (assistant_id, behavior["id"], now(), ident),
        )
        return {"assistant_id": assistant_id, "reused": False}

    @app.get("/api/enrollment/sessions/{ident}/preview-config")
    def preview_config(ident: str, user=Depends(admin)):
        gate()
        row = own_session(ident, user)
        consent(row, "private_preview")
        if not row["assistant_id"]:
            raise HTTPException(409, "Create the isolated preview assistant first.")
        return {
            "assistant_id": row["assistant_id"],
            "public_key": _safe_public_key(),
            "widget_src": "https://unpkg.com/@vapi-ai/client-sdk-react/dist/embed/widget.umd.js",
            "max_duration_seconds": 180,
        }

    @app.post("/api/enrollment/sessions/{ident}/revoke")
    def revoke(ident: str, body: RevokeRequest, user=Depends(admin)):
        gate()
        if not body.confirm:
            raise HTTPException(403, "Confirm revocation.")
        own_session(ident, user, require_active=False)
        stamp = now()
        store.execute(
            "UPDATE enrollment_sessions SET state='revoked',revoked_at=?,updated=? WHERE id=? AND revoked_at IS NULL",
            (stamp, stamp, ident),
        )
        store.audit(user["id"], "enrollment_revoked", ident, {"provider_cleanup": "pending"})
        return {
            "state": "revoked",
            "local_use_blocked": True,
            "provider_cleanup": "pending",
            "note": "External artifacts require tracked provider deletion; universal deletion is not claimed.",
        }

    static = Path(__file__).parent / "static"

    @app.get("/enroll", include_in_schema=False)
    def enrollment_page():
        return FileResponse(static / "enroll.html")
