import json
import sqlite3
import uuid

import pytest
from fastapi import HTTPException

from studio.app import Store, now
from studio.learning import LearningEngine
from studio.migrations import migrate


class Model:
    def __init__(self, value='ask_purpose'):
        self.value = value
        self.calls = 0

    def analyze(self, transcript, context):
        self.calls += 1
        return [{'category': 'behavior', 'key': 'qualification.first_move', 'value': {'action': self.value}, 'confidence': .65}]


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    migrate(store)
    user = store.create_user('Trainer')
    pid = uuid.uuid4().hex
    store.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)', (pid, user['id'], 'Trainer', 'Emirati Arabic', 'both', '', now()))
    consent = uuid.uuid4().hex
    store.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,?)', (consent, pid, 'internal-research-v0.1', 1, 1, 1, 'consent', now(), None))
    model = Model()
    engine = LearningEngine(store, model)
    return store, user, pid, consent, model, engine


def session(env, split='train', scenario='purpose-01', mode='teaching', consent=None):
    store, user, pid, cid, model, engine = env
    ident = uuid.uuid4().hex
    stamp = now()
    store.execute('''INSERT INTO teaching_sessions(id,profile_id,consent_id,status,mode,split,scenario_id,realtime_provider,started_at,created_at)
        VALUES(?,?,?,'active',?,?,?,'local',?,?)''', (ident, pid, consent or cid, mode, split, scenario, stamp, stamp))
    return ident


def turn(env, sid, index=0, role='trainer', text='What matters most: living there or investment?', consent=None):
    store, user, pid, cid, model, engine = env
    ident = uuid.uuid4().hex
    stamp = now()
    capture_mode = store.one('SELECT mode FROM teaching_sessions WHERE id=?', (sid,))['mode']
    store.execute('''INSERT INTO conversation_turns(id,session_id,profile_id,consent_id,turn_index,role,transcript,transcript_state,started_at,ended_at,created_at,provider_metadata_json)
        VALUES(?,?,?,?,?,?,?,'final',?,?,?,?)''', (ident, sid, pid, consent or cid, index, role, text, stamp, stamp, stamp, json.dumps({'capture_mode': capture_mode})))
    return ident


def correction(env, sid, index, value, *, locked=False, actor=None):
    store, user, pid, cid, model, engine = env
    target = turn(env, sid, index, 'raneen', 'Which area?')
    source = turn(env, sid, index + 1, text='Ask ' + value + ' first.')
    result = engine.record_correction(pid, sid, target, 'Ask ' + value + ' first.', {'category': 'behavior', 'key': 'qualification.first_move', 'value': {'action': value}}, source, locked, actor or user['id'])
    return result, target, source


def action(context):
    return context['personal_rules'][0]['value']['action'] if context['personal_rules'] else None


