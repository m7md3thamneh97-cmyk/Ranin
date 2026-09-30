"""Independent release regressions; synthetic fixtures and no provider traffic.

Audio codecs are tested separately. These tests isolate authorization and the
moment a paid provider result arrives after consent has already been withdrawn.
"""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from studio.app import now, uid
from studio.enrollment import ProviderError
from studio.runtime import create_app


SCOPES = dict(recording=True, external_processing=True, voice_cloning=True,
              private_preview=True, self_attestation=True)
VOICE = "voice_synthetic_review"
ASSISTANT = "11111111-2222-3333-4444-555555555555"


def auth(user):
    return {"Authorization": "Bearer " + user["token"]}


@pytest.fixture
def release(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "VAPI_API_KEY"):
        monkeypatch.setenv(key, "synthetic-" + key)
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "1")
    monkeypatch.setenv("RANEEN_VAPI_TEMPLATE_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.delenv("VAPI_PUBLIC_API_KEY", raising=False)
    app = create_app(tmp_path)
    owner = app.state.store.create_user("Synthetic owner", "admin")
    other = app.state.store.create_user("Synthetic other owner", "admin")
    contributor = app.state.store.create_user("Synthetic contributor", "contributor")
    with TestClient(app) as client:
        response = client.post("/api/enrollment/sessions", headers=auth(owner), json=SCOPES)
        assert response.status_code == 201
        yield client, app, owner, other, contributor, response.json()["id"]


def seed_voice(app, sid, state="sample_required"):
    app.state.store.execute(
        "UPDATE enrollment_sessions SET voice_id=?,voice_state=? WHERE id=?", (VOICE, state, sid)
    )


def seed_behavior(app, sid, voice_approved=True):
    store = app.state.store
    seed_voice(app, sid, "ready")
    behavior = uid()
    payload = json.dumps({"evidence_origin": "trusted_audio_v1", "evidence": [{"interpretation": "Ask to confirm an ambiguous number."}]})
    store.execute("INSERT INTO enrollment_behavior_versions VALUES(?,?,?,?,?,?)",
                  (behavior, sid, 1, payload, hashlib.sha256(payload.encode()).hexdigest(), now()))
    if voice_approved:
        store.execute("INSERT INTO enrollment_voice_approvals VALUES(?,?,?)", (sid, VOICE, now()))
    return behavior


class RevokingProvider:
    def __init__(self, store, sid, revoke_on):
        self.store, self.sid, self.revoke_on = store, sid, revoke_on
        self.deleted_voices = []
        self.deleted_assistants = []
        self.created_assistants = []

    def withdraw(self):
        self.store.execute("UPDATE enrollment_sessions SET revoked_at=?,state='revoked' WHERE id=?",
                           (now(), self.sid))

    async def eleven_speech(self, *args):
        assert self.revoke_on == "speech"
        self.withdraw()
        return b"synthetic-preview-result"

    async def eleven_clone(self, *args):
        assert self.revoke_on == "clone"
        self.withdraw()
        return {"voice_id": VOICE, "requires_verification": False}

    async def eleven_delete_voice(self, key, voice_id):
        self.deleted_voices.append(voice_id)

    async def vapi_delete_assistant(self, key, assistant_id):
        self.deleted_assistants.append(assistant_id)

    async def vapi_json(self, key, method, path, *, json_body=None):
        if method == "GET":
            if self.revoke_on == "template":
                self.withdraw()
            return {"model": {"provider": "openai", "model": "gpt-4.1-mini"},
                    "tools": [{"type": "transferCall"}], "serverUrl": "https://must-not-inherit.invalid"}
        assert method == "POST" and path == "/assistant"
        self.created_assistants.append(json_body)
        if self.revoke_on == "assistant":
            self.withdraw()
        return {"id": ASSISTANT}


def test_late_synthesized_sample_is_not_persisted_or_returned_after_revocation(release, monkeypatch):
    client, app, owner, _, _, sid = release
    seed_voice(app, sid)
    app.state.enrollment_provider = RevokingProvider(app.state.store, sid, "speech")
    monkeypatch.setattr("studio.enrollment_audio.validate_synthesized_audio", lambda _: {"active_ms": 1000})
    response = client.post(f"/api/enrollment/sessions/{sid}/preview", headers=auth(owner),
                           json={"approve": True, "kind": "question"})
    assert response.status_code == 410
    assert "synthetic-preview-result" not in response.text
    assert not list((app.state.store.root / "enrollments" / sid / "previews").glob("*.mp3"))
    row = app.state.store.one("SELECT state,voice_state FROM enrollment_sessions WHERE id=?", (sid,))
    assert row["state"] == "revoked" and row["voice_state"] != "ready"


def test_late_clone_is_tracked_and_deleted_when_consent_was_withdrawn(release, monkeypatch, tmp_path):
    client, app, owner, _, _, sid = release
    provider = RevokingProvider(app.state.store, sid, "clone")
    app.state.enrollment_provider = provider
    sample = tmp_path / "synthetic.wav"
    sample.write_bytes(b"synthetic-codec-tested-separately")
    monkeypatch.setattr("studio.enrollment_audio.prepare_clone_sample", lambda *args, **kwargs:
                        ([{"path": str(sample), "mime": "audio/wav", "seq": 0}],
                         {"total_ms": 60000, "active_ms": 60000, "chunks": [{"seq": 0}]}))
    response = client.post(f"/api/enrollment/sessions/{sid}/clone", headers=auth(owner), json={"approve": True, "final_seq": 0})
    assert response.status_code == 410
    assert provider.deleted_voices == [VOICE]
    operations = app.state.store.all("SELECT kind,state,provider_id FROM enrollment_operations WHERE session_id=?", (sid,))
    assert any(x["kind"] == "voice_clone" and x["provider_id"] == VOICE for x in operations)
    assert any(x["kind"] == "cleanup_voice" and x["state"] == "succeeded" for x in operations)
    assert client.post(f"/api/enrollment/sessions/{sid}/preview", headers=auth(owner),
                       json={"approve": True, "kind": "number"}).status_code == 410


def test_late_assistant_is_deleted_and_does_not_reactivate_enrollment(release):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid)
    provider = RevokingProvider(app.state.store, sid, "assistant")
    app.state.enrollment_provider = provider
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 410
    assert provider.deleted_assistants == [ASSISTANT]
    assert app.state.store.one("SELECT state FROM enrollment_sessions WHERE id=?", (sid,))["state"] == "revoked"
    config = provider.created_assistants[0]
    assert not config.get("tools") and not config.get("serverUrl")
    assert config["maxDurationSeconds"] == 180
    assert config["voice"]["voiceId"] == VOICE


