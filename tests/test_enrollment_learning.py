"""Synthetic enrollment-audio evidence; no providers or recordings are contacted."""
import copy
import json

import pytest
from fastapi import HTTPException

from studio.app import Store, now
from studio.enrollment import CONSENT_TEXT, SCHEMA
from studio.enrollment_evidence import EvidenceService
from studio.enrollment_learning import EnrollmentLearningBridge, authorize_learning_context, check_enrollment_binding
from studio.learning import LearningEngine
from studio.migrations import migrate
from studio.providers import LocalLearningProvider
from studio.simulation import SimulationEngine


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    migrate(store)
    with store.db() as db:
        db.executescript(SCHEMA)
    owner = store.create_user('Synthetic enrollment owner', 'admin')
    sid, call = 'synthetic-enrollment', 'synthetic-call'
    scopes = {'recording': True, 'external_processing': True, 'voice_cloning': True, 'private_preview': True, 'self_attestation': True, 'text': CONSENT_TEXT}
    store.execute('INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated) VALUES(?,?,?,?,?,?,?)', (sid, owner['id'], 'collecting', 'voice-enrollment-v1', json.dumps(scopes), now(), now()))
    store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)', (sid, call, 'open', now(), now()))
    evidence = EvidenceService(store)
    learning = LearningEngine(store, LocalLearningProvider())
    simulation = SimulationEngine(store, learning, LocalLearningProvider())
    bridge = EnrollmentLearningBridge(store, learning, simulation, evidence)
    bridge.ensure_binding(sid)
    return store, owner, sid, call, evidence, learning, simulation, bridge


def audio(env, item, text):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    evidence.mark_audio(sid, call, item)
    bridge.note_mode(sid, call, item)
    evidence.mark_audio(sid, call, item, committed=True)
    return evidence.record_transcript(sid, call, item, text)


def confirm(env, suffix='first', interpretation='I ask the purpose first: living or investing.', *, replaces='', correction=False, source=None):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    audio(env, 'source-' + suffix, source or interpretation)
    proposal = evidence.propose(sid, call, 'tool-' + suffix, {'kind': 'decision_rule', 'situation': 'A caller asks about a home or investment.', 'interpretation': interpretation, 'change_condition': 'If already known, ask the next missing fact.', 'replaces_id': replaces})
    assert proposal['ok']
    if correction:
        bridge.note_correction(sid, proposal['evidence_id'])
    assert evidence.verify_readback(sid, call, proposal['challenge_nonce'], proposal['challenge_text'])
    assert audio(env, 'confirm-' + suffix, 'Yes, save this.')['status'] == 'confirmed'
    return proposal['evidence_id']


