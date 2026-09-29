"""Voice enrollment Gate A tests. Providers and audio are synthetic/mocked."""
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from studio.enrollment import ProviderError
from studio.runtime import create_app


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

    async def eleven_speech(self, api_key, voice_id, text):
        assert api_key == "test-eleven"
        assert voice_id == "voice_test_123456"
        self.speech_calls.append(text)
        return b"ID3" + b"x" * 1500

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
    data = payload if payload is not None else (("chunk-%d-" % seq).encode() + b"x" * 1200)
    headers = auth(owner) | {
        "Content-Type": "audio/webm",
        "X-Speaker-Role": "contributor",
        "X-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
        "X-Duration-Ms": str(duration),
    }
    return c.put("/api/enrollment/sessions/%s/chunks/%d" % (sid, seq), headers=headers, content=data)


def confirm_pattern(c, owner, sid, suffix="1"):
    first = "u-" + suffix
    second = "c-" + suffix
    evidence = "ev-" + suffix
    assert c.post(
        "/api/enrollment/sessions/%s/transcripts" % sid,
        headers=auth(owner),
        json={"item_id": first, "transcript": "لا، مليون ونص، مش مليونين."},
    ).status_code == 200
    r = c.post(
        "/api/enrollment/sessions/%s/tool" % sid,
        headers=auth(owner),
        json={
            "call_id": evidence,
            "name": "propose_evidence",
            "source_item_id": first,
            "arguments": {
                "kind": "decision_rule",
                "situation": "The caller corrects a budget.",
                "interpretation": "Acknowledge the corrected number before asking the next question.",
                "change_condition": "If the amount is still ambiguous, confirm it first.",
            },
        },
    )
    assert r.status_code == 200, r.text
    assert c.post(
        "/api/enrollment/sessions/%s/transcripts" % sid,
        headers=auth(owner),
        json={"item_id": second, "transcript": "آه، بالضبط."},
    ).status_code == 200
    r = c.post(
        "/api/enrollment/sessions/%s/tool" % sid,
        headers=auth(owner),
        json={
            "call_id": "confirm-call-" + suffix,
            "name": "confirm_evidence",
            "source_item_id": second,
            "arguments": {"evidence_id": evidence, "accepted": True, "correction": ""},
        },
    )
    assert r.status_code == 200
    return evidence


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
    clone = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True})
    assert clone.status_code == 200 and clone.json()["state"] == "ready"
    assert clone.json()["voice_version_id"]
    voice_version_id = clone.json()["voice_version_id"]
    version_row = app.state.store.one(
        "SELECT * FROM enrollment_voice_versions WHERE id=?", (voice_version_id,)
    )
    manifest = __import__("json").loads(version_row["sample_manifest"])
    assert version_row["provider_voice_id"] == "voice_test_123456"
    assert version_row["state"] == "ready"
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
    assert cfg.status_code == 200
    assert cfg.json()["public_key"] == "test-vapi-public"
    confirm_pattern(c, owner, sid, "later")
    v2 = c.post("/api/enrollment/sessions/%s/behavior" % sid, headers=auth(owner), json={"approve": True}).json()
    assert v2["version"] == 2
    reused = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True}).json()
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
    first = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True})
    assert first.status_code == 502
    second = c.post("/api/enrollment/sessions/%s/clone" % sid, headers=auth(owner), json={"approve": True})
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