def test_missing_credential_does_not_reserve_a_permanent_assistant_operation(release, monkeypatch):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid)
    monkeypatch.delenv("VAPI_API_KEY")
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 503
    assert app.state.store.all("SELECT * FROM enrollment_operations WHERE session_id=?", (sid,)) == []


def test_assistant_requires_human_voice_approval(release):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid, voice_approved=False)
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 409
    assert app.state.store.all("SELECT * FROM enrollment_operations WHERE session_id=?", (sid,)) == []


def test_old_preview_config_never_returns_a_reusable_key(release, monkeypatch):
    client, app, owner, other, contributor, sid = release
    monkeypatch.setenv("VAPI_PUBLIC_API_KEY", "synthetic-browser-key-must-not-escape")
    app.state.store.execute("UPDATE enrollment_sessions SET assistant_id=? WHERE id=?", (ASSISTANT, sid))
    path = f"/api/enrollment/sessions/{sid}/preview-config"
    for headers, status in (({}, 401), (auth(other), 403), (auth(contributor), 403), (auth(owner), 410)):
        response = client.get(path, headers=headers)
        assert response.status_code == status
        assert "synthetic-browser-key-must-not-escape" not in response.text


def test_revocation_during_template_read_prevents_assistant_dispatch(release):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid)
    provider = RevokingProvider(app.state.store, sid, "template")
    app.state.enrollment_provider = provider
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 410
    assert provider.created_assistants == []
    assert not app.state.store.one("SELECT id FROM enrollment_operations WHERE session_id=? AND state='dispatching'", (sid,))


