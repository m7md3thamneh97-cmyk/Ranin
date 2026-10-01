import pytest
from fastapi import HTTPException

from studio.app import Store, now
from studio.transport import TransportRegistry


@pytest.fixture
def transport(tmp_path):
    store=Store(tmp_path)
    owner=store.create_user('Synthetic owner','admin')
    stamp=now()
    store.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)',('profile',owner['id'],'Synthetic agent','Arabic','both','',stamp))
    store.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,?)',('consent','profile','test',1,0,0,'Synthetic consent',stamp,None))
    store.execute('INSERT INTO teaching_sessions(id,profile_id,consent_id,started_at,created_at) VALUES(?,?,?,?,?)',('session','profile','consent',stamp,stamp))
    session=store.one('SELECT * FROM teaching_sessions WHERE id=?',('session',))
    session['owner_id']=owner['id']
    class Fake:
        creates=0
        deleted=[]
        fail_create=False
        fail_delete=False
        config=None
        def create_assistant(self,config):
            self.creates+=1
            self.config=config
            if self.fail_create:
                raise TimeoutError('Synthetic uncertain outcome')
            return 'synthetic-assistant'
        def get_assistant(self,ident):
            return {'id':ident,'metadata':self.config['metadata']}
        def delete_assistant(self,ident):
            if self.fail_delete:
                raise TimeoutError('Synthetic offline cleanup')
            self.deleted.append(ident)
    fake=Fake()
    return store,session,fake,TransportRegistry(store,lambda:fake)


def test_transport_replay_creates_exactly_one_assistant(transport):
    store,session,fake,registry=transport
    config={'metadata':{'raneen_session_id':'session'},'name':'Synthetic'}
    assert registry.provision(session,config)=='synthetic-assistant'
    assert registry.provision(session,config)=='synthetic-assistant'
    assert fake.creates==1
    assert 'raneen_operation_id' in fake.config['metadata']


def test_uncertain_creation_cannot_repeat_after_restart(transport):
    store,session,fake,registry=transport
    fake.fail_create=True
    config={'metadata':{'raneen_session_id':'session'}}
    with pytest.raises(TimeoutError):
        registry.provision(session,config)
    assert store.one('SELECT state FROM transport_operations')['state']=='outcome_unknown'
    TransportRegistry(store,lambda:fake).recover()
    with pytest.raises(HTTPException) as error:
        registry.provision(session,config)
    assert error.value.status_code==409
    assert fake.creates==1
    reconciled=registry.reconcile('session','synthetic-created-before-timeout',session['owner_id'])
    assert reconciled['state']=='ready'
    assert registry.provision(session,config)=='synthetic-created-before-timeout'
    assert fake.creates==1


def test_unknown_assistant_cannot_be_reconciled_to_another_session(transport):
    store,session,fake,registry=transport
    fake.fail_create=True
    with pytest.raises(TimeoutError):
        registry.provision(session,{'metadata':{'raneen_session_id':'session'}})
    with pytest.raises(HTTPException):
        registry.reconcile('another-session','synthetic-assistant',session['owner_id'])
    assert store.one('SELECT provider_id FROM transport_operations')['provider_id'] is None


def test_remote_cleanup_receipt_survives_source_deletion(transport):
    store,session,fake,registry=transport
    registry.provision(session,{'metadata':{'raneen_session_id':'session'}})
    fake.fail_delete=True
    assert registry.cleanup('profile')[0]['state']=='deletion_pending'
    store.execute('DELETE FROM teaching_sessions WHERE id=?',('session',))
    store.execute('DELETE FROM consents WHERE id=?',('consent',))
    store.execute('DELETE FROM profiles WHERE id=?',('profile',))
    fake.fail_delete=False
    assert registry.recover()[0]['state']=='deleted'
    assert fake.deleted==['synthetic-assistant']


def test_late_found_assistant_is_cleaned_after_deleted_profile(transport):
    store,session,fake,registry=transport
    fake.fail_create=True
    with pytest.raises(TimeoutError):
        registry.provision(session,{'metadata':{'raneen_session_id':'session'}})
    store.execute('DELETE FROM teaching_sessions WHERE id=?',('session',))
    store.execute('DELETE FROM consents WHERE id=?',('consent',))
    store.execute('DELETE FROM profiles WHERE id=?',('profile',))
    with pytest.raises(HTTPException) as error:
        registry.reconcile('session','synthetic-late-assistant','stranger')
    assert error.value.status_code==403
    assert fake.deleted==[]
    registry.reconcile('session','synthetic-late-assistant',session['owner_id'])
    assert fake.deleted==['synthetic-late-assistant']
    assert store.one('SELECT state FROM transport_operations')['state']=='deleted'
