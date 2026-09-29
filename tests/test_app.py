import array
import io
import json
import math
import wave
import zipfile
import pytest
from fastapi.testclient import TestClient
from studio.app import create_app, analyze_wav, CONSENT_VERSION


def wav(silent=False, clipped=False, channels=1, seconds=1.2):
    output = io.BytesIO()
    with wave.open(output, 'wb') as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(16000)
        values = array.array('h', [0 if silent else 32767 if clipped else int(5000 * math.sin(i / 10)) for i in range(int(16000 * seconds) * channels)])
        w.writeframes(values.tobytes())
    return output.getvalue()


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    admin = store.create_user('Owner', 'admin')
    contributor = store.create_user('Contributor')
    other = store.create_user('Other')
    return TestClient(app), store, admin, contributor, other


def auth(user):
    return {'Authorization': 'Bearer ' + user['token']}


def profile(client, user, voice=True, behavior=True, consent=True):
    response = client.post('/api/profiles', headers=auth(user), json={'name':'Speaker','dialect':'Emirati Arabic','contribution':'both','style_notes':'Natural questions.'})
    assert response.status_code == 201, response.text
    ident = response.json()['id']
    if consent:
        consent_for(client,user,ident,voice,behavior)
    return ident


def consent_for(client,user,ident,voice=True,behavior=True):
    response = client.post(f'/api/profiles/{ident}/consent', headers=auth(user), json={'collection':True,'behavior_export':behavior,'voice_export':voice,'version':CONSENT_VERSION,'self_attestation':True})
    assert response.status_code == 201, response.text
    return response.json()['id']


def example(client,user,pid,scenario='purpose-01',audio=None,verified=True):
    return client.post('/api/examples', headers=auth(user), json=dict(profile_id=pid,scenario_id=scenario,session_id='session-one',response_text='شو الأهم عندك، السكن ولا الاستثمار؟',transcript_verified=verified,action='clarify',decision_cue='The primary objective is unclear.',alternative='Do not invent property recommendations.',change_condition='A near-term move would prioritize housing.',delivery='question',pronunciation_notes='',audio_id=audio))


def approve(client,user,example_id):
    return client.post(f'/api/examples/{example_id}/review',headers=auth(user),json={'status':'approved','notes':'Test review'})


def export_files(client,admin,pid,kind='behavior'):
    response=client.get(f'/api/export/{pid}?kind={kind}',headers=auth(admin))
    assert response.status_code==200,response.text
    z=zipfile.ZipFile(io.BytesIO(response.content))
    return {name:z.read(name) for name in z.namelist()}


def test_auth_required(env):
    c,*_=env
    assert c.get('/api/profiles').status_code==401
    assert c.get('/api/me',headers={'Authorization':'Bearer wrong'}).status_code==401


def test_static_does_not_expose_data(env):
    c,*_=env
    assert c.get('/').status_code==200
    assert c.get('/static/../data/studio.sqlite3').status_code==404
    assert 'frame-ancestors' in c.get('/').headers['content-security-policy']


def test_profile_owner_isolation(env):
    c,s,a,u,o=env;pid=profile(c,u)
    assert c.get('/api/library',params={'profile_id':pid},headers=auth(o)).status_code==403
    assert c.get('/api/profiles',headers=auth(o)).json()['items']==[]


def test_admin_cannot_consent_for_employee(env):
    c,s,a,u,o=env;pid=profile(c,u,consent=False)
    r=c.post(f'/api/profiles/{pid}/consent',headers=auth(a),json=dict(collection=True,behavior_export=True,voice_export=True,version=CONSENT_VERSION,self_attestation=True))
    assert r.status_code==403


def test_collection_requires_consent(env):
    c,s,a,u,o=env;pid=profile(c,u,consent=False)
    assert example(c,u,pid).status_code==409
    assert c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).status_code==409