def test_unknown_clone_operation_blocks_a_different_sample_manifest(release, monkeypatch, tmp_path):
    client, app, owner, _, _, sid = release
    provider = RevokingProvider(app.state.store, sid, "clone")
    app.state.enrollment_provider = provider
    sample = tmp_path / "different-synthetic.wav"
    sample.write_bytes(b"codec-tested-separately")
    monkeypatch.setattr("studio.enrollment_audio.prepare_clone_sample", lambda *args, **kwargs:
                        ([{"path": str(sample), "mime": "audio/wav", "seq": 0}],
                         {"total_ms": 60000, "active_ms": 60000, "chunks": [{"seq": 99}]}))
    app.state.store.execute("INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,'{}',?,?)",
                            (uid(), sid, "voice_clone", "previous-manifest", "outcome_unknown", now(), now()))
    response = client.post(f"/api/enrollment/sessions/{sid}/clone", headers=auth(owner), json={"approve": True, "final_seq": 0})
    assert response.status_code == 409
    assert provider.deleted_voices == []
    assert app.state.store.one("SELECT revoked_at FROM enrollment_sessions WHERE id=?", (sid,))["revoked_at"] is None
    assert len(app.state.store.all("SELECT * FROM enrollment_operations WHERE session_id=?", (sid,))) == 1


def test_realtime_hangup_failure_does_not_skip_other_revocation_cleanup(release):
    client, app, owner, _, _, sid = release
    seed_voice(app, sid, "ready")
    provider = RevokingProvider(app.state.store, sid, "none")

    async def uncertain_hangup(*args):
        raise ProviderError("Synthetic uncertain hangup", uncertain=True)

    provider.openai_hangup = uncertain_hangup
    app.state.enrollment_provider = provider
    app.state.store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)",
                            (sid, "rtc_synthetic_review", "open", now(), now()))
    response = client.post(f"/api/enrollment/sessions/{sid}/revoke", headers=auth(owner), json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["state"] == "revoked"
    assert provider.deleted_voices == [VOICE]
    assert app.state.store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?", (sid,))["state"] == "close_unknown"


def test_public_evidence_routes_cannot_create_confirmed_behavior(release):
    client, app, owner, _, _, sid = release
    for path, body in (
        ("transcripts", {"item_id": "fake-turn", "transcript": "Yes, save this"}),
        ("tool", {"call_id": "fake-tool", "name": "confirm_evidence", "arguments": {"accepted": True}, "source_item_id": "fake-turn"}),
    ):
        response = client.post(f"/api/enrollment/sessions/{sid}/{path}", headers=auth(owner), json=body)
        assert response.status_code in {403, 410}, response.text
    assert client.post(f"/api/enrollment/sessions/{sid}/behavior", headers=auth(owner), json={"approve": True}).status_code == 409


def test_legacy_confirmed_row_without_audio_provenance_cannot_compile(release):
    client, app, owner, _, _, sid = release
    app.state.store.execute("INSERT INTO enrollment_evidence VALUES(?,?,?,?,?,'confirmed',?,?,?)",
                            (uid(), sid, "untrusted-browser-item", "response_pattern", '{}', "Yes", now(), now()))
    response = client.post(f"/api/enrollment/sessions/{sid}/behavior", headers=auth(owner), json={"approve": True})
    assert response.status_code == 409


def test_confirmation_audio_started_before_readback_cannot_approve_afterward(release):
    from studio.enrollment_evidence import EvidenceService

    _, app, _, _, _, sid = release
    store = app.state.store
    call_id = "rtc_synthetic_review"
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, call_id, "open", now(), now()))
    evidence = EvidenceService(store)
    evidence.mark_audio(sid, call_id, "demonstration")
    evidence.mark_audio(sid, call_id, "demonstration", committed=True)
    evidence.record_transcript(sid, call_id, "demonstration", "When the budget is ambiguous, I ask which number was intended.")
    proposal = evidence.propose(sid, call_id, "tool-synthetic", {
        "kind": "decision_rule", "situation": "Ambiguous budget", "interpretation": "Confirm an ambiguous amount.",
        "change_condition": "When the amount is clear, continue.", "replaces_id": "",
    })
    assert proposal["ok"], proposal
    evidence.mark_audio(sid, call_id, "premature-approval")
    assert evidence.verify_readback(sid, call_id, proposal["challenge_nonce"], proposal["challenge_text"])
    evidence.mark_audio(sid, call_id, "premature-approval", committed=True)
    evidence.record_transcript(sid, call_id, "premature-approval", "Yes, save this")
    assert evidence.confirmed_rows(sid) == []
    evidence.mark_audio(sid, call_id, "new-approval")
    evidence.mark_audio(sid, call_id, "new-approval", committed=True)
    evidence.record_transcript(sid, call_id, "new-approval", "Yes, save this")
    assert len(evidence.confirmed_rows(sid)) == 1


