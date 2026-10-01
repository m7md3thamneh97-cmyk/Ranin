"""Read-only contract smoke against a RAN-BE-01 checkout, using synthetic data.

python tests/learning_backend_contract.py --backend-root /path/to/Ranin
No hosted service or provider is contacted. No source files are modified.
"""
import argparse
import os
from pathlib import Path
import sys
import tempfile

parser=argparse.ArgumentParser()
parser.add_argument('--backend-root',type=Path,default=Path(__file__).resolve().parents[1])
args=parser.parse_args()
for key in list(os.environ):
    if key.startswith(('OPENAI_','ELEVENLABS_','VAPI_','RANEEN_')):
        os.environ.pop(key,None)
sys.path.insert(0,str(args.backend_root.resolve()))
from studio.app import create_app
from fastapi.testclient import TestClient

with tempfile.TemporaryDirectory(prefix='raneen-contract-') as d:
    app=create_app(d)
    user=app.state.store.create_user('Synthetic contract check','admin')
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+user['token']
        profile=client.post('/api/profiles',json={'name':'Synthetic trainer','dialect':'English','contribution':'both','style_notes':''}).json()
        pid=profile['id']
        consent=client.get('/api/consent-text').json()
        response=client.post('/api/profiles/'+pid+'/consent',json={'collection':True,'self_attestation':True,'version':consent['version']})
        assert response.is_success,response.status_code
        response=client.post('/api/sessions',json={'profile_id':pid,'mode':'teaching','realtime_provider':'local','idempotency_key':'frontend-contract'})
        assert response.status_code==201,response.status_code
        sid=response.json()['id']
        state=client.get('/api/profiles/'+pid+'/learning-state').json()
        events=client.get('/api/sessions/'+sid+'/events?after=0&limit=100').json()
        assert state['profile_id']==pid and isinstance(state['hypotheses'],list)
        assert isinstance(events['cursor'],int) and isinstance(events['items'],list)
        assert isinstance(state['versions'],list)
        print('PASS: frontend learning-state, versions, and event cursor contracts match the backend; temporary synthetic database, no providers.')
