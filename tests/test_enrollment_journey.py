"""Recovery/read-model regressions; synthetic data and no provider network calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from studio.app import now, uid
from studio.enrollment import ProviderError
from studio.runtime import create_app


SCOPES = dict(recording=True, external_processing=True, voice_cloning=True,
              private_preview=True, self_attestation=True)


def auth(user):
    return {"Authorization": "Bearer " + user["token"]}


class CleanupOnlyProvider:
    def __init__(self):
        self.hangups = []
        self.deleted_voices = []
        self.deleted_assistants = []

    async def openai_hangup(self, key, call_id):
        assert key == "test-openai"
        self.hangups.append(call_id)

    async def eleven_delete_voice(self, key, voice_id):
        assert key == "test-eleven"
        self.deleted_voices.append(voice_id)

    async def vapi_delete_assistant(self, key, assistant_id):
        assert key == "test-vapi"
        self.deleted_assistants.append(assistant_id)

    def __getattr__(self, name):
        raise AssertionError("Unexpected provider action: " + name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "VAPI_API_KEY", "VAPI_PUBLIC_API_KEY", "RANEEN_VAPI_TEMPLATE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "1")
    app = create_app(tmp_path)
    owner = app.state.store.create_user("Synthetic owner", "admin")
    other = app.state.store.create_user("Synthetic other owner", "admin")
    contributor = app.state.store.create_user("Synthetic contributor", "contributor")
    fake = CleanupOnlyProvider()
    app.state.enrollment_provider = fake
    with TestClient(app) as client:
        yield client, app.state.store, owner, other, contributor, fake


def start(client, owner):
    response = client.post("/api/enrollment/sessions", headers=auth(owner), json=SCOPES)
    assert response.status_code == 201
    return response.json()["id"]


def add_operation(store, sid, state, kind="voice_clone"):
    store.execute(
        "INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,?,?,?)",
        (uid(), sid, kind, uid(), state, '{"error":"synthetic private diagnostic"}', now(), now()),
    )


def snapshot(store):
    with store.db() as db:
        return tuple(db.iterdump())


def test_recovery_is_owner_only_and_retains_revoked_sessions(env, monkeypatch):
    client, store, owner, other, contributor, fake = env
    revoked = start(client, owner)
    assert client.post(f"/api/enrollment/sessions/{revoked}/revoke", headers=auth(owner), json={"confirm": True}).status_code == 200
    active = start(client, owner)
    other_sid = start(client, other)
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")
    result = client.get("/api/enrollment/sessions", headers=auth(owner))
    assert result.status_code == 200
    assert result.json()["enabled"] is False
    sessions = {item["id"]: item for item in result.json()["sessions"]}
    assert set(sessions) == {active, revoked}
    assert sessions[revoked]["revoked"] is True
    assert sessions[active]["revoked"] is False
    assert other_sid not in result.text
    assert client.get("/api/enrollment/sessions").status_code == 401
    assert client.get("/api/enrollment/sessions", headers=auth(contributor)).status_code == 403
    assert client.get(f"/api/enrollment/sessions/{active}/journey", headers=auth(contributor)).status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("get", "", None), ("get", "/journey", None),
    ("post", "/webrtc-close", None), ("post", "/cleanup", None),
    ("post", "/revoke", {"confirm": True}),
])
def test_feature_off_does_not_bypass_resource_ownership(env, monkeypatch, method, path, body):
    client, store, owner, other, contributor, fake = env
    sid = start(client, owner)
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")
    kwargs = {"headers": auth(other)}
    if body is not None:
        kwargs["json"] = body
    response = getattr(client, method)(f"/api/enrollment/sessions/{sid}{path}", **kwargs)
    assert response.status_code == 403
    assert store.one("SELECT revoked_at FROM enrollment_sessions WHERE id=?", (sid,))["revoked_at"] is None


def test_feature_off_keeps_stop_revoke_and_tracked_cleanup_accessible(env, monkeypatch):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_synthetic", "open", now(), now()))
    store.execute("UPDATE enrollment_sessions SET voice_id=?,assistant_id=? WHERE id=?", ("voice_synthetic", "assistant_synthetic", sid))
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven")
    monkeypatch.setenv("VAPI_API_KEY", "test-vapi")
    assert client.get(f"/api/enrollment/sessions/{sid}", headers=auth(owner)).status_code == 200
    assert client.post(f"/api/enrollment/sessions/{sid}/webrtc-close", headers=auth(owner)).json() == {"state": "closed"}
    assert fake.hangups == ["rtc_synthetic"]
    assert client.post(f"/api/enrollment/sessions/{sid}/revoke", headers=auth(owner), json={"confirm": True}).status_code == 200
    assert client.post(f"/api/enrollment/sessions/{sid}/cleanup", headers=auth(owner)).status_code == 200
    assert fake.deleted_voices == ["voice_synthetic"]
    assert fake.deleted_assistants == ["assistant_synthetic"]
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == "revoked" and state["can_resume"] is False
    assert state["preview_allowed"] is False


def test_feature_off_blocks_new_work_without_provider_creation(env, monkeypatch):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")
    assert client.post("/api/enrollment/sessions", headers=auth(owner), json=SCOPES).status_code == 404
    for action, body in [
        ("clone", {"approve": True}),
        ("assistant", {"approve": True, "behavior_id": "synthetic-behavior"}),
        ("behavior", {"approve": True}),
    ]:
        assert client.post(f"/api/enrollment/sessions/{sid}/{action}", headers=auth(owner), json=body).status_code == 404
    assert client.post(f"/api/enrollment/sessions/{sid}/webrtc", headers=auth(owner), content=b"v=0").status_code == 404
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == "interview" and state["can_resume"] is False
    assert store.all("SELECT * FROM enrollment_operations") == []


def test_get_projection_preserves_saved_counts_without_quality_or_private_data_claims(env):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    # Claimed duration is deliberately unrelated to the legacy clean_ms field.
    store.execute("UPDATE enrollment_sessions SET clean_ms=999999 WHERE id=?", (sid,))
    store.execute("INSERT INTO enrollment_chunks VALUES(?,?,?,?,?,?,?,?,?)", (sid, 4, "synthetic-digest", 1200, 4000, "audio/webm", "contributor", "/synthetic/private/audio", now()))
    for status in ("confirmed", "pending", "rejected"):
        store.execute("INSERT INTO enrollment_evidence VALUES(?,?,?,?,?,?,NULL,?,NULL)",
                      (uid(), sid, "synthetic-private-source", "response_pattern", '{"text":"synthetic private evidence"}', status, now()))
    store.execute("INSERT INTO enrollment_transcripts VALUES(?,?,?,?)", (sid, "synthetic-item", "synthetic private transcript", now()))
    before = snapshot(store)
    response = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner))
    data = response.json()
    assert data["saved_audio_ms"] == 4000
    assert data["chunk_count"] == 1 and data["next_seq"] == 5
    assert data["confirmed_patterns"] == 1 and data["pending_patterns"] == 1
    assert data["stage"] == "interview" and data["can_resume"] is True
    assert data["preview_allowed"] is False
    assert "clean_ms" not in data and "voice_ready" not in data
    assert "synthetic private" not in response.text and "/synthetic/" not in response.text
    listed = client.get("/api/enrollment/sessions", headers=auth(owner))
    assert listed.json()["sessions"][0]["captured_ms"] == 4000
    assert "synthetic private" not in listed.text
    assert snapshot(store) == before


@pytest.mark.parametrize("operation_state", ["dispatching", "outcome_unknown"])
def test_unconfirmed_provider_outcomes_require_review_without_retry(env, operation_state):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    add_operation(store, sid, operation_state)
    before = snapshot(store)
    for _ in range(2):
        response = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner))
        state = response.json()
        assert state["stage"] == "provider_outcome_unknown"
        assert state["suggested_action"] == "contact_support"
        assert state["provider_pending"] is True
        assert state["can_resume"] is False and state["preview_allowed"] is False
        assert "diagnostic" not in response.text
    assert snapshot(store) == before


@pytest.mark.parametrize("voice_state,stage", [("ready", "voice_setup"), ("verification_required", "verification_required"), ("outcome_unknown", "provider_outcome_unknown")])
def test_legacy_voice_state_never_enables_preview(env, voice_state, stage):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("UPDATE enrollment_sessions SET voice_state=?,voice_id=? WHERE id=?", (voice_state, "voice_synthetic", sid))
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == stage
    assert state["preview_allowed"] is False and state["can_resume"] is False
    assert "voice_id" not in state


def test_unconfirmed_interview_stop_is_visible(env):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_synthetic", "close_unknown", now(), now()))
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == "provider_outcome_unknown" and state["can_resume"] is False


def test_known_call_stop_can_be_retried_after_revocation_with_feature_off(env, monkeypatch):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_synthetic", "close_unknown", now(), now()))
    store.execute("UPDATE enrollment_sessions SET state='revoked',revoked_at=? WHERE id=?", (now(), sid))
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("RANEEN_VOICE_ENROLLMENT_ENABLED", "0")

    async def transient_stop(key, call_id):
        assert key == "test-openai"
        fake.hangups.append(call_id)
        if len(fake.hangups) == 1:
            raise ProviderError("Synthetic uncertain hangup", uncertain=True)

    monkeypatch.setattr(fake, "openai_hangup", transient_stop)
    endpoint = f"/api/enrollment/sessions/{sid}/webrtc-close"
    assert client.post(endpoint, headers=auth(owner)).status_code == 502
    assert store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?", (sid,))["state"] == "close_unknown"
    assert client.post(endpoint, headers=auth(owner)).json()["state"] == "closed"
    assert fake.hangups == ["rtc_synthetic", "rtc_synthetic"]
    assert store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?", (sid,))["state"] == "closed"


def test_late_hangup_does_not_mark_a_different_call_closed(env, monkeypatch):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_old", "close_unknown", now(), now()))
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")

    async def late_stop(key, call_id):
        assert call_id == "rtc_old"
        store.execute("UPDATE enrollment_realtime_calls SET call_id='rtc_new',state='open' WHERE session_id=?", (sid,))

    monkeypatch.setattr(fake, "openai_hangup", late_stop)
    assert client.post(f"/api/enrollment/sessions/{sid}/webrtc-close", headers=auth(owner)).status_code == 200
    assert store.one("SELECT call_id,state FROM enrollment_realtime_calls WHERE session_id=?", (sid,)) == {"call_id": "rtc_new", "state": "open"}


def test_revoked_pending_operation_does_not_appear_cleaned_up(env):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    add_operation(store, sid, "dispatching")
    store.execute("UPDATE enrollment_sessions SET state='revoked',revoked_at=? WHERE id=?", (now(), sid))
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == "revoked" and state["provider_pending"] is True
    assert state["cleanup_state"] == "manual_reconciliation"


def test_recovery_requires_explicit_stop_before_replacing_an_open_call(env):
    client, store, owner, _, _, fake = env
    sid = start(client, owner)
    store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_synthetic", "open", now(), now()))
    state = client.get(f"/api/enrollment/sessions/{sid}/journey", headers=auth(owner)).json()
    assert state["stage"] == "interview" and state["can_resume"] is False
    assert state["interview_call_state"] == "open"
    assert state["suggested_action"] == "stop_interview"
    assert fake.hangups == []


def test_recovery_list_limit_is_bounded(env):
    client, store, owner, _, _, fake = env
    for _ in range(3):
        sid = start(client, owner)
        store.execute("UPDATE enrollment_sessions SET state='complete' WHERE id=?", (sid,))
    assert len(client.get("/api/enrollment/sessions?limit=2", headers=auth(owner)).json()["sessions"]) == 2
    assert client.get("/api/enrollment/sessions?limit=0", headers=auth(owner)).status_code == 422
    assert client.get("/api/enrollment/sessions?limit=51", headers=auth(owner)).status_code == 422


def test_concurrent_start_admits_only_one_active_enrollment(env):
    client, store, owner, _, _, fake = env
    barrier = Barrier(8)

    def attempt(_):
        barrier.wait(timeout=10)
        response = client.post("/api/enrollment/sessions", headers=auth(owner), json=SCOPES)
        assert response.status_code == 201
        return response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert len({item["id"] for item in results}) == 1
    assert sum(not item["resumed"] for item in results) == 1
    assert store.one("SELECT COUNT(*) AS n FROM enrollment_sessions WHERE owner_id=?", (owner["id"],))["n"] == 1
    assert store.one("SELECT COUNT(*) AS n FROM audit WHERE event='enrollment_started'")["n"] == 1