def seed_preview(app, sid):
    behavior = seed_behavior(app, sid)
    app.state.store.execute("UPDATE enrollment_sessions SET assistant_id=?,active_behavior_id=? WHERE id=?",
                            (ASSISTANT, behavior, sid))
    app.state.store.execute("INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)",
                            (uid(), sid, "vapi_assistant", "synthetic-reviewed", "succeeded", ASSISTANT,
                             json.dumps({"behavior_id": behavior}), now(), now()))


class PreviewProvider:
    def __init__(self, store=None, sid=None, error=None):
        self.store, self.sid, self.error = store, sid, error
        self.calls = []
        self.stops = []

    async def vapi_json(self, key, method, path, *, json_body=None):
        assert method == "POST" and path == "/call"
        self.calls.append(json_body)
        if self.error:
            raise self.error
        if self.store is not None:
            self.store.execute("UPDATE enrollment_sessions SET revoked_at=?,state='revoked' WHERE id=?", (now(), self.sid))
        return {"id": "99999999-2222-3333-4444-555555555555", "webCallUrl": "https://synthetic.daily.co/private-room",
                "monitor": {"controlUrl": "https://synthetic.vapi.ai/private-control"}, "transport": {"provider": "daily"}}

    async def vapi_end_call(self, url):
        self.stops.append(url)


def test_preview_call_allowance_and_duration_are_server_owned(release):
    client, app, owner, _, _, sid = release
    seed_preview(app, sid)
    provider = PreviewProvider()
    app.state.enrollment_provider = provider
    path = f"/api/enrollment/sessions/{sid}/preview-call"
    for _ in range(3):
        response = client.post(path, headers=auth(owner), json={"approve": True})
        assert response.status_code == 200, response.text
        assert response.json()["max_duration_seconds"] == 180
        assert "control" not in response.text and "synthetic-VAPI_API_KEY" not in response.text
        assert client.post(path + "/close", headers=auth(owner)).status_code == 200
    assert client.post(path, headers=auth(owner), json={"approve": True}).status_code == 429
    assert len(provider.calls) == 3 and len(provider.stops) == 3
    for payload in provider.calls:
        assert payload["assistantId"] == ASSISTANT
        assert payload["transport"] == {"provider": "daily", "roomDeleteOnUserLeaveEnabled": True}
        assert payload["assistantOverrides"]["maxDurationSeconds"] == 180
        assert not payload["assistantOverrides"]["artifactPlan"]["recordingEnabled"]
        assert "phoneNumberId" not in payload and "customer" not in payload


