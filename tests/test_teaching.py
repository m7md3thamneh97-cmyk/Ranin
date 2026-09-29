"""Guided teaching tests. All vendor traffic is mocked; no real voices or calls."""
import json
import secrets
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from studio.runtime import create_app
from studio.teaching import reserve_calls, prompt_for, snapshot, Provider
from test_app import profile, auth, example, approve, wav, consent_for

class FakeProvider:
    def __init__(self):
        self.calls=[]; self.fail=False
    async def request(self, provider, method, path, key, **kw):
        self.calls.append((provider,method,path,key,kw))
        if self.fail:
            raise HTTPException(502,'Provider unavailable; no automatic retry.')
        if path=='/audio/transcriptions':return {'text':'لا، مليون ونص، مب مليونين.'}
        if path=='/chat/completions':return {'choices':[{'message':{'content':'شو الأهم عندك؟'}}]}
        if method=='GET':return {'voice':{'provider':'11labs','voiceId':'test-voice','apiKey':'NEVER_COPY'},'transcriber':{'provider':'speechmatics','language':'ar_en'},'model':{'tools':[{'type':'transferCall'}]},'serverUrl':'https://never-copy.example'}
        return {'id':'11111111-2222-3333-4444-555555555555'}

@pytest.fixture
def env(tmp_path,monkeypatch):
    for k in ['OPENAI_API_KEY','VAPI_API_KEY','RANEEN_VAPI_TEMPLATE_ID','RANEEN_AI_DAILY_CALL_LIMIT']:
        monkeypatch.delenv(k,raising=False)
    app=create_app(tmp_path); s=app.state.store; a=s.create_user('Owner','admin');u=s.create_user('Other','contributor')
    fake=FakeProvider();app.state.teaching_provider=fake
    return TestClient(app),s,a,u,fake

def pack(c,a,p):
    e=example(c,a,p).json()['id'];assert approve(c,a,e).status_code==200
    r=c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p,'approve':True});assert r.status_code==201,r.text
    return r.json()['id'],e

def keys(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','private-test-openai-not-real')
    monkeypatch.setenv('VAPI_API_KEY','private-test-vapi-not-real')
    monkeypatch.setenv('RANEEN_VAPI_TEMPLATE_ID','aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')

def test_home_and_legacy_available(env):
    c,s,a,u,f=env
    assert 'guided.js' in c.get('/').text
    assert '/static/app.js' in c.get('/advanced').text
    assert c.get('/api/teaching/status').status_code==401
    assert c.get('/api/teaching/status',headers=auth(a)).json()['weight_training'] is False

def test_no_keys_exposed_or_calls_on_status(env,monkeypatch):
    c,s,a,u,f=env;keys(monkeypatch)
    r=c.get('/api/teaching/status',headers=auth(a))
    assert r.json()['transcription_configured'] is True
    assert 'private-test' not in r.text and not f.calls

def test_pack_needs_explicit_approval(env):
    c,s,a,u,f=env;p=profile(c,a)
    assert c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p}).status_code==403

def test_empty_pack_refused(env):
    c,s,a,u,f=env;p=profile(c,a)
    assert c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p,'approve':True}).status_code==409

def test_no_behavior_permission_refused(env):
    c,s,a,u,f=env;p=profile(c,a,behavior=False);e=example(c,a,p).json()['id'];approve(c,a,e)
    assert c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p,'approve':True}).status_code==409

def test_pack_is_reviewed_exact_and_deduplicated(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    r=c.get('/api/teaching/packs/'+pk,headers=auth(a));assert r.status_code==200
    assert r.json()['source_example_ids']==[e]
    assert 'شو الأهم' in r.json()['system_prompt']
    assert 'voiceId' not in r.json()['system_prompt']
    r2=c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p,'approve':True})
    assert r2.json()['id']==pk and not f.calls

