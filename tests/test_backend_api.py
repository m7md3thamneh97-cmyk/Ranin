"""Full HTTP trainer workflows, preserving real evidence and offline provider honesty."""
import copy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from studio.app import CONSENT_VERSION, create_app
from studio.providers import LocalLearningProvider, ProviderError
from test_app import wav


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER','local')
    for key in ('OPENAI_API_KEY','ELEVENLABS_API_KEY','VAPI_PUBLIC_KEY','VAPI_PRIVATE_KEY','VAPI_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    app=create_app(tmp_path)
    user=app.state.store.create_user('Trainer')
    other=app.state.store.create_user('Other')
    c=TestClient(app)
    h={'Authorization':'Bearer '+user['token']}
    def post(path,body=None,expected=200):
        response=c.post(path,headers=h,json=body if body is not None else {})
        assert response.status_code==expected,response.text
        return response.json()
    pid=post('/api/profiles',{'name':'Agent','dialect':'Emirati Arabic'},201)['id']
    post(f'/api/profiles/{pid}/consent',{'collection':True,'behavior_export':True,'voice_export':True,'version':CONSENT_VERSION,'self_attestation':True},201)
    return app,c,user,other,h,pid,post


def test_complete_spoken_teaching_correction_retry(api):
    app,c,user,other,h,pid,post=api
    sid=post('/api/sessions',{'profile_id':pid},201)['id']
    evidence=[]
    for transcript in (
        'When someone says the property is expensive, I first ask what price they are comparing it with.',
        'If a client objects to the price, I ask which competing project they compare it with first.',
    ):
        evidence.append(post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':transcript},201)['id'])
    state=c.get(f'/api/profiles/{pid}/learning-state',headers=h).json()
    hypothesis=next(x for x in state['hypotheses'] if x['key']=='price_objection.first_move')
    assert hypothesis['evidence_count']==2
    assert hypothesis['state']=='tentative'
    confirmation=post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':'Yes, this is how I handle price objections.','analyze':False},201)
    post(f"/api/hypotheses/{hypothesis['id']}/confirm",{'source_turn_id':confirmation['id']})
    original=post(f'/api/sessions/{sid}/simulation',{'scenario_id':'purpose-01','caller_text':'The property is expensive.'},201)
    before=copy.deepcopy(c.get(f'/api/profiles/{pid}/versions',headers=h).json())
    source=post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':'No. Ask their budget first before anything else.'},201)
    corrected=post(f"/api/turns/{original['turn_id']}/correct",{'source_turn_id':source['id']})
    assert corrected['normalized_corrections'][0]['key']=='price_objection.first_move'
    retry=post(f"/api/simulations/{original['id']}/retry",{},201)
    assert retry['response_text']!=original['response_text']
    assert 'budget' in retry['response_text'].lower()
    assert retry['profile_version_id']!=original['profile_version_id']
    assert retry['parent_run_id']==original['id']
    stored=c.get(f"/api/simulations/{original['id']}",headers=h).json()
    assert stored['response_text']==original['response_text']
    after=c.get(f'/api/profiles/{pid}/versions',headers=h).json()['items']
    assert all(any(x==old for x in after) for old in before['items'])
    assert all(t in app.state.learning.compile_context(pid)['provenance']['source_turn_ids'] for t in [source['id']])
    with pytest.raises(sqlite3.IntegrityError):
        app.state.store.execute('UPDATE conversation_turns SET transcript=? WHERE id=?',('rewritten',evidence[0]))
    post(f'/api/sessions/{sid}/resume-teaching')
    assert c.get(f'/api/sessions/{sid}',headers=h).json()['mode']=='teaching'


def test_session_turn_order_idempotency_and_end(api):
    app,c,user,other,h,pid,post=api
    session={'profile_id':pid,'idempotency_key':'first-session'}
    sid=post('/api/sessions',session,201)['id']
    assert post('/api/sessions',session,201)['id']==sid
    assert c.post('/api/sessions',headers=h,json={**session,'mode':'simulation'}).status_code==409
    body={'role':'trainer','transcript':'A saved turn.','turn_index':0,'external_id':'source-one','analyze':False}
    first=post(f'/api/sessions/{sid}/turns',body,201)
    assert post(f'/api/sessions/{sid}/turns',body,201)['id']==first['id']
    assert c.post(f'/api/sessions/{sid}/turns',headers=h,json={**body,'transcript':'different'}).status_code==409
    assert c.post(f'/api/sessions/{sid}/turns',headers=h,json={'role':'trainer','transcript':'out of order','turn_index':4}).status_code==409
    post(f'/api/sessions/{sid}/end')
    post(f'/api/sessions/{sid}/end')
    assert post(f'/api/sessions/{sid}/turns',body,201)['id']==first['id']
    assert c.post(f'/api/sessions/{sid}/turns',headers=h,json={'role':'trainer','transcript':'after end'}).status_code==409
    assert c.get(f'/api/sessions/{sid}/turns',headers={'Authorization':'Bearer '+other['token']}).status_code==403


class FailingLearning(LocalLearningProvider):
    def analyze(self,transcript,context):
        raise ProviderError(503,'provider_timeout','Try again later.')


def test_failed_background_learning_keeps_evidence_and_retries(api):
    app,c,user,other,h,pid,post=api
    sid=post('/api/sessions',{'profile_id':pid},201)['id']
    app.state.learning.provider.provider=FailingLearning()
    turn=post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':'I first ask about the purpose, living or investing.'},201)
    jobs=c.get(f'/api/sessions/{sid}/learning-jobs',headers=h).json()['items']
    assert jobs[0]['status']=='failed'
    assert app.state.store.one('SELECT id FROM conversation_turns WHERE id=?',(turn['id'],))
    assert app.state.store.all('SELECT * FROM observations')==[]
    app.state.learning.provider.provider=LocalLearningProvider()
    post(f"/api/learning-jobs/{jobs[0]['id']}/retry")
    jobs=c.get(f'/api/sessions/{sid}/learning-jobs',headers=h).json()['items']
    assert jobs[0]['status']=='completed' and jobs[0]['attempts']==2
    assert app.state.store.all('SELECT * FROM observations')


class FakeVoice:
    def __init__(self):
        self.clones=[]; self.speeches=[];self.deleted=[]
    def clone(self,name,samples):
        self.clones.append(samples)
        return 'voice-test'
    def synthesize(self,voice_id,text):
        self.speeches.append((voice_id,text))
        return b'ID3test-audio'
    def delete(self,voice_id):
        self.deleted.append(voice_id)


def test_approved_voice_used_in_simulation_and_withdrawal_blocks_it(api):
    app,c,user,other,h,pid,post=api
    fake=FakeVoice();app.state.voice.provider=fake
    post(f'/api/profiles/{pid}/provider-authorizations',{'provider':'elevenlabs','scope':'voice_clone','self_attestation':True},201)
    sid=post('/api/sessions',{'profile_id':pid},201)['id']
    upload=c.post(f'/api/profiles/{pid}/audio',headers={**h,'Content-Type':'audio/wav'},content=wav(seconds=61))
    assert upload.status_code==201
    turn=post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':'A clean trainer recording.','audio_id':upload.json()['id']},201)
    assert turn['voice_sample']['eligibility']=='pending'
    post(f"/api/voice/samples/{turn['voice_sample']['id']}/review",{'trainer_only':True,'clean_speech':True})
    candidate=post(f'/api/profiles/{pid}/voice/candidate',{'idempotency_key':'first-voice'},201)
    assert candidate['status']=='ready'
    assert c.get(f"/api/voice/{candidate['id']}/preview",headers=h).content.startswith(b'ID3')
    post(f"/api/voice/{candidate['id']}/approve")
    run=post(f'/api/sessions/{sid}/simulation',{},201)
    assert run['voice_version_id']==candidate['id']
    response=c.get(f"/api/simulations/{run['id']}/audio",headers=h)
    assert response.status_code==200 and response.content.startswith(b'ID3')
    assert fake.speeches[-1]==('voice-test',run['response_text'])
    post(f'/api/profiles/{pid}/withdraw')
    assert c.get(f"/api/simulations/{run['id']}/audio",headers=h).status_code==409
    assert c.get(f"/api/voice/{candidate['id']}/preview",headers=h).status_code==409


def test_late_audio_attachment_preserves_turn_and_partition(api):
    app,c,user,other,h,pid,post=api
    sid=post('/api/sessions',{'profile_id':pid},201)['id']
    tid=post(f'/api/sessions/{sid}/turns',{'role':'trainer','transcript':'My original words.'},201)['id']
    audio=c.post(f'/api/profiles/{pid}/audio',headers={**h,'Content-Type':'audio/wav'},content=wav()).json()['id']
    attached=post(f'/api/turns/{tid}/audio',{'audio_id':audio},201)
    assert attached['sample']['eligibility']=='pending'
    assert app.state.store.one('SELECT audio_id FROM conversation_turns WHERE id=?',(tid,))['audio_id'] is None
    hold=post('/api/sessions',{'profile_id':pid,'split':'holdout','scenario_id':'holdout-01'},201)['id']
    ht=post(f'/api/sessions/{hold}/turns',{'role':'trainer','transcript':'Held out.'},201)['id']
    assert c.post(f'/api/turns/{ht}/audio',headers=h,json={'audio_id':audio}).status_code==409


def test_holdout_and_provider_readiness_are_explicit(api):
    app,c,user,other,h,pid,post=api
    hold=post('/api/sessions',{'profile_id':pid,'split':'holdout','scenario_id':'holdout-01'},201)['id']
    t=post(f'/api/sessions/{hold}/turns',{'role':'trainer','transcript':'A holdout answer.'},201)
    assert 'learning_job' not in t
    assert c.post(f"/api/turns/{t['id']}/analyze",headers=h,json={}).status_code==409
    status=c.get('/api/backend/status',headers=h).json()
    assert status['providers']['learning']['provider']=='local_rules'
    assert status['providers']['voice']['configured'] is False
    assert all(p['live_verified'] is False for p in status['providers'].values())
    assert status['production_calls_enabled'] is False
    schema=c.get('/api/backend/schema',headers=h).json()
    assert '/api/turns/{tid}/correct' in schema['paths']


def test_canonical_vapi_history_without_transcript_timestamp_and_reconnect(api,monkeypatch):
    app,c,user,other,h,pid,post=api
    monkeypatch.setenv('RANEEN_VAPI_WEBHOOK_SECRET','callback-test-secret-'+'x'*40)
    ph={'Authorization':'Bearer '+'callback-test-secret-'+'x'*40}
    post(f'/api/profiles/{pid}/provider-authorizations',{'provider':'vapi','scope':'realtime_voice','self_attestation':True},201)
    sid=post('/api/sessions',{'profile_id':pid,'realtime_provider':'vapi'},201)['id']
    call={'id':'call-one','metadata':{'raneen_session_id':sid}}
    identity_free={'message':{'type':'transcript','role':'user','transcriptType':'final','transcript':'I ask about the purpose first.','call':call}}
    assert c.post('/api/providers/vapi/webhook',headers=ph,json=identity_free).json()['status']=='pending_timed_history'
    history={'message':{'type':'conversation-update','call':call,'messages':[
        {'role':'user','message':'I first ask about purpose, living or investing.','time':100,'endTime':102,'secondsFromStart':0},
        {'role':'bot','message':'What changes when they need to move soon?','time':103,'endTime':104,'secondsFromStart':3},
    ],'artifact':{'recordingUrl':'https://example.test/first'}}}
    first=c.post('/api/providers/vapi/webhook',headers=ph,json=history)
    assert first.status_code==200,first.text
    history['message']['artifact']['recordingUrl']='https://example.test/rotated'
    repeated=c.post('/api/providers/vapi/webhook',headers=ph,json=history)
    assert repeated.status_code==200,repeated.text
    assert repeated.json()['accepted_turn_ids']==first.json()['accepted_turn_ids']
    assert len(c.get(f'/api/sessions/{sid}/turns',headers=h).json()['items'])==2
    assert len(app.state.store.all('SELECT * FROM learning_analyses'))==1
    ended=c.post('/api/providers/vapi/webhook',headers=ph,json={'message':{'type':'status-update','status':'ended','call':call}})
    assert ended.status_code==200
    assert c.get(f'/api/sessions/{sid}',headers=h).json()['status']=='active'
    post(f'/api/sessions/{sid}/reconnect')
    history['message']['call']['id']='call-two'
    history['message']['messages']=[{'role':'user','message':'I keep my Arabic dialect when the caller uses an English place name.','time':200,'endTime':202,'secondsFromStart':0}]
    resumed=c.post('/api/providers/vapi/webhook',headers=ph,json=history)
    assert resumed.status_code==200,resumed.text
    assert len(c.get(f'/api/sessions/{sid}/turns',headers=h).json()['items'])==3
    assert c.get(f'/api/sessions/{sid}',headers=h).json()['provider_call_id']=='call-two'


def test_transport_provisions_server_credentials_without_returning_them(tmp_path,monkeypatch):
    monkeypatch.setenv('RANEEN_LEARNING_STUDIO_ENABLED','1')
    from studio.providers import VapiProvider
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER','local')
    values={'VAPI_PRIVATE_KEY':'private-test-key','VAPI_PUBLIC_KEY':'public-test-key','RANEEN_VAPI_PUBLIC_KEY_RESTRICTED':'true','RANEEN_VAPI_VOICE_ID':'base-voice','RANEEN_PUBLIC_ORIGIN':'https://studio.test','RANEEN_VAPI_WEBHOOK_SECRET':'private-callback-secret-'+'x'*40}
    for key,value in values.items():
        monkeypatch.setenv(key,value)
    sent=[]
    monkeypatch.setattr(VapiProvider,'create_assistant',lambda self,config:sent.append(copy.deepcopy(config)) or 'assistant-test')
    app=create_app(tmp_path,public_origin='https://studio.test')
    owner=app.state.store.create_user('Trainer')
    h={'Authorization':'Bearer '+owner['token']}
    c=TestClient(app,base_url='https://studio.test')
    pid=c.post('/api/profiles',headers=h,json={'name':'Agent','dialect':'Emirati Arabic'}).json()['id']
    assert c.post(f'/api/profiles/{pid}/consent',headers=h,json={'collection':True,'version':CONSENT_VERSION,'self_attestation':True}).status_code==201
    assert c.post(f'/api/profiles/{pid}/provider-authorizations',headers=h,json={'provider':'vapi','scope':'realtime_voice','self_attestation':True}).status_code==201
    sid=c.post('/api/sessions',headers=h,json={'profile_id':pid}).json()['id']
    response=c.post(f'/api/sessions/{sid}/transport',headers=h,json={})
    assert response.status_code==200,response.text
    assert response.json()['assistant_id']=='assistant-test'
    assert values['VAPI_PRIVATE_KEY'] not in response.text
    assert values['RANEEN_VAPI_WEBHOOK_SECRET'] not in response.text
    assert sent[0]['model']['url']==f'https://studio.test/api/providers/vapi/sessions/{sid}'
    assert sent[0]['credentials'][0]['apiKey']==values['RANEEN_VAPI_WEBHOOK_SECRET']
    assert 'conversation-update' in sent[0]['serverMessages']
    assert 'transcript' not in sent[0]['serverMessages']