def test_uncertain_preview_call_creation_blocks_another_paid_attempt(release):
    client, app, owner, _, _, sid = release
    seed_preview(app, sid)
    provider = PreviewProvider(error=ProviderError("Synthetic timeout", uncertain=True))
    app.state.enrollment_provider = provider
    path = f"/api/enrollment/sessions/{sid}/preview-call"
    assert client.post(path, headers=auth(owner), json={"approve": True}).status_code == 502
    assert client.post(path, headers=auth(owner), json={"approve": True}).status_code == 409
    assert len(provider.calls) == 1
    assert app.state.store.one("SELECT state FROM enrollment_preview_calls WHERE session_id=?", (sid,))["state"] == "outcome_unknown"


def test_late_preview_call_is_stopped_without_returning_room_after_revocation(release):
    client, app, owner, _, _, sid = release
    seed_preview(app, sid)
    provider = PreviewProvider(app.state.store, sid)
    app.state.enrollment_provider = provider
    response = client.post(f"/api/enrollment/sessions/{sid}/preview-call", headers=auth(owner), json={"approve": True})
    assert response.status_code == 410
    assert "private-room" not in response.text
    assert provider.stops == ["https://synthetic.vapi.ai/private-control"]


def test_feature_off_stops_existing_preview_without_bypassing_ownership(release, monkeypatch):
    client, app, owner, other, contributor, sid = release
    seed_preview(app, sid)
    provider = PreviewProvider()
    app.state.enrollment_provider = provider
    path = f"/api/enrollment/sessions/{sid}/preview-call"
    assert client.post(path, headers=auth(owner), json={"approve": True}).status_code == 200
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")
    assert client.post(path, headers=auth(owner), json={"approve": True}).status_code == 404
    for headers, code in (({}, 401), (auth(other), 403), (auth(contributor), 403)):
        assert client.post(path + "/close", headers=headers).status_code == code
    assert provider.stops == []
    assert client.post(path + "/close", headers=auth(owner)).json()["state"] == "closed"
    assert len(provider.stops) == 1


def test_legacy_compiled_behavior_cannot_create_a_new_assistant(release):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid)
    app.state.store.execute("UPDATE enrollment_behavior_versions SET payload='{}' WHERE id=?", (behavior,))
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 409
    assert app.state.store.all("SELECT * FROM enrollment_operations WHERE session_id=?", (sid,)) == []


def test_legacy_prepared_assistant_is_not_eligible_for_a_preview_call(release):
    client, app, owner, _, _, sid = release
    seed_preview(app, sid)
    app.state.store.execute("UPDATE enrollment_behavior_versions SET payload='{}' WHERE session_id=?", (sid,))
    provider = PreviewProvider()
    app.state.enrollment_provider = provider
    response = client.post(f"/api/enrollment/sessions/{sid}/preview-call", headers=auth(owner), json={"approve": True})
    assert response.status_code == 409
    assert provider.calls == []


def test_new_owner_assistant_uses_documented_defaults_without_a_template(release, monkeypatch):
    client, app, owner, _, _, sid = release
    behavior = seed_behavior(app, sid)
    monkeypatch.delenv("RANEEN_VAPI_TEMPLATE_ID")
    provider = RevokingProvider(app.state.store, sid, "none")
    app.state.enrollment_provider = provider
    response = client.post(f"/api/enrollment/sessions/{sid}/assistant", headers=auth(owner),
                           json={"approve": True, "behavior_id": behavior})
    assert response.status_code == 200, response.text
    assert len(provider.created_assistants) == 1
    config = provider.created_assistants[0]
    assert config["model"]["provider"] == "openai"
    assert config["model"]["model"] == "gpt-4.1-mini"
    assert config["transcriber"] == {"provider": "openai", "model": "gpt-4o-transcribe", "language": "ar"}
    assert config["voice"]["voiceId"] == VOICE
    assert not config.get("tools") and not config.get("serverUrl")
