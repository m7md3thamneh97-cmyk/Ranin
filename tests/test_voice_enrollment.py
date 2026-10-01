"""Voice enrollment Gate A tests. Providers and audio are synthetic/mocked."""
import hashlib
import os
import subprocess
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from studio.enrollment import ProviderError
from studio.runtime import create_app


@lru_cache(maxsize=32)
def synthetic_audio(seq=0, fmt='webm', duration=15):
    """Genuine locally synthesized tone fixtures; never real enrollment speech."""
    return subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',f'sine=frequency={240+seq*13}:sample_rate=16000:duration={duration}', '-c:a','libopus' if fmt=='webm' else 'libmp3lame','-b:a','20k' if fmt=='webm' else '64k','-f',fmt,'pipe:1'],check=True,capture_output=True).stdout


def auth(user):
    return {"Authorization": "Bearer " + user["token"]}


def scopes():
    return {
        "self_attestation": True,
        "recording": True,
        "external_processing": True,
        "voice_cloning": True,
        "private_preview": True,
    }


class FakeProviders:
    def __init__(self):
        self.clone_calls = 0
        self.speech_calls = []
        self.vapi_calls = []
        self.secret_calls = 0
        self.hangup_calls = 0
        self.deleted_voices = []
        self.deleted_assistants = []
        self.clone_error = None
        self.requires_verification = False

    async def openai_realtime_secret(self, api_key, safety_id, session):
        self.secret_calls += 1
        assert api_key == "test-openai"
        assert safety_id and session["type"] == "realtime"
        assert any(x["name"] == "propose_evidence" for x in session["tools"])
        return {"value": "ek_test_ephemeral", "expires_at": 9999999999}

    async def openai_create_call(self, api_key, safety_id, sdp, session):
        self.secret_calls += 1
        assert api_key == "test-openai"
        assert safety_id and sdp.startswith("v=")
        assert session["type"] == "realtime"
        assert any(x["name"] == "propose_evidence" for x in session["tools"])
        return {"call_id": "rtc_test_123456", "sdp": "v=0\r\ns=mock\r\n"}

    async def openai_hangup(self, api_key, call_id):
        assert api_key == "test-openai"
        assert call_id == "rtc_test_123456"
        self.hangup_calls += 1

    async def eleven_clone(self, api_key, name, files):
        self.clone_calls += 1
        assert api_key == "test-eleven"
        assert files and all("sample-" in x[0] for x in files)
        if self.clone_error:
            raise self.clone_error
        return {"voice_id": "voice_test_123456", "requires_verification": self.requires_verification}

    async def eleven_delete_voice(self, api_key, voice_id):
        assert api_key == "test-eleven"
        self.deleted_voices.append(voice_id)

    async def eleven_speech(self, api_key, voice_id, text):
        assert api_key == "test-eleven"
        assert voice_id == "voice_test_123456"
        self.speech_calls.append(text)
        return synthetic_audio(fmt="mp3",duration=1)

    async def vapi_delete_assistant(self, api_key, assistant_id):
        assert api_key == "test-vapi-private"
        self.deleted_assistants.append(assistant_id)

    async def vapi_json(self, api_key, method, path, *, json_body=None):
        assert api_key == "test-vapi-private"
        self.vapi_calls.append((method, path, json_body))
        if method == "GET":
            return {
                "model": {"provider": "openai", "model": "gpt-4.1-mini"},
                "transcriber": {"provider": "speechmatics", "model": "enhanced", "language": "ar_en"},
                "tools": [{"type": "transferCall"}],
                "serverUrl": "https://must-not-copy.example",
            }
        return {"id": "11111111-2222-3333-4444-555555555555"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in [
        "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "VAPI_API_KEY",
        "VAPI_PUBLIC_API_KEY", "RANEEN_VAPI_TEMPLATE_ID",
        "RANEEN_VOICE_ENROLLMENT_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)
    app = create_app(tmp_path)
    owner = app.state.store.create_user("Owner", "admin")
    other = app.state.store.create_user("Other", "admin")
    fake = FakeProviders()
    app.state.enrollment_provider = fake
    app.state.enrollment_sideband = FakeSideband()
    return TestClient(app), app, owner, other, fake


def enable(monkeypatch):
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven")
    monkeypatch.setenv("VAPI_API_KEY", "test-vapi-private")
    monkeypatch.setenv("VAPI_PUBLIC_API_KEY", "test-vapi-public")
    monkeypatch.setenv("RANEEN_VAPI_TEMPLATE_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def start(c, owner):
    r = c.post("/api/enrollment/sessions", headers=auth(owner), json=scopes())
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(c, owner, sid, seq, duration=15000, payload=None):
    data = payload if payload is not None else synthetic_audio(seq=seq,duration=duration/1000)
    headers = auth(owner) | {
        "Content-Type": "audio/webm",
        "X-Speaker-Role": "contributor",
        "X-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
        "X-Duration-Ms": str(duration),
    }
    return c.put("/api/enrollment/sessions/%s/chunks/%d" % (sid, seq), headers=headers, content=data)


class FakeSideband:
    """Synthetic transport: no real network or provider events in route tests."""
    async def attach(self, session_id, call_id, api_key):
        pass

    async def close(self, session_id, call_id=None):
        pass

    async def stop_all(self):
        pass


def confirm_pattern(c, owner, sid, suffix="1"):
    """Inject labeled synthetic PROVIDER evidence, never browser-authored text."""
    from studio.app import now
    from studio.enrollment_evidence import EvidenceService
    store = c.app.state.store
    evidence = EvidenceService(store)
    call_id = "rtc_synthetic_evidence_" + suffix
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET call_id=excluded.call_id,state='open'", (sid, call_id, "open", now(), now()))
    first, second = "synthetic-answer-" + suffix, "synthetic-approval-" + suffix
    evidence.mark_audio(sid, call_id, first)
    evidence.mark_audio(sid, call_id, first, committed=True)
    evidence.record_transcript(sid, call_id, first, "لا، مليون ونص، مش مليونين.")
    proposal = evidence.propose(sid, call_id, "synthetic-tool-" + suffix, {
        "kind": "decision_rule", "situation": "The caller corrects a budget.",
        "interpretation": "Acknowledge the corrected number before asking the next question.",
        "change_condition": "If the amount is still ambiguous, confirm it first.",
        "replaces_id": "",
    })
    assert proposal["ok"]
    assert evidence.verify_readback(sid, call_id, proposal["challenge_nonce"], proposal["challenge_text"])
    evidence.mark_audio(sid, call_id, second)
    evidence.mark_audio(sid, call_id, second, committed=True)
    result = evidence.record_transcript(sid, call_id, second, "Yes, save this.")
    assert result["status"] == "confirmed"
    store.execute("UPDATE enrollment_realtime_calls SET state='closed' WHERE session_id=? AND call_id=?", (sid, call_id))
    return proposal["evidence_id"]


def test_feature_default_off_and_owner_isolation(env, monkeypatch):
    c, app, owner, other, fake = env
    status = c.get("/api/enrollment/status", headers=auth(owner))
    assert status.status_code == 200 and status.json()["enabled"] is False
    assert c.get("/api/enrollment/consent", headers=auth(owner)).status_code == 404
    enable(monkeypatch)
    sid = start(c, owner)
    assert c.get("/api/enrollment/sessions/" + sid, headers=auth(other)).status_code == 403


def test_scopes_and_resumable_ordered_chunks(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    bad = scopes()
    bad["voice_cloning"] = False
    assert c.post("/api/enrollment/sessions", headers=auth(owner), json=bad).status_code == 422
    sid = start(c, owner)
    assert upload(c, owner, sid, 2).status_code == 200
    assert upload(c, owner, sid, 0).status_code == 200
    first = upload(c, owner, sid, 1)
    assert first.status_code == 200
    duplicate = upload(c, owner, sid, 1)
    assert duplicate.status_code == 200 and duplicate.json()["deduplicated"] is True
    conflict = upload(c, owner, sid, 1, payload=b"different" * 200)
    assert conflict.status_code == 409
    interviewer = auth(owner) | {
        "Content-Type": "audio/webm",
        "X-Speaker-Role": "interviewer",
        "X-Chunk-Sha256": hashlib.sha256(b"x").hexdigest(),
        "X-Duration-Ms": "1000",
    }
    assert c.put("/api/enrollment/sessions/%s/chunks/3" % sid, headers=interviewer, content=b"x").status_code == 422
    state = c.get("/api/enrollment/sessions/" + sid, headers=auth(owner)).json()
    assert [x["seq"] for x in state["chunks"]] == [0, 1, 2]


def test_realtime_evidence_and_behavior_versions(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(c, owner)
    rtc = c.post(
        "/api/enrollment/sessions/%s/webrtc" % sid,
        headers=auth(owner) | {"Content-Type": "application/sdp"},
        content=b"v=0\r\ns=test\r\n",
    )
    assert rtc.status_code == 200 and rtc.text.startswith("v=0")
    assert fake.secret_calls == 1
    closed = c.post("/api/enrollment/sessions/%s/webrtc-close" % sid, headers=auth(owner), json={})
    assert closed.status_code == 200 and closed.json()["state"] == "closed"
    assert fake.hangup_calls == 1
    confirm_pattern(c, owner, sid, "1")
    v1 = c.post("/api/enrollment/sessions/%s/behavior" % sid, headers=auth(owner), json={"approve": True})
    assert v1.status_code == 201
    assert "مليون ونص" in app.state.store.one(
        "SELECT payload FROM enrollment_behavior_versions WHERE id=?", (v1.json()["id"],)
    )["payload"]
    confirm_pattern(c, owner, sid, "2")
    v2 = c.post("/api/enrollment/sessions/%s/behavior" % sid, headers=auth(owner), json={"approve": True})
    assert v2.status_code == 201
    assert v2.json()["version"] == v1.json()["version"] + 1


def test_clone_fresh_previews_and_vapi_assistant_reuse_voice(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(c, owner)
    for seq in range(4):
        assert upload(c, owner, sid, seq).status_code == 200
    confirm_pattern(c, owner, sid)
    behavior = c.post("/api/enrollment/sessions/%s/behavior" % sid, headers=auth(owner), json={"approve": True}).json()
    clone = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True,"final_seq":3})
    assert clone.status_code == 200 and clone.json()["state"] == "sample_required"
    assert clone.json()["voice_version_id"]
    voice_version_id = clone.json()["voice_version_id"]
    version_row = app.state.store.one(
        "SELECT * FROM enrollment_voice_versions WHERE id=?", (voice_version_id,)
    )
    manifest = __import__("json").loads(version_row["sample_manifest"])
    assert version_row["provider_voice_id"] == "voice_test_123456"
    assert version_row["state"] == "sample_required"
    assert manifest["total_ms"] >= 60000
    assert [x["seq"] for x in manifest["chunks"]] == [0, 1, 2, 3]
    assert all(len(x["sha256"]) == 64 for x in manifest["chunks"])
    assert fake.clone_calls == 1
    for kind in ["question", "number", "correction"]:
        preview = c.post(
            "/api/enrollment/sessions/%s/preview" % sid,
            headers=auth(owner),
            json={"approve": True, "kind": kind},
        )
        assert preview.status_code == 200 and preview.content.startswith(b"ID3")
    assert len(fake.speech_calls) == 3
    assert c.post(f"/api/enrollment/sessions/{sid}/voice-approval",headers=auth(owner),json={"approve":True}).status_code == 200
    assistant = c.post(
        "/api/enrollment/sessions/%s/assistant" % sid,
        headers=auth(owner),
        json={"approve": True, "behavior_id": behavior["id"]},
    )
    assert assistant.status_code == 200
    sent = [x for x in fake.vapi_calls if x[0] == "POST"][0][2]
    assert sent["voice"]["voiceId"] == "voice_test_123456"
    assert "tools" not in sent and "serverUrl" not in sent
    cfg = c.get("/api/enrollment/sessions/%s/preview-config" % sid, headers=auth(owner))
    assert cfg.status_code == 410
    assert "test-vapi-public" not in cfg.text
    confirm_pattern(c, owner, sid, "later")
    v2 = c.post("/api/enrollment/sessions/%s/behavior" % sid, headers=auth(owner), json={"approve": True}).json()
    assert v2["version"] == 2
    reused = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True,"final_seq":3}).json()
    assert reused["reused"] is True
    assert reused["voice_version_id"] == voice_version_id
    assert fake.clone_calls == 1
    assert app.state.store.one(
        "SELECT COUNT(*) AS n FROM enrollment_voice_versions WHERE session_id=?", (sid,)
    )["n"] == 1


def test_unknown_clone_outcome_blocks_duplicate_retry(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(c, owner)
    for seq in range(4):
        upload(c, owner, sid, seq)
    fake.clone_error = ProviderError("timeout", uncertain=True)
    first = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True,"final_seq":3})
    assert first.status_code == 502
    second = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True,"final_seq":3})
    assert second.status_code == 200 and second.json()["state"] == "outcome_unknown"
    assert fake.clone_calls == 1