def test_voice_upload_and_signal_stats(env):
    c,s,a,u,o=env;pid=profile(c,u)
    r=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav())
    assert r.status_code==201,r.text
    assert r.json()['stats']['sample_rate']==16000
    assert r.json()['stats']['flags']==[]
    assert c.get('/api/audio/'+r.json()['id'],headers=auth(o)).status_code==403


def test_invalid_audio_rejected(env):
    c,s,a,u,o=env;pid=profile(c,u)
    assert c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=b'not-a-wav').status_code==422
    assert c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/mp3'},content=b'data').status_code==415


def test_unverified_cannot_be_approved(env):
    c,s,a,u,o=env;pid=profile(c,u);r=example(c,u,pid,verified=False)
    assert r.status_code==201
    assert approve(c,a,r.json()['id']).status_code==409


def test_contributor_cannot_approve(env):
    c,s,a,u,o=env;pid=profile(c,u);r=example(c,u,pid)
    assert approve(c,u,r.json()['id']).status_code==403


def test_pending_is_not_exported(env):
    c,s,a,u,o=env;pid=profile(c,u);example(c,u,pid)
    files=export_files(c,a,pid)
    assert files['behavior/train.jsonl']==b''
    assert json.loads(files['manifest.json'])['trained_model'] is False


def test_holdout_excluded_from_training_profile(env):
    c,s,a,u,o=env;pid=profile(c,u)
    for scenario in ('purpose-01','holdout-01'):
        r=example(c,u,pid,scenario);assert approve(c,a,r.json()['id']).status_code==200
    files=export_files(c,a,pid)
    train=[json.loads(line) for line in files['behavior/train.jsonl'].splitlines()]
    hold=[json.loads(line) for line in files['behavior/holdout.jsonl'].splitlines()]
    draft=json.loads(files['behavior/profile-draft.json'])
    assert len(train)==len(hold)==1
    assert train[0]['family']=='mixed-purpose'
    assert hold[0]['family']=='conflicting-requirements'
    assert all(x['family']!='conflicting-requirements' for x in draft['examples'])


def test_dialect_text_preserved_verbatim(env):
    c,s,a,u,o=env;pid=profile(c,u);r=example(c,u,pid);approve(c,a,r.json()['id'])
    files=export_files(c,a,pid)
    assert json.loads(files['behavior/train.jsonl'])['response']=='شو الأهم عندك، السكن ولا الاستثمار؟'


def test_voice_scope_not_inferred_from_behavior(env):
    c,s,a,u,o=env;pid=profile(c,u,voice=False)
    assert c.get(f'/api/export/{pid}?kind=voice',headers=auth(a)).status_code==403


def test_new_scope_not_retroactive(env):
    c,s,a,u,o=env;pid=profile(c,u,voice=False)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    r=example(c,u,pid,audio=audio);approve(c,a,r.json()['id'])
    consent_for(c,u,pid,voice=True)
    files=export_files(c,a,pid,'voice')
    assert not any(name.endswith('.wav') for name in files)


def test_withdraw_blocks_exports_and_collection(env):
    c,s,a,u,o=env;pid=profile(c,u);r=example(c,u,pid);approve(c,a,r.json()['id'])
    assert c.post(f'/api/profiles/{pid}/withdraw',headers=auth(u),json={}).status_code==200
    assert c.get(f'/api/export/{pid}',headers=auth(a)).status_code==409
    assert example(c,u,pid).status_code==409
    assert c.get('/api/library',params={'profile_id':pid},headers=auth(a)).status_code==409


def test_reconsent_does_not_restore_withdrawn_data(env):
    c,s,a,u,o=env;pid=profile(c,u);r=example(c,u,pid);approve(c,a,r.json()['id'])
    c.post(f'/api/profiles/{pid}/withdraw',headers=auth(u),json={})
    consent_for(c,u,pid)
    assert export_files(c,a,pid)['behavior/train.jsonl']==b''