def test_holdout_and_pending_excluded(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    h=example(c,a,p,scenario='holdout-03').json()['id'];approve(c,a,h)
    example(c,a,p,scenario='budget-01')
    data=snapshot(s,p)
    assert [x['id'] for x in data['examples']]==[e]
    assert 'تضمنون' not in prompt_for(data)

def test_different_profile_isolated(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    assert c.get('/api/teaching/packs/'+pk,headers=auth(u)).status_code==403
    p2=profile(c,u)
    assert c.post('/api/teaching/packs',headers=auth(a),json={'profile_id':p2,'approve':True}).status_code==403

def test_rejection_invalidates_pack(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    c.post(f'/api/examples/{e}/review',headers=auth(a),json={'status':'rejected'})
    assert c.get('/api/teaching/packs/'+pk,headers=auth(a)).status_code==409
    assert c.get('/api/teaching/packs',params={'profile_id':p},headers=auth(a)).json()['items'][0]['valid'] is False

def test_withdrawal_and_reconsent_do_not_reauthorize_old_samples(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    c.post('/api/profiles/'+p+'/withdraw',headers=auth(a),json={})
    consent_for(c,a,p)
    assert c.get('/api/teaching/packs/'+pk,headers=auth(a)).status_code==409

def test_profile_change_invalidates_old_pack(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    s.execute('UPDATE profiles SET dialect=? WHERE id=?',('Different dialect',p))
    assert c.get('/api/teaching/packs/'+pk,headers=auth(a)).status_code==409

def test_profile_delete_cascades_without_breaking_core(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    assert c.delete('/api/profiles/'+p,headers=auth(a)).status_code==200
    assert s.one('SELECT * FROM teaching_packs WHERE id=?',(pk,)) is None

def test_releases_survive_restart(env,tmp_path):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    c2=TestClient(create_app(tmp_path))
    assert c2.get('/api/teaching/packs/'+pk,headers=auth(a)).status_code==200

def test_compare_requires_opt_in(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch)
    assert c.post(f'/api/teaching/packs/{pk}/compare',headers=auth(a),json={'message':'test'}).status_code==403
    assert not f.calls

def test_compare_missing_key_explicit(env):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p)
    r=c.post(f'/api/teaching/packs/{pk}/compare',headers=auth(a),json={'message':'test','authorize_external':True})
    assert r.status_code==503 and 'OPENAI_API_KEY' in r.text and not f.calls

def test_compare_same_model_only_examples_differ(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch)
    r=c.post(f'/api/teaching/packs/{pk}/compare',headers=auth(a),json={'message':'New caller need','authorize_external':True})
    assert r.status_code==200,r.text
    assert len(f.calls)==2
    b,t=[x[4]['json'] for x in f.calls]
    assert b['model']==t['model'] and b['store'] is False
    assert b['messages'][1]==t['messages'][1]
    assert 'شو الأهم' not in b['messages'][0]['content'] and 'شو الأهم' in t['messages'][0]['content']
    assert s.one('SELECT calls FROM teaching_usage')['calls']==2

def test_failed_compare_no_automatic_retry(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch);f.fail=True
    assert c.post(f'/api/teaching/packs/{pk}/compare',headers=auth(a),json={'message':'hello','authorize_external':True}).status_code==502
    assert len(f.calls)==1

def test_daily_cap_persists(env,monkeypatch):
    c,s,a,u,f=env;monkeypatch.setenv('RANEEN_AI_DAILY_CALL_LIMIT','1')
    reserve_calls(s)
    with pytest.raises(HTTPException) as caught:reserve_calls(s)
    assert caught.value.status_code==429

def test_bad_daily_config_fails_closed(env,monkeypatch):
    c,s,a,u,f=env;monkeypatch.setenv('RANEEN_AI_DAILY_CALL_LIMIT','bad')
    with pytest.raises(HTTPException):reserve_calls(s)

def audio(c,a,p):
    return c.post('/api/profiles/'+p+'/audio',headers={**auth(a),'Content-Type':'audio/wav'},content=wav()).json()['id']

def test_transcribe_owned_audio_only_and_needs_consent(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);aid=audio(c,a,p);keys(monkeypatch)
    assert c.post('/api/teaching/transcribe',headers=auth(a),json={'audio_id':aid}).status_code==403
    r=c.post('/api/teaching/transcribe',headers=auth(a),json={'audio_id':aid,'authorize_external':True})
    assert r.status_code==200,r.text
    assert r.json()['verified'] is False
    assert f.calls[0][4]['data']['response_format']=='json'
    c.post('/api/profiles/'+p+'/withdraw',headers=auth(a),json={})
    assert c.post('/api/teaching/transcribe',headers=auth(a),json={'audio_id':aid,'authorize_external':True}).status_code==409

def test_transcribe_other_profile_forbidden(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,u);aid=audio(c,u,p);keys(monkeypatch)
    assert c.post('/api/teaching/transcribe',headers=auth(a),json={'audio_id':aid,'authorize_external':True}).status_code==403
    assert not f.calls

def test_vapi_needs_voice_permission(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch)
    r=c.post(f'/api/teaching/packs/{pk}/vapi-test',headers=auth(a),json={'authorize_external':True})
    assert r.status_code==403 and not f.calls

def test_vapi_creates_separate_tool_free_assistant_once(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch)
    args={'authorize_external':True,'authorize_test_voice':True}
    r=c.post(f'/api/teaching/packs/{pk}/vapi-test',headers=auth(a),json=args)
    assert r.status_code==200,r.text
    assert r.json()['original_unchanged'] is True
    assert [x[1] for x in f.calls]==['GET','POST']
    config=f.calls[1][4]['json']
    assert config['name'].startswith('Raneen TEST')
    assert 'serverUrl' not in config and 'tools' not in config['model']
    assert config['serverMessages']==[]
    assert config['artifactPlan']['recordingEnabled'] is False
    assert 'NEVER_COPY' not in json.dumps(config)
    assert 'شو الأهم' in config['model']['messages'][0]['content']
    assert c.post(f'/api/teaching/packs/{pk}/vapi-test',headers=auth(a),json=args).status_code==200
    assert len(f.calls)==2

def test_uncertain_publication_blocks_duplicate(env,monkeypatch):
    c,s,a,u,f=env;p=profile(c,a);pk,e=pack(c,a,p);keys(monkeypatch)
    s.execute('INSERT INTO teaching_publications VALUES(?,?,NULL,?)',(pk,'unknown','test'))
    r=c.post(f'/api/teaching/packs/{pk}/vapi-test',headers=auth(a),json={'authorize_external':True,'authorize_test_voice':True})
    assert r.status_code==409 and not f.calls

def test_provider_errors_do_not_leak_response_or_secret(monkeypatch):
    import asyncio,httpx
    class BadClient:
        def __init__(self,**kw): assert kw['follow_redirects'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kw):return httpx.Response(401,json={'error':'secret-token-and-private-content'})
    monkeypatch.setattr(httpx,'AsyncClient',BadClient)
    with pytest.raises(HTTPException) as e:asyncio.run(Provider().request('openai','POST','/chat/completions','secret'))
    assert 'secret-token' not in str(e.value.detail) and e.value.status_code==502

def test_hosted_new_routes_keep_guards(tmp_path):
    app=create_app(tmp_path,public_origin='https://studio.example.com',owner_only=True)
    s=app.state.store;a=s.create_user('Owner','admin');c=TestClient(app,base_url='https://studio.example.com')
    assert c.get('/api/teaching/status').status_code==401
    assert c.get('/api/teaching/status',headers=auth(a)).status_code==200
    assert c.post('/api/teaching/packs',headers={**auth(a),'Origin':'https://evil.example'},json={'profile_id':'x','approve':True}).status_code==403
    assert 'frame-ancestors' in c.get('/').headers['content-security-policy']
    assert 'private-test' not in c.get('/static/guided.js').text
