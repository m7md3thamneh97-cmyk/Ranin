"""Owner-flow admission and real-codec regressions; providers stay synthetic."""
import json
from studio.app import now, uid
from test_voice_enrollment import env, enable, start, auth, upload, synthetic_audio


def test_interview_budget_is_not_reset_by_a_new_connection(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)',(uid(),sid,'realtime_call','older','succeeded','rtc_old',json.dumps({'consumed_seconds':1795}),now(),now()))
    response=c.post(f'/api/enrollment/sessions/{sid}/webrtc',headers=auth(owner)|{'Content-Type':'application/sdp'},content='v=0\r\ns=test\r\n')
    assert response.status_code==429 and fake.secret_calls==0


def test_unknown_interview_blocks_another_paid_connection(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)',(sid,'reservation_unknown','outcome_unknown',now(),now()))
    response=c.post(f'/api/enrollment/sessions/{sid}/webrtc',headers=auth(owner)|{'Content-Type':'application/sdp'},content='v=0\r\ns=test\r\n')
    assert response.status_code==409 and fake.secret_calls==0


def test_clone_requires_the_final_acknowledged_sequence(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    for seq in range(4): assert upload(c,owner,sid,seq).status_code==200
    response=c.post(f'/api/enrollment/sessions/{sid}/clone',headers=auth(owner),json={'approve':True,'final_seq':4})
    assert response.status_code==409 and fake.clone_calls==0
    assert not app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=?',(sid,))


def test_clone_missing_key_does_not_reserve_or_decode(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    monkeypatch.delenv('ELEVENLABS_API_KEY')
    response=c.post(f'/api/enrollment/sessions/{sid}/clone',headers=auth(owner),json={'approve':True,'final_seq':0})
    assert response.status_code==503 and fake.clone_calls==0
    assert not app.state.store.all('SELECT * FROM enrollment_operations WHERE session_id=?',(sid,))


def test_provider_id_and_invalid_mp3_never_establish_voice_ready(env,monkeypatch):
    c,app,owner,_,fake=env
    enable(monkeypatch);sid=start(c,owner)
    app.state.store.execute("UPDATE enrollment_sessions SET voice_id='voice_test_123456',voice_state='sample_required' WHERE id=?",(sid,))
    async def invalid(*args): return b'ID3'+b'x'*1500
    fake.eleven_speech=invalid
    response=c.post(f'/api/enrollment/sessions/{sid}/preview',headers=auth(owner),json={'approve':True,'kind':'question'})
    assert response.status_code==502
    state=c.get(f'/api/enrollment/sessions/{sid}/workflow',headers=auth(owner)).json()
    assert state['voice_state']=='sample_required' and not state['voice_approved'] and not state['preview_allowed']