def test_revocation_blocks_local_use(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(c, owner)
    assert upload(c, owner, sid, 0).status_code == 200
    r = c.post("/api/enrollment/sessions/%s/revoke" % sid, headers=auth(owner), json={"confirm": True})
    assert r.status_code == 200 and r.json()["local_use_blocked"] is True
    assert upload(c, owner, sid, 1).status_code == 410


def test_revocation_deletes_known_provider_artifacts_and_is_idempotent(env, monkeypatch):
    c, app, owner, _, fake = env
    enable(monkeypatch)
    sid = start(c, owner)
    for seq in range(4):
        upload(c, owner, sid, seq)
    confirm_pattern(c, owner, sid)
    behavior = c.post(
        "/api/enrollment/sessions/%s/behavior" % sid,
        headers=auth(owner),
        json={"approve": True},
    ).json()
    clone = c.post(
        "/api/enrollment/sessions/%s/clone" % sid,
        headers=auth(owner),
        json={"approve": True,"final_seq":3},
    )
    assert clone.status_code == 200
    for kind in ['question','number','correction']:
        assert c.post(f'/api/enrollment/sessions/{sid}/preview',headers=auth(owner),json={'approve':True,'kind':kind}).status_code==200
    assert c.post(f'/api/enrollment/sessions/{sid}/voice-approval',headers=auth(owner),json={'approve':True}).status_code==200
    assistant = c.post(
        "/api/enrollment/sessions/%s/assistant" % sid,
        headers=auth(owner),
        json={"approve": True, "behavior_id": behavior["id"]},
    )
    assert assistant.status_code == 200
    revoked = c.post(
        "/api/enrollment/sessions/%s/revoke" % sid,
        headers=auth(owner),
        json={"confirm": True},
    )
    assert revoked.status_code == 200
    assert revoked.json()["provider_cleanup"] == "complete"
    assert fake.deleted_voices == ["voice_test_123456"]
    assert fake.deleted_assistants == ["11111111-2222-3333-4444-555555555555"]
    # Delete is idempotent: the cleanup endpoint observes succeeded operations and
    # does not issue the provider DELETE again.
    retry = c.post("/api/enrollment/sessions/%s/cleanup" % sid, headers=auth(owner), json={})
    assert retry.status_code == 200 and retry.json()["provider_cleanup"] == "complete"
    assert len(fake.deleted_voices) == 1 and len(fake.deleted_assistants) == 1