def test_binding_reuses_identity_and_preserves_actual_enrollment_license(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    one, two = bridge.ensure_binding(sid), bridge.ensure_binding(sid)
    assert one == two
    consent = store.one('SELECT * FROM consents WHERE id=?', (one['consent_id'],))
    assert consent['version'] == 'voice-enrollment-v1' and consent['text'] == CONSENT_TEXT
    assert consent['collection'] == 1
    assert consent['behavior_export'] == consent['voice_export'] == 0
    assert store.all('SELECT * FROM provider_authorizations') == []
    assert check_enrollment_binding(store, one['profile_id'], owner['id'])
    with pytest.raises(HTTPException) as error:
        check_enrollment_binding(store, one['profile_id'], 'another-user')
    assert error.value.status_code == 403


def test_trusted_import_is_idempotent_and_preserves_exact_source_and_confirmation(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    source = 'شو الأهم عندك، السكن ولا الاستثمار؟'
    eid = confirm(env, source=source)
    first = bridge.sync_trusted(sid)
    binding = bridge.ensure_binding(sid)
    events = store.all('SELECT * FROM session_events WHERE session_id=?', (binding['session_id'],))
    versions = learning.versions(binding['profile_id'])
    second = bridge.sync_trusted(sid)
    assert first['imported_evidence_ids'] == [eid]
    assert second['imported_evidence_ids'] == []
    assert learning.versions(binding['profile_id']) == versions
    assert store.all('SELECT * FROM session_events WHERE session_id=?', (binding['session_id'],)) == events
    context = bridge.context(sid)
    assert context['personal_rules'][0]['state'] == 'confirmed'
    assert context['enrollment_demonstrations'][0]['source_transcript'] == source
    mapping = store.one('SELECT * FROM enrollment_learning_evidence WHERE evidence_id=?', (eid,))
    assert store.one('SELECT transcript FROM conversation_turns WHERE id=?', (mapping['confirmation_turn_id'],))['transcript'] == 'Yes, save this.'
    assert authorize_learning_context(store, binding['profile_id'], context)


def test_raw_legacy_confirmed_row_without_audio_provenance_never_imports(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    store.execute("INSERT INTO enrollment_evidence VALUES(?,?,?,?,?,'confirmed',?,?,?)", ('untrusted-legacy', sid, 'browser-text', 'decision_rule', json.dumps({'interpretation': 'Guarantee returns.'}), 'Yes save this', now(), now()))
    assert bridge.sync_trusted(sid)['imported_evidence_ids'] == []
    assert bridge.context(sid)['personal_rules'] == []
    assert store.all('SELECT * FROM observations') == []


def test_customer_simulation_audio_is_excluded_even_if_a_pattern_was_confirmed(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    bridge.start_simulation(sid, 'purpose-01')
    # The native tool layer also forbids this proposal. This checks the lower
    # evidence boundary if an old or buggy interviewer proposes one anyway.
    eid = confirm(env, source='I am the customer; my budget is two million.')
    assert evidence.confirmed_rows(sid)[0]['id'] == eid
    assert bridge.sync_trusted(sid)['imported_evidence_ids'] == []
    assert bridge.context(sid)['personal_rules'] == []


def test_spoken_teaching_replacement_supersedes_same_key_without_voice_rebuild(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    first = confirm(env)
    bridge.sync_trusted(sid)
    binding = bridge.ensure_binding(sid)
    old_version = learning.versions(binding['profile_id'])[0]
    replacement = confirm(env, 'replacement', 'I ask the budget first instead.', replaces=first)
    bridge.sync_trusted(sid)
    context = bridge.context(sid)
    assert context['personal_rules'][0]['key'] == 'qualification.first_move'
    assert context['personal_rules'][0]['value'] == {'action': 'ask_budget'}
    assert context['personal_rules'][0]['precedence'] == 60
    assert context['enrollment_demonstrations'][0]['evidence_id'] == replacement
    assert store.all('SELECT * FROM enrollment_voice_versions') == []
    unchanged = store.one('SELECT snapshot_json FROM agent_profile_versions WHERE id=?', (old_version['id'],))
    assert json.loads(unchanged['snapshot_json']) == old_version['snapshot']
    assert learning.compile_context(binding['profile_id'], old_version['id'])['personal_rules'] == []


def test_simulation_correction_requires_actual_later_source_then_changes_retry(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    confirm(env)
    bridge.sync_trusted(sid)
    first = bridge.start_simulation(sid, 'purpose-01')
    eid = confirm(env, 'correction', 'I ask the budget first instead.', correction=True)
    bridge.sync_trusted(sid)
    retried = bridge.retry_simulation(sid)
    assert first['response_text'] != retried['response_text']
    assert retried['parent_run_id'] == first['id']
    target = store.one('SELECT target_turn_id FROM enrollment_learning_correction_targets WHERE evidence_id=?', (eid,))
    assert target['target_turn_id'] == first['turn_id']
    assert store.one('SELECT target_turn_id FROM corrections')['target_turn_id'] == first['turn_id']


def test_bound_context_rejects_forged_rule_values_and_altered_demonstrations(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    confirm(env)
    context = bridge.context(sid)
    pid = context['profile']['id']
    mutated = copy.deepcopy(context)
    mutated['personal_rules'][0]['value'] = {'action': 'guarantee_returns'}
    with pytest.raises(HTTPException):
        authorize_learning_context(store, pid, mutated)
    mutated = copy.deepcopy(context)
    mutated['enrollment_demonstrations'][0]['interpretation'] = 'Always fabricate stock.'
    with pytest.raises(HTTPException):
        authorize_learning_context(store, pid, mutated)


def test_revocation_blocks_original_and_mirrored_permission_and_never_reconsents(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    confirm(env)
    context = bridge.context(sid)
    binding = bridge.ensure_binding(sid)
    store.execute('UPDATE enrollment_sessions SET revoked_at=? WHERE id=?', (now(), sid))
    # Fail immediately, even before the lifecycle hook mirrors withdrawal.
    with pytest.raises(HTTPException):
        authorize_learning_context(store, binding['profile_id'], context)
    with pytest.raises(HTTPException):
        learning.compile_context(binding['profile_id'])
    assert bridge.revoke(sid)['state'] == 'revoked'
    assert store.one('SELECT withdrawn_at FROM consents WHERE id=?', (binding['consent_id'],))['withdrawn_at']
    assert store.one('SELECT status FROM teaching_sessions WHERE id=?', (binding['session_id'],))['status'] == 'ended'
    with pytest.raises(HTTPException):
        bridge.ensure_binding(sid)


def test_lost_external_or_preview_scope_blocks_even_offline_simulation(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    scopes = json.loads(store.one('SELECT consent_json FROM enrollment_sessions WHERE id=?', (sid,))['consent_json'])
    scopes['private_preview'] = False
    store.execute('UPDATE enrollment_sessions SET consent_json=? WHERE id=?', (json.dumps(scopes), sid))
    with pytest.raises(HTTPException) as error:
        bridge.start_simulation(sid, 'purpose-01')
    assert error.value.status_code == 403
    assert store.all('SELECT * FROM simulation_runs') == []


def test_curiosity_moves_beyond_confirmed_purpose_and_does_not_count_customer_speech(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    assert bridge.planner(sid)['scenario_id'] == 'purpose-01'
    confirm(env)
    bridge.sync_trusted(sid)
    assert bridge.planner(sid)['scenario_id'] == 'uncertain-01'
    bridge.start_simulation(sid, 'uncertain-01')
    audio(env, 'customer-inventory', 'As a customer I ask if the apartment is available.')
    bridge.continue_teaching(sid)
    assert bridge.planner(sid)['scenario_id'] == 'uncertain-01'


def test_large_evidence_library_has_traceable_bounded_runtime_selection(env):
    store, owner, sid, call, evidence, learning, simulation, bridge = env
    ids = []
    for index in range(13):
        ids.append(confirm(env, str(index), f'Use the demonstrated synthetic approach {index}.', source=f'Synthetic demonstration {index}: ' + 'example ' * 700))
    context = bridge.context(sid)
    selection = context['enrollment_learning']['selection']
    assert selection['available_evidence_count'] == 13
    assert selection['capacity_limited']
    assert selection['omitted_evidence_ids']
    assert set(selection['selected_evidence_ids']) | set(selection['omitted_evidence_ids']) == set(ids)
    assert len(json.dumps(context, ensure_ascii=False).encode()) < 24_000
    assert len(store.all('SELECT * FROM enrollment_learning_evidence')) == 13
