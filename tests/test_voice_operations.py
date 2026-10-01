"""Synthetic receipts only; no paid provider calls or physical microphone claims."""
import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException

from studio.migrations import migrate
from studio.providers import ProviderError
from test_voice import voice, reviewed, stamp, blocked


def unknown_candidate(env, key='uncertain'):
    s, e, p, pid, u, o, c = env
    reviewed(env)
    original = p.clone
    def uncertain(name, files):
        original(name, files)
        raise ProviderError(503, 'provider_timeout', 'SECRET_KEY', uncertain=True)
    p.clone = uncertain
    return e.create_candidate(pid, u['id'], idempotency_key=key), original


def test_unknown_clone_does_not_retry_same_or_different_key(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    assert failed['error']['code'] == 'voice_clone_outcome_unknown'
    assert 'SECRET' not in json.dumps(failed)
    p.clone = original
    assert e.create_candidate(pid, u['id'], idempotency_key='uncertain')['status'] == 'failed'
    blocked(lambda: e.create_candidate(pid, u['id'], idempotency_key='different'))
    assert len(p.clones) == 1
    op = e.list_clone_operations(pid)[0]
    assert op['state'] == 'outcome_unknown'
    assert op['provider_name'] == p.clones[0][0]


def test_checked_absence_allows_one_explicit_new_attempt(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    blocked(lambda: e.reconcile_clone(failed['id'], o['id'], confirmed_not_created=True, notes='Provider account checked.'), 403)
    blocked(lambda: e.reconcile_clone(failed['id'], u['id'], confirmed_not_created=True), 422)
    result = e.reconcile_clone(failed['id'], u['id'], confirmed_not_created=True, notes='Provider account checked: no tagged voice exists.')
    assert result['state'] == 'reconciled_not_created'
    p.clone = original
    ready = e.create_candidate(pid, u['id'], idempotency_key='uncertain')
    assert ready['id'] == failed['id']
    assert ready['status'] == 'ready'
    assert len(p.clones) == 2
    assert [op['state'] for op in e.list_clone_operations(pid)] == ['reconciled_not_created', 'succeeded']


def test_confirmed_rejection_retries_are_bounded(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    p.fail_clone = True
    for _ in range(3):
        assert e.create_candidate(pid, u['id'], idempotency_key='bounded-rejection')['status'] == 'failed'
    fourth = e.create_candidate(pid, u['id'], idempotency_key='bounded-rejection')
    assert fourth['status'] == 'failed'
    assert len(p.clones) == 3
    assert len(e.list_clone_operations(pid)) == 3


def test_found_clone_requires_matching_provider_adapter_and_is_reused(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    blocked(lambda: e.reconcile_clone(failed['id'], u['id'], provider_voice_id='remote-1', notes='Found tagged voice in account.'))
    def verify(remote, expected_name):
        assert remote == 'remote-1'
        assert expected_name == p.clones[0][0]
    p.reconcile_created_voice = verify
    result = e.reconcile_clone(failed['id'], u['id'], provider_voice_id='remote-1', notes='Provider GET matched the private operation name and verified owner.')
    assert result['status'] == 'failed'  # Preview and contributor approval remain separate.
    assert result['provider_voice_id'] == 'remote-1'
    assert e.active_voice(pid) is None
    p.clone = original
    ready = e.create_candidate(pid, u['id'], idempotency_key='uncertain')
    assert ready['status'] == 'ready'
    assert ready['provider_voice_id'] == 'remote-1'
    assert len(p.clones) == 1


def test_found_stock_or_mismatched_voice_cannot_be_adopted(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    def mismatch(remote, expected_name):
        raise ProviderError(502, 'voice_reconciliation_mismatch', 'Resource mismatch.')
    p.reconcile_created_voice = mismatch
    with pytest.raises(ProviderError):
        e.reconcile_clone(failed['id'], u['id'], provider_voice_id='stock-voice', notes='Operator found a voice but its identity did not match.')
    assert e.get_version(failed['id'])['provider_voice_id'] is None
    assert e.list_clone_operations(pid)[0]['state'] == 'outcome_unknown'


def test_restart_after_clone_dispatch_blocks_recreation(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'], idempotency_key='restart')
    # Simulate the durable cut just after external creation, before its receipt.
    s.execute("UPDATE voice_versions SET status='building',provider_voice_id=NULL WHERE id=?", (candidate['id'],))
    s.execute("UPDATE voice_provider_operations SET state='dispatching',provider_voice_id=NULL WHERE voice_version_id=?", (candidate['id'],))
    s.execute('DELETE FROM voice_cleanup_jobs WHERE id=?', (candidate['id'],))
    e.recover_pending_clones()
    failed = e.create_candidate(pid, u['id'], idempotency_key='restart')
    assert failed['status'] == 'failed'
    assert failed['error']['code'] == 'voice_clone_outcome_unknown'
    assert len(p.clones) == 1


def test_stale_build_is_not_permission_to_repeat_creation(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    s.execute("UPDATE voice_versions SET status='building',updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (failed['id'],))
    s.execute("UPDATE voice_provider_operations SET state='dispatching' WHERE voice_version_id=?", (failed['id'],))
    assert e.create_candidate(pid, u['id'], idempotency_key='uncertain')['error']['code'] == 'voice_clone_outcome_unknown'
    assert len(p.clones) == 1


def test_pre_dispatch_interruption_can_resume_without_uncertain_creation(voice):
    s, e, p, pid, u, o, c = voice
    sample, audio = reviewed(voice)
    path = s.audio_dir / (audio + '.wav')
    original_bytes = path.read_bytes()
    path.write_bytes(b'changed synthetic fixture')
    failed = e.create_candidate(pid, u['id'], idempotency_key='pre-dispatch')
    assert failed['status'] == 'failed'
    assert e.list_clone_operations(pid)[0]['state'] == 'prepared'
    assert p.clones == []
    path.write_bytes(original_bytes)
    s.execute("UPDATE voice_versions SET status='building' WHERE id=?", (failed['id'],))
    e.recover_pending_clones()
    ready = e.create_candidate(pid, u['id'], idempotency_key='pre-dispatch')
    assert ready['status'] == 'ready'
    assert len(p.clones) == 1


def test_additive_upgrade_preserves_legacy_candidate_and_blocks_untracked_retry(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    p.fail_clone = True
    failed = e.create_candidate(pid, u['id'], idempotency_key='legacy')
    with s.db() as db:
        db.execute('DROP TABLE voice_provider_operations')
        db.execute("DELETE FROM schema_migrations WHERE name='007_voice_operations.sql'")
    migrate(s)
    p.fail_clone = False
    blocked_candidate = e.create_candidate(pid, u['id'], idempotency_key='legacy')
    assert blocked_candidate['id'] == failed['id']
    assert blocked_candidate['error']['code'] == 'voice_clone_outcome_unknown'
    assert len(p.clones) == 1
    assert len(s.all('SELECT * FROM voice_samples WHERE profile_id=?', (pid,))) == 1


def test_uncertain_operation_custody_survives_local_deletion(voice):
    s, e, p, pid, u, o, c = voice
    failed, original = unknown_candidate(voice)
    result = e.delete_external_for_profile(pid)
    assert result[0]['status'] == 'external_reconciliation_required'
    with s.db() as db:
        for table in ('conversation_turns', 'teaching_sessions', 'provider_authorizations', 'audio', 'consents', 'profiles'):
            db.execute('DELETE FROM ' + table + ' WHERE ' + ('id' if table == 'profiles' else 'profile_id') + '=?', (pid,))
    op = s.one('SELECT * FROM voice_provider_operations WHERE voice_version_id=?', (failed['id'],))
    assert op['state'] == 'outcome_unknown'
    p.reconcile_created_voice = lambda remote, name: None
    cleanup = e.reconcile_clone(failed['id'], u['id'], provider_voice_id='remote-1', notes='Found operation-tagged clone after local deletion; remove it.')
    assert cleanup['state'] == 'cleanup_required'
    assert p.deleted == ['remote-1']


@pytest.fixture
def ready_enrollment(voice, monkeypatch):
    s, e, p, pid, u, o, c = voice
    monkeypatch.setenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '1')
    enrollment_id, version_id = uuid.uuid4().hex, uuid.uuid4().hex
    remote, created = 'enrollment-voice-1', stamp()
    root = s.root / 'enrollments' / enrollment_id
    root.mkdir(parents=True)
    chunk_path = root / 'chunk-0.wav'
    chunk_path.write_bytes(b'SYNTHETIC_MICROPHONE_FIXTURE')
    digest = hashlib.sha256(chunk_path.read_bytes()).hexdigest()
    source = dict(seq=0, sha256=digest, byte_count=chunk_path.stat().st_size)
    manifest = dict(provider='elevenlabs', chunks=[source], total_ms=61000, speaker_verified=False, human_listening_required=True)
    serial = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    manifest_digest = hashlib.sha256(serial.encode()).hexdigest()
    scopes = {k: True for k in ('recording', 'external_processing', 'voice_cloning', 'private_preview', 'self_attestation')}
    s.execute('INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,voice_id,voice_state,created,updated) VALUES(?,?,?,\'voice-enrollment-v1\',?,?,\'ready\',?,?)', (enrollment_id, u['id'], 'ready', json.dumps(scopes), remote, created, created))
    s.execute('INSERT INTO enrollment_chunks VALUES(?,?,?,?,?,?,?,?,?)', (enrollment_id, 0, digest, chunk_path.stat().st_size, 61000, 'audio/wav', 'contributor', str(chunk_path), created))
    s.execute('INSERT INTO enrollment_voice_versions VALUES(?,?,?,?,?,?,?,?,?,?)', (version_id, enrollment_id, 1, 'elevenlabs', remote, 'ready', serial, manifest_digest, created, created))
    detail = dict(voice_version_id=version_id, manifest_digest=manifest_digest, requires_verification=False)
    s.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, enrollment_id, 'voice_clone', 'ivc-v1:' + manifest_digest, 'succeeded', remote, json.dumps(detail), created, created))
    (root / 'previews').mkdir()
    for kind in ('question', 'number', 'correction'):
        raw = b'ID3_SYNTHETIC_' + kind.encode()
        (root / 'previews' / (remote + '-' + kind + '.mp3')).write_bytes(raw)
        detail = dict(kind=kind, sha256=hashlib.sha256(raw).hexdigest(), quality={'duration_ms': 1000})
        s.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, enrollment_id, 'voice_preview', remote + ':' + kind, 'succeeded', remote, json.dumps(detail), created, created))
    s.execute('INSERT INTO enrollment_voice_approvals VALUES(?,?,?)', (enrollment_id, remote, created))
    return voice, enrollment_id, version_id, remote, root


def test_approved_enrollment_adapter_reuses_existing_voice_without_promoting_research(ready_enrollment):
    env, enrollment, version, remote, root = ready_enrollment
    s, e, p, pid, u, o, c = env
    result = e.resolve_approved_enrollment_voice(enrollment, u['id'])
    assert result['source'] == 'enrollment'
    assert result['enrollment_voice_version_id'] == version
    assert result['provider_voice_id'] == remote
    assert e.active_voice(pid) is None
    assert e.list_versions(pid) == []
    assert e.list_samples(pid) == []
    assert p.clones == p.speeches == []


def test_enrollment_adapter_owner_and_default_flag(ready_enrollment, monkeypatch):
    env, enrollment, version, remote, root = ready_enrollment
    s, e, p, pid, u, o, c = env
    blocked(lambda: e.resolve_approved_enrollment_voice(enrollment, o['id']), 403)
    monkeypatch.delenv('RANEEN_VOICE_ENROLLMENT_ENABLED')
    blocked(lambda: e.resolve_approved_enrollment_voice(enrollment, u['id']), 404)


@pytest.mark.parametrize('mutation', ['revoked', 'research_scope', 'verification', 'missing_approval', 'wrong_voice', 'preview_changed', 'preview_missing', 'source_changed', 'other_speaker', 'unknown_create', 'cleanup'])
def test_enrollment_adapter_requires_actual_approved_consented_preserved_source(ready_enrollment, mutation):
    env, enrollment, version, remote, root = ready_enrollment
    s, e, p, pid, u, o, c = env
    if mutation == 'revoked':
        s.execute('UPDATE enrollment_sessions SET revoked_at=? WHERE id=?', (stamp(), enrollment))
    elif mutation == 'research_scope':
        s.execute("UPDATE enrollment_sessions SET consent_version='internal-research-v0.1' WHERE id=?", (enrollment,))
    elif mutation == 'verification':
        s.execute("UPDATE enrollment_sessions SET voice_state='verification_required' WHERE id=?", (enrollment,))
    elif mutation == 'missing_approval':
        s.execute('DELETE FROM enrollment_voice_approvals WHERE session_id=?', (enrollment,))
    elif mutation == 'wrong_voice':
        s.execute("UPDATE enrollment_voice_versions SET provider_voice_id='other-voice' WHERE id=?", (version,))
    elif mutation == 'preview_changed':
        (root / 'previews' / (remote + '-question.mp3')).write_bytes(b'changed')
    elif mutation == 'preview_missing':
        (root / 'previews' / (remote + '-question.mp3')).unlink()
    elif mutation == 'source_changed':
        (root / 'chunk-0.wav').write_bytes(b'changed')
    elif mutation == 'other_speaker':
        s.execute("UPDATE enrollment_chunks SET role='interviewer' WHERE session_id=?", (enrollment,))
    elif mutation == 'unknown_create':
        s.execute("UPDATE enrollment_operations SET state='outcome_unknown' WHERE session_id=? AND kind='voice_clone'", (enrollment,))
    elif mutation == 'cleanup':
        created = stamp()
        s.execute('INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, enrollment, 'cleanup_voice', remote, 'retryable', remote, '{}', created, created))
    blocked(lambda: e.resolve_approved_enrollment_voice(enrollment, u['id']), 410 if mutation == 'revoked' else 409)
    assert p.clones == p.speeches == []