def test_voice_export_contains_only_approved_own_audio(env):
    c,s,a,u,o=env;pid=profile(c,u)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    r=example(c,u,pid,audio=audio);approve(c,a,r.json()['id'])
    files=export_files(c,a,pid,'all')
    assert len([name for name in files if name.endswith('.wav')])==1
    assert f'voice/train/{audio}.wav' in files
    assert files[f'voice/train/{audio}.wav']==wav()


def test_low_quality_audio_not_in_voice_export(env):
    c,s,a,u,o=env;pid=profile(c,u)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav(silent=True)).json()['id']
    r=example(c,u,pid,audio=audio);approve(c,a,r.json()['id'])
    files=export_files(c,a,pid,'voice')
    assert not any(name.endswith('.wav') for name in files)


def test_audio_cannot_cross_profiles(env):
    c,s,a,u,o=env;pid=profile(c,u);otherpid=profile(c,o)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    assert example(c,o,otherpid,audio=audio).status_code==422


def test_same_audio_cannot_leak_between_splits(env):
    c,s,a,u,o=env;pid=profile(c,u)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    assert example(c,u,pid,audio=audio).status_code==201
    assert example(c,u,pid,scenario='holdout-01',audio=audio).status_code==409
    duplicate=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    assert example(c,u,pid,scenario='holdout-01',audio=duplicate).status_code==409


def test_delete_removes_local_audio_and_records(env):
    c,s,a,u,o=env;pid=profile(c,u)
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**auth(u),'Content-Type':'audio/wav'},content=wav()).json()['id']
    example(c,u,pid,audio=audio)
    assert (s.audio_dir/(audio+'.wav')).exists()
    assert c.delete(f'/api/profiles/{pid}',headers=auth(u)).status_code==200
    assert not (s.audio_dir/(audio+'.wav')).exists()
    assert s.one('SELECT id FROM profiles WHERE id=?',(pid,)) is None
    assert c.get('/api/audio/'+audio,headers=auth(u)).status_code==404


def test_preference_review_and_export(env):
    c,s,a,u,o=env;pid=profile(c,u)
    r=c.post('/api/preferences',headers=auth(u),json=dict(profile_id=pid,scenario_id='purpose-01',candidate_a='What is your budget?',candidate_b='Is living there or investing more important?',preferred='b',reason='Clarifies the stated ambiguity first.',replacement='',source='Hand-authored test'))
    assert r.status_code==201,r.text
    rid=r.json()['id']
    assert c.post(f'/api/preferences/{rid}/review',headers=auth(a),json={'status':'approved'}).status_code==200
    files=export_files(c,a,pid)
    assert json.loads(files['preferences/train.jsonl'])['preferred']=='b'


def test_cross_origin_blocked(env):
    c,s,a,u,o=env
    assert c.get('/api/me',headers={**auth(u),'Origin':'https://evil.example'}).status_code==403
    assert c.get('/api/me',headers={**auth(u),'Origin':'http://testserver'}).status_code==200


def test_unknown_host_blocked(env):
    c,*_=env
    assert c.get('/',headers={'Host':'evil.example'}).status_code==400


def test_tokens_hashed_at_rest(env):
    c,s,a,u,o=env
    user=s.one('SELECT * FROM users WHERE id=?',(u['id'],))
    assert user['token_hash'] != u['token']
    assert 'token' not in c.get('/api/me',headers=auth(u)).json()


def test_scenario_families_have_single_split(env):
    from studio.scenarios import SCENARIOS
    families={}
    for scenario in SCENARIOS:
        families.setdefault(scenario['family'],set()).add(scenario['split'])
    assert all(len(splits)==1 for splits in families.values())


def test_signal_checks_not_language_scores():
    assert analyze_wav(wav())['flags']==[]
    assert 'very_quiet_or_silence' in analyze_wav(wav(silent=True))['flags']
    assert 'possible_clipping' in analyze_wav(wav(clipped=True))['flags']
    assert 'very_short' in analyze_wav(wav(seconds=.2))['flags']
    with pytest.raises(ValueError):analyze_wav(wav(channels=2))
    with pytest.raises(ValueError):analyze_wav(wav()[:-5])