def test_repeated_evidence_is_independent_and_analysis_idempotent(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    first = turn(env, sid)
    learned = engine.analyze_turn(first)
    assert learned['hypotheses'][0]['state'] == 'tentative'
    repeated = engine.analyze_turn(first)
    assert repeated['idempotent']
    assert model.calls == 1
    assert repeated['hypotheses'][0]['evidence_count'] == 1
    engine.analyze_turn(turn(env, sid, 1))
    hypothesis = engine.learning_state(pid)['hypotheses'][0]
    assert hypothesis['evidence_count'] == 2
    assert hypothesis['precedence'] == 30
    assert hypothesis['state'] == 'tentative'


def test_holdout_and_customer_simulation_cannot_teach(env):
    engine = env[-1]
    for sid in [session(env, split='holdout'), session(env, scenario='holdout-01'), session(env, mode='simulation')]:
        with pytest.raises(HTTPException) as error:
            engine.analyze_turn(turn(env, sid))
        assert error.value.status_code == 409
    assert env[0].all('SELECT * FROM observations') == []
    assert env[-2].calls == 0


def test_ai_words_and_cross_profile_evidence_rejected(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    with pytest.raises(HTTPException) as error:
        engine.analyze_turn(turn(env, sid, role='raneen'))
    assert error.value.status_code == 422
    other = store.create_user('Other')
    learned = engine.analyze_turn(turn(env, sid, 1))
    confirmation = turn(env, sid, 2, text='Yes, that is my approach.')
    with pytest.raises(HTTPException) as error:
        engine.confirm_hypothesis(learned['hypotheses'][0]['id'], confirmation, actor_id=other['id'])
    assert error.value.status_code == 403
    confirmed = engine.confirm_hypothesis(learned['hypotheses'][0]['id'], confirmation, actor_id=user['id'])
    assert confirmed['hypotheses'][0]['state'] == 'confirmed'


def test_correction_changes_retry_without_mutating_snapshot(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    learned = engine.analyze_turn(turn(env, sid))
    original = learned['profile_version']
    corrected, target, source = correction(env, sid, 1, 'ask_budget')
    assert corrected['applied']
    assert action(engine.compile_context(pid)) == 'ask_budget'
    assert action(engine.compile_context(pid, original['id'])) == 'ask_purpose'
    unchanged = store.one('SELECT * FROM agent_profile_versions WHERE id=?', (original['id'],))
    assert json.loads(unchanged['snapshot_json']) == original['snapshot']
    with pytest.raises(sqlite3.IntegrityError):
        store.execute("UPDATE agent_profile_versions SET snapshot_json='{}' WHERE id=?", (original['id'],))
    assert store.one('SELECT * FROM corrections WHERE id=?', (corrected['correction_id'],))['source_turn_id'] == source


def test_locked_explicit_rule_outweighs_correction_and_new_inference(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    locked, _, _ = correction(env, sid, 0, 'ask_purpose', locked=True)
    corrected, _, _ = correction(env, sid, 2, 'ask_budget')
    assert not corrected['applied']
    assert corrected['blocked_by_locked_rule']
    model.value = 'ask_location'
    engine.analyze_turn(turn(env, sid, 4))
    context = engine.compile_context(pid)
    assert action(context) == 'ask_purpose'
    assert context['personal_rules'][0]['state'] == 'locked'


def test_replaying_old_correction_does_not_roll_back_new_correction(env):
    store, user, pid, cid, model, engine = env
    sid = session(env, mode='simulation')
    first, target, source = correction(env, sid, 0, 'ask_budget')
    second, _, _ = correction(env, sid, 2, 'ask_purpose')
    count = len(engine.versions(pid))
    replay = engine.record_correction(pid, sid, target, 'Ask ask_budget first.', {'category': 'behavior', 'key': 'qualification.first_move', 'value': {'action': 'ask_budget'}}, source, actor_id=user['id'])
    assert replay['idempotent']
    assert len(engine.versions(pid)) == count
    assert action(engine.compile_context(pid)) == 'ask_purpose'


def test_contradictory_inferences_are_visible_not_averaged(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    engine.analyze_turn(turn(env, sid))
    model.value = 'ask_budget'
    engine.analyze_turn(turn(env, sid, 1))
    state = engine.learning_state(pid)
    assert all(h['conflict'] for h in state['hypotheses'])
    assert engine.compile_context(pid)['personal_rules'] == []
    assert state['planner']['action'] == 'challenge_contradiction'


def test_withdrawal_and_reconsent_do_not_restore_old_evidence(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    learned = engine.analyze_turn(turn(env, sid))
    version = learned['profile_version']['id']
    store.execute('UPDATE consents SET withdrawn_at=? WHERE id=?', (now(), cid))
    with pytest.raises(HTTPException):
        engine.compile_context(pid, version)
    fresh = uuid.uuid4().hex
    store.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,?)', (fresh, pid, 'internal-research-v0.1', 1, 1, 1, 'new consent', now(), None))
    context = engine.compile_context(pid, version)
    assert context['personal_rules'] == []
    assert context['snapshot']['real_estate_behavior'] == {}
    assert context['provenance']['source_turn_ids'] == []


def test_provider_failure_and_withdrawal_race_write_no_learning(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    source = turn(env, sid)
    def failing(transcript, context):
        raise RuntimeError('provider unavailable')
    model.analyze = failing
    with pytest.raises(RuntimeError):
        engine.analyze_turn(source)
    assert store.all('SELECT * FROM observations') == []
    assert engine.versions(pid) == []
    def withdrawing(transcript, context):
        store.execute('UPDATE consents SET withdrawn_at=? WHERE id=?', (now(), cid))
        return [{'category': 'behavior', 'key': 'qualification.first_move', 'value': {'action': 'ask_budget'}, 'confidence': .7}]
    model.analyze = withdrawing
    with pytest.raises(HTTPException) as error:
        engine.analyze_turn(source)
    assert error.value.status_code == 409
    assert store.all('SELECT * FROM observations') == []
    assert store.all('SELECT * FROM learning_analyses') == []


def test_reviewed_train_examples_import_with_exact_provenance(env):
    store, user, pid, cid, model, engine = env
    included = uuid.uuid4().hex
    holdout = uuid.uuid4().hex
    for ident, scenario in [(included, 'purpose-01'), (holdout, 'holdout-01')]:
        payload = {'scenario_id': scenario, 'response_text': 'Exact approved human wording.', 'transcript_verified': True, 'decision_cue': 'Purpose matters first.'}
        store.execute('INSERT INTO examples(id,profile_id,consent_id,payload,status,created) VALUES(?,?,?,?,?,?)', (ident, pid, cid, json.dumps(payload), 'approved', now()))
    result = engine.import_examples(pid)
    assert result['imported_examples'] == 1
    assert model.calls == 1
    context = engine.compile_context(pid)
    assert context['provenance']['source_example_ids'] == [included]
    assert context['representative_examples'][0]['response_text'] == 'Exact approved human wording.'
    store.execute("UPDATE examples SET status='rejected' WHERE id=?", (included,))
    assert engine.compile_context(pid)['personal_rules'] == []
    assert engine.compile_context(pid)['representative_examples'] == []


def test_teaching_evidence_survives_session_role_reversal(env):
    store, user, pid, cid, model, engine = env
    sid = session(env)
    source = turn(env, sid)
    engine.analyze_turn(source)
    store.execute("UPDATE teaching_sessions SET mode='simulation' WHERE id=?", (sid,))
    assert action(engine.compile_context(pid)) == 'ask_purpose'
    next_source = turn(env, sid, 1)
    with pytest.raises(HTTPException):
        engine.analyze_turn(next_source)
    assert action(engine.compile_context(pid)) == 'ask_purpose'
