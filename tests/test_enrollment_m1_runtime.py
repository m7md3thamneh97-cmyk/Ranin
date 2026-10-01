"""Synthetic trusted provider events; no microphones or paid/network operations."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from studio.app import now
from studio.runtime import create_app
from studio.teaching import personal_runtime_prompt


@pytest.fixture
def linked(tmp_path, monkeypatch):
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    app = create_app(tmp_path)
    store = app.state.store
    owner = store.create_user('Synthetic owner', 'admin')
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + owner['token']}
    result = client.post('/api/enrollment/sessions', headers=headers, json={
        'recording': True, 'external_processing': True, 'voice_cloning': True,
        'private_preview': True, 'self_attestation': True})
    assert result.status_code == 201, result.text
    sid = result.json()['id']
    call = 'rtc_synthetic_m1'
    store.execute('INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)', (sid, call, 'open', now(), now()))
    return client, app, store, owner, headers, sid, call


def speak(linked, ident, text):
    _, app, _, _, _, sid, call = linked
    bridge = app.state.enrollment_sideband
    bridge.consume(sid, call, {'type': 'input_audio_buffer.speech_started', 'item_id': ident})
    bridge.consume(sid, call, {'type': 'input_audio_buffer.committed', 'item_id': ident})
    return bridge.consume(sid, call, {'type': 'conversation.item.input_audio_transcription.completed', 'item_id': ident, 'transcript': text})


def tool(linked, ident, name, args):
    _, app, _, _, _, sid, call = linked
    return app.state.enrollment_sideband.consume(sid, call, {
        'type': 'response.done', 'response': {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': ident, 'name': name, 'arguments': json.dumps(args)}]}})


def confirm(linked, proposal_messages, suffix):
    _, app, store, _, _, sid, call = linked
    challenge = next(m['response']['metadata']['raneen_challenge'] for m in proposal_messages
        if m.get('type') == 'response.create' and 'raneen_challenge' in m.get('response', {}).get('metadata', {}))
    row = store.one('SELECT challenge_text FROM enrollment_evidence_provenance WHERE challenge_nonce=?', (challenge,))
    app.state.enrollment_sideband.consume(sid, call, {'type': 'response.done', 'response': {
        'status': 'completed', 'metadata': {'raneen_challenge': challenge}, 'output': [
            {'role': 'assistant', 'content': [{'type': 'output_audio', 'transcript': row['challenge_text']}]}]}})
    return speak(linked, 'confirm-' + suffix, 'Yes, save this.')


def result(messages):
    return json.loads(next(m['item']['output'] for m in messages if m.get('item', {}).get('type') == 'function_call_output'))


def teach(linked):
    speak(linked, 'source-first', 'I first ask the purpose: home or investment.')
    proposed = tool(linked, 'proposal-first', 'propose_evidence', {
        'kind': 'decision_rule', 'situation': 'A caller mixes home and investment goals.',
        'interpretation': 'I first ask the purpose: home or investment.',
        'change_condition': 'If the goal is clear I ask about the budget.', 'replaces_id': ''})
    confirm(linked, proposed, 'first')


def test_journey_binding_is_stable_and_readable_with_the_existing_frontend_contract(linked):
    client, app, store, owner, headers, sid, call = linked
    one = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=headers)
    two = client.get(f'/api/enrollment/sessions/{sid}/workflow', headers=headers)
    assert one.status_code == two.status_code == 200
    assert one.json()['learning'] == two.json()['learning']
    binding = one.json()['learning']
    assert binding['contract'] == 'raneen-backend-v1'
    assert binding['session_id'] != sid
    assert store.one('SELECT owner_id FROM profiles WHERE id=?', (binding['profile_id'],))['owner_id'] == owner['id']
    assert client.get(f"/api/profiles/{binding['profile_id']}/learning-state", headers=headers).status_code == 200
    assert client.post(f'/api/enrollment/sessions/{sid}/transcripts', headers=headers,
        json={'item_id': 'browser-injection', 'transcript': 'Yes save this'}).status_code == 410


def test_spoken_learning_practice_correction_retry_updates_version_and_preserves_original(linked):
    client, app, store, owner, headers, sid, call = linked
    teach(linked)
    binding = client.get(f'/api/enrollment/sessions/{sid}/journey', headers=headers).json()['learning']
    state = app.state.learning.learning_state(binding['profile_id'])
    assert state['hypotheses'] and state['profile_version_id']
    speak(linked, 'request-practice', 'Let us practice this case.')
    started = tool(linked, 'tool-start', 'start_simulation', {'scenario_id': 'purpose-01'})
    first = result(started)
    assert first['id']
    assert any(m.get('response', {}).get('metadata', {}).get('raneen_simulation') == first['id'] for m in started)
    # Ordinary role-play is never imported as a contributor demonstration.
    before = len(store.all('SELECT * FROM observations'))
    speak(linked, 'customer', 'I want an apartment and my budget is two million.')
    denied = tool(linked, 'tool-customer-evidence', 'propose_evidence', {
        'kind': 'decision_rule', 'situation': 'A buyer asks for an apartment.',
        'interpretation': 'Always ask budget first.', 'change_condition': 'Any buyer.', 'replaces_id': ''})
    assert result(denied)['ok'] is False
    assert len(store.all('SELECT * FROM observations')) == before
    speak(linked, 'human-correction', 'Ask the budget first instead.')
    correction = tool(linked, 'tool-correction', 'propose_correction', {
        'kind': 'decision_rule', 'situation': 'A caller mixes home and investment goals.',
        'interpretation': 'Ask the budget first instead.',
        'change_condition': 'If the budget is confirmed ask about the purpose.', 'replaces_id': ''})
    confirm(linked, correction, 'correction')
    speak(linked, 'request-retry', 'Please retry the same case.')
    retried = result(tool(linked, 'tool-retry', 'retry_simulation', {}))
    assert retried['parent_run_id'] == first['id']
    assert retried['profile_version_id'] != first['profile_version_id']
    assert retried['response_text'] != first['response_text']
    assert 'ميزانيتك' in retried['response_text']
    assert app.state.simulation.get(first['id'])['response_text'] == first['response_text']
    assert store.all('SELECT * FROM enrollment_voice_versions') == []


def test_replayed_provider_tool_does_not_generate_or_speak_a_second_simulation(linked):
    _, app, store, _, _, sid, call = linked
    speak(linked, 'practice', 'Let us practice.')
    first = tool(linked, 'replayed-tool', 'start_simulation', {'scenario_id': 'purpose-01'})
    repeated = tool(linked, 'replayed-tool', 'start_simulation', {'scenario_id': 'purpose-01'})
    assert result(repeated)['id'] == result(first)['id']
    assert len(store.all('SELECT * FROM simulation_runs')) == 1
    assert not any(m['type'] == 'response.create' for m in repeated)


def test_spoken_tools_reject_holdouts_and_revoked_enrollment(linked):
    client, app, store, owner, headers, sid, call = linked
    speak(linked, 'practice', 'Let us practice.')
    denied = result(tool(linked, 'heldout-tool', 'start_simulation', {'scenario_id': 'holdout-03'}))
    assert denied.get('status') == 409
    assert store.all('SELECT * FROM simulation_runs') == []
    store.execute('UPDATE enrollment_sessions SET revoked_at=? WHERE id=?', (now(), sid))
    app.state.enrollment_learning.revoke(sid)
    assert tool(linked, 'after-revocation', 'start_simulation', {'scenario_id': 'purpose-01'}) == []
    assert store.all('SELECT * FROM simulation_runs') == []


def test_original_enrollment_revocation_during_generation_blocks_save_before_mirror_hook(linked):
    _, app, store, _, _, sid, call = linked
    binding = app.state.enrollment_learning.ensure_binding(sid)
    class WithdrawDuringResponse:
        name = 'synthetic-local-provider'
        def respond(self, context, messages):
            store.execute('UPDATE enrollment_sessions SET revoked_at=? WHERE id=?', (now(), sid))
            return 'This generated response must not be stored.'
    app.state.simulation.provider = WithdrawDuringResponse()
    with pytest.raises(HTTPException) as exc:
        app.state.simulation.start(binding['session_id'], 'purpose-01')
    assert exc.value.status_code == 409
    assert store.one('SELECT withdrawn_at FROM consents WHERE id=?', (binding['consent_id'],))['withdrawn_at'] is None
    assert store.all('SELECT * FROM simulation_runs') == []


def test_personal_runtime_prompt_is_bounded_and_distinguishes_voice_and_behavior():
    prompt = personal_runtime_prompt({'profile': {'name': 'Private owner name', 'dialect': 'Arabic'},
        'profile_version_id': 'synthetic-v1', 'personal_rules': [], 'domain_policy': []},
        {'action': 'introduce_variation', 'question': 'How do you respond?'})
    assert 'Private owner name' not in prompt
    assert 'synthetic-v1' in prompt
    assert 'next_probe' in prompt
    assert 'interviewer voice' in prompt
    preview = personal_runtime_prompt({'profile': {}, 'personal_rules': []}, mode='simulation', runtime='private_preview')
    assert 'approved synthetic voice' in preview
    assert 'start_simulation' not in preview
    with pytest.raises(HTTPException) as exc:
        personal_runtime_prompt({'profile': {}, 'personal_rules': [{'value': 'x' * 29000}]})
    assert exc.value.status_code == 409


def test_composed_lifespan_recovers_enrollment_operations_and_stops_sideband(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    owner = store.create_user('Synthetic owner', 'admin')
    sid = 'synthetic-recovery'
    store.execute('INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated) VALUES(?,?,?,?,?,?,?)',
        (sid, owner['id'], 'collecting', 'voice-enrollment-v1', '{}', now(), now()))
    store.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,NULL,?,?,?)',
        ('synthetic-op', sid, 'voice_clone', 'operation', 'dispatching', '{}', now(), now()))
    stopped = []
    async def stop_all():
        stopped.append(True)
    app.state.enrollment_sideband.stop_all = stop_all
    with TestClient(app):
        assert store.one('SELECT state FROM enrollment_operations WHERE id=?', ('synthetic-op',))['state'] == 'outcome_unknown'
        assert hasattr(app.state, 'learning_jobs')
    assert stopped == [True]
