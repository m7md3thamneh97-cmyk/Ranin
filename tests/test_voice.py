import hashlib
import io
import json
import uuid
import wave
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from studio.app import CONSENT_TEXT, CONSENT_VERSION, Store
from studio.migrations import migrate
from studio.voice import PREVIEW_TEXT, VoiceEngine
from studio.providers import ProviderError


def stamp():
    return datetime.now(timezone.utc).isoformat()


class FakeVoice:
    def __init__(self):
        self.clones = []
        self.speeches = []
        self.fail_clone = False
        self.fail_tts = False
        self.deleted = []

    def clone(self, name, samples):
        self.clones.append((name, samples))
        if self.fail_clone:
            raise ProviderError(503, 'provider_authentication_failed', 'SECRET_PROVIDER_RESPONSE', upstream_status=401)
        return 'remote-' + str(len(self.clones))

    def synthesize(self, ident, text):
        self.speeches.append((ident, text))
        if self.fail_tts:
            raise RuntimeError('SECRET_PROVIDER_RESPONSE')
        return b'ID3fixed-preview'

    def delete(self, ident):
        self.deleted.append(ident)


@pytest.fixture
def voice(tmp_path):
    store = Store(tmp_path)
    migrate(store)
    # The integrated runtime installs enrollment tables after core migrations.
    # Use its actual additive schema so profile deletion exercises bridge FKs.
    from studio.enrollment import SCHEMA as enrollment_schema
    with store.db() as db:
        db.executescript(enrollment_schema)
    user = store.create_user('Trainer')
    stranger = store.create_user('Stranger')
    profile_id = uuid.uuid4().hex
    store.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)', (profile_id, user['id'], 'Trainer', 'Emirati Arabic', 'both', '', stamp()))
    consent_id = add_consent(store, profile_id)
    add_authorization(store, profile_id, consent_id)
    provider = FakeVoice()
    return store, VoiceEngine(store, provider), provider, profile_id, user, stranger, consent_id


def add_consent(store, profile_id, voice_export=True):
    ident = uuid.uuid4().hex
    store.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,NULL)', (ident, profile_id, CONSENT_VERSION, 1, 1, int(voice_export), CONSENT_TEXT, stamp()))
    return ident


def add_authorization(store, profile_id, consent_id, *, provider='elevenlabs', scope='voice_clone'):
    ident = uuid.uuid4().hex
    store.execute('INSERT INTO provider_authorizations(id,profile_id,consent_id,provider,scope,self_attestation,created_at) VALUES(?,?,?,?,?,1,?)', (ident, profile_id, consent_id, provider, scope, stamp()))
    return ident


def add_turn(env, *, seconds=61, seed=1, flags=None, role='trainer', split='train', mode='teaching',
             consent=None, raw=None, transcript='هذه كلمات المتدرب.', audio_on_turn=True, metadata=None):
    store, engine, provider, profile_id, user, stranger, source_consent = env
    consent = consent or source_consent
    session_id, audio_id, turn_id = [uuid.uuid4().hex for _ in range(3)]
    created = stamp()
    store.execute('INSERT INTO teaching_sessions(id,profile_id,consent_id,split,mode,started_at,created_at) VALUES(?,?,?,?,?,?,?)', (session_id, profile_id, consent, split, mode, created, created))
    if raw is None:
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes((5000 + seed).to_bytes(2, 'little', signed=True) * int(8000 * seconds))
        raw = output.getvalue()
    stats = dict(duration_seconds=seconds, sample_rate=8000, flags=flags or [])
    store.execute('INSERT INTO audio VALUES(?,?,?,?,?,?,?)', (audio_id, profile_id, consent, hashlib.sha256(raw).hexdigest(), json.dumps(stats), '{}', created))
    (store.audio_dir / (audio_id + '.wav')).write_bytes(raw)
    metadata = metadata if metadata is not None else dict(capture_mode=mode, split=split)
    store.execute('INSERT INTO conversation_turns(id,session_id,profile_id,consent_id,turn_index,role,transcript,transcript_state,audio_id,provider_metadata_json,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,\'final\',?,?,?,?,?)',
                  (turn_id, session_id, profile_id, consent, 0, role, transcript, audio_id if audio_on_turn else None, json.dumps(metadata), created, created, created))
    return turn_id, audio_id, session_id


def reviewed(env, **kwargs):
    store, engine, provider, profile_id, user, stranger, consent_id = env
    turn_id, audio_id, session_id = add_turn(env, **kwargs)
    sample = engine.ingest_turn(turn_id)
    return engine.review_sample(sample['id'], user['id'], trainer_only=True, clean_speech=True), audio_id


def blocked(call, code=409):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == code


def test_auto_sample_remains_pending_until_owner_review(voice):
    s, e, p, pid, u, o, c = voice
    turn, _, _ = add_turn(voice)
    sample = e.ingest_turn(turn)
    assert sample['eligibility'] == 'pending'
    assert sample['quality']['signal_only'] is True
    assert sample['quality']['speech_confirmed'] is False
    assert e.ingest_turn(turn)['id'] == sample['id']
    blocked(lambda: e.create_candidate(pid, u['id']))
    assert p.clones == []


def test_single_speaker_and_clean_speech_are_separate_gates(voice):
    s, e, p, pid, u, o, c = voice
    turn, _, _ = add_turn(voice)
    sample = e.ingest_turn(turn)
    result = e.review_sample(sample['id'], u['id'], trainer_only=True, clean_speech=False)
    assert result['eligibility'] == 'rejected'
    blocked(lambda: e.create_candidate(pid, u['id']))
    result = e.review_sample(sample['id'], u['id'], trainer_only=False, clean_speech=True)
    assert result['eligibility'] == 'rejected'


def test_voice_owner_controls_review_candidate_and_approval(voice):
    s, e, p, pid, u, o, c = voice
    sample, _ = reviewed(voice)
    blocked(lambda: e.review_sample(sample['id'], o['id'], trainer_only=True, clean_speech=True), 403)
    blocked(lambda: e.create_candidate(pid, o['id']), 403)
    candidate = e.create_candidate(pid, u['id'])
    blocked(lambda: e.approve(candidate['id'], o['id']), 403)


def test_ready_clone_fixed_preview_and_explicit_approval(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'])
    assert candidate['status'] == 'ready'
    assert candidate['version_number'] == 1
    assert p.speeches == [(candidate['provider_voice_id'], PREVIEW_TEXT)]
    assert e.active_voice(pid) is None
    assert e.preview(candidate['id']) == b'ID3fixed-preview'
    approved = e.approve(candidate['id'], u['id'])
    assert approved['status'] == 'approved'
    assert e.active_voice(pid)['id'] == candidate['id']
    assert e.synthesize(candidate['id'], 'معاينة المحاكاة') == b'ID3fixed-preview'


def test_minimum_duration_and_deduplication(voice):
    s, e, p, pid, u, o, c = voice
    _, audio = reviewed(voice, seconds=31)
    raw = (s.audio_dir / (audio + '.wav')).read_bytes()
    turn, _, _ = add_turn(voice, seconds=31, raw=raw)
    duplicate = e.ingest_turn(turn)
    assert duplicate['eligibility'] == 'rejected'
    blocked(lambda: e.review_sample(duplicate['id'], u['id'], trainer_only=True, clean_speech=True))
    blocked(lambda: e.create_candidate(pid, u['id']))
    reviewed(voice, seconds=31, seed=2)
    assert e.create_candidate(pid, u['id'])['source_manifest']['duration_seconds'] == 62
    with pytest.raises(ValueError):
        VoiceEngine(s, p, minimum_seconds=59)


def test_no_sample_for_assistant_speech_and_signal_failure_not_overridable(voice):
    s, e, p, pid, u, o, c = voice
    turn, _, _ = add_turn(voice, role='raneen')
    assert e.ingest_turn(turn) is None
    turn, _, _ = add_turn(voice, flags=['possible_clipping'], seed=2)
    bad = e.ingest_turn(turn)
    assert bad['eligibility'] == 'rejected'
    blocked(lambda: e.review_sample(bad['id'], u['id'], trainer_only=True, clean_speech=True))


def test_holdout_turn_and_exact_copy_cannot_enter_voice_training(voice):
    s, e, p, pid, u, o, c = voice
    turn, audio, _ = add_turn(voice, split='holdout')
    assert e.ingest_turn(turn)['eligibility'] == 'rejected'
    raw = (s.audio_dir / (audio + '.wav')).read_bytes()
    turn, _, _ = add_turn(voice, raw=raw)
    assert e.ingest_turn(turn)['eligibility'] == 'rejected'
    assert p.clones == []


def test_holdout_example_audio_hash_blocks_new_turn_copy(voice):
    s, e, p, pid, u, o, c = voice
    turn, audio, _ = add_turn(voice)
    payload = dict(scenario_id='holdout-01', audio_id=audio)
    s.execute('INSERT INTO examples(id,profile_id,consent_id,payload,created) VALUES(?,?,?,?,?)', (uuid.uuid4().hex, pid, c, json.dumps(payload), stamp()))
    raw = (s.audio_dir / (audio + '.wav')).read_bytes()
    copied, _, _ = add_turn(voice, raw=raw)
    assert e.ingest_turn(copied)['eligibility'] == 'rejected'


def test_provider_permission_is_separate_from_voice_export(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    s.execute('DELETE FROM provider_authorizations WHERE profile_id=?', (pid,))
    add_authorization(s, pid, c, provider='openai', scope='text_learning')
    blocked(lambda: e.create_candidate(pid, u['id']))
    assert p.clones == []


def test_source_scope_not_retroactive_and_source_authorization_is_required(voice):
    s, e, p, pid, u, o, c = voice
    s.execute('UPDATE consents SET voice_export=0 WHERE id=?', (c,))
    turn, _, _ = add_turn(voice)
    newer = add_consent(s, pid)
    add_authorization(s, pid, newer)
    sample = e.ingest_turn(turn)
    assert sample['eligibility'] == 'rejected'
    blocked(lambda: e.review_sample(sample['id'], u['id'], trainer_only=True, clean_speech=True))
    blocked(lambda: e.create_candidate(pid, u['id']))


def test_new_current_consent_requires_new_provider_authorization(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    newer = add_consent(s, pid)
    blocked(lambda: e.create_candidate(pid, u['id']))
    add_authorization(s, pid, newer)
    assert e.create_candidate(pid, u['id'])['status'] == 'ready'


def test_withdrawal_blocks_preview_approval_and_simulation_even_after_reconsent(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'])
    e.approve(candidate['id'], u['id'])
    s.execute('UPDATE consents SET withdrawn_at=? WHERE id=?', (stamp(), c))
    newer = add_consent(s, pid)
    add_authorization(s, pid, newer)
    blocked(lambda: e.preview(candidate['id']))
    blocked(lambda: e.approve(candidate['id'], u['id']))
    blocked(lambda: e.active_voice(pid))
    blocked(lambda: e.synthesize(candidate['id'], 'blocked'))


def test_provider_authorization_withdrawal_does_not_reenable_old_voice(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'])
    e.approve(candidate['id'], u['id'])
    s.execute('UPDATE provider_authorizations SET withdrawn_at=? WHERE profile_id=?', (stamp(), pid))
    add_authorization(s, pid, c)
    blocked(lambda: e.preview(candidate['id']))
    blocked(lambda: e.active_voice(pid))


def test_failed_upgrade_preserves_approved_and_resumes_same_candidate(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    approved = e.approve(e.create_candidate(pid, u['id'])['id'], u['id'])
    reviewed(voice, seed=2)
    p.fail_clone = True
    failed = e.create_candidate(pid, u['id'], idempotency_key='upgrade')
    assert failed['status'] == 'failed'
    assert 'SECRET' not in json.dumps(failed)
    assert e.active_voice(pid)['id'] == approved['id']
    p.fail_clone = False
    ready = e.create_candidate(pid, u['id'], idempotency_key='upgrade')
    assert ready['id'] == failed['id']
    assert ready['status'] == 'ready'
    assert ready['version_number'] == 2
    assert e.active_voice(pid)['id'] == approved['id']
    e.reject(ready['id'], u['id'], 'Needs more data')
    assert e.active_voice(pid)['id'] == approved['id']
    blocked(lambda: e.reject(approved['id'], u['id']))


def test_tts_failure_retains_remote_id_retry_does_not_clone_twice(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    p.fail_tts = True
    failed = e.create_candidate(pid, u['id'], idempotency_key='tts-retry')
    assert failed['status'] == 'failed'
    assert failed['provider_voice_id'] == 'remote-1'
    p.fail_tts = False
    ready = e.create_candidate(pid, u['id'], idempotency_key='tts-retry')
    assert ready['id'] == failed['id']
    assert ready['status'] == 'ready'
    assert len(p.clones) == 1
    assert e.create_candidate(pid, u['id'], idempotency_key='tts-retry')['id'] == ready['id']
    assert len(p.clones) == 1


def test_changed_original_bytes_blocks_upload(voice):
    s, e, p, pid, u, o, c = voice
    _, audio = reviewed(voice)
    (s.audio_dir / (audio + '.wav')).write_bytes(b'changed')
    failed = e.create_candidate(pid, u['id'])
    assert failed['status'] == 'failed'
    assert failed['error']['code'] == 'source_blocked'
    assert p.clones == []


def test_external_delete_reports_cleanup_and_removes_local_previews(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'])
    assert e.delete_external_for_profile(pid) == [{'id': candidate['id'], 'status': 'deleted'}]
    assert p.deleted == [candidate['provider_voice_id']]
    assert not e.get_version(candidate['id'])['preview_available']


def test_real_unconfigured_provider_never_fakes_clone(voice):
    from studio.providers import ElevenLabsVoiceProvider
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    engine = VoiceEngine(s, ElevenLabsVoiceProvider(api_key=''))
    candidate = engine.create_candidate(pid, u['id'])
    assert candidate['status'] == 'failed'
    assert candidate['provider_voice_id'] is None
    assert engine.active_voice(pid) is None


def test_teaching_capture_survives_switch_to_simulation(voice):
    s, e, p, pid, u, o, c = voice
    turn, audio, session = add_turn(voice, metadata={'capture_mode': 'teaching', 'split': 'train'})
    s.execute("UPDATE teaching_sessions SET mode='simulation' WHERE id=?", (session,))
    sample = e.ingest_turn(turn)
    assert sample['eligibility'] == 'pending'
    e.review_sample(sample['id'], u['id'], trainer_only=True, clean_speech=True)
    assert e.create_candidate(pid, u['id'])['status'] == 'ready'
    simulated, _, session = add_turn(voice, seed=2, mode='simulation')
    assert e.ingest_turn(simulated)['eligibility'] == 'rejected'


def test_remote_cleanup_custody_survives_local_profile_deletion(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    candidate = e.create_candidate(pid, u['id'])
    def failing_delete(ident):
        raise RuntimeError('Provider outage')
    p.delete = failing_delete
    assert e.delete_external_for_profile(pid) == [{'id': candidate['id'], 'status': 'external_deletion_failed'}]
    job = s.one('SELECT * FROM voice_cleanup_jobs WHERE id=?', (candidate['id'],))
    assert job['status'] == 'pending'
    assert job['provider_voice_id'] == candidate['provider_voice_id']
    with s.db() as db:
        for table in ('conversation_turns', 'teaching_sessions', 'provider_authorizations', 'audio', 'consents', 'profiles'):
            db.execute('DELETE FROM ' + table + ' WHERE ' + ('id' if table == 'profiles' else 'profile_id') + '=?', (pid,))
    assert s.one('SELECT * FROM voice_versions WHERE id=?', (candidate['id'],)) is None
    p.delete = lambda ident: p.deleted.append(ident)
    assert e.retry_external_cleanup(pid) == [{'id': candidate['id'], 'status': 'deleted'}]
    assert p.deleted == [candidate['provider_voice_id']]


def test_verification_failure_retains_remote_custody_and_is_not_ready(voice):
    from studio.providers import ProviderError
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    def needs_verification(name, samples):
        error = ProviderError(409, 'voice_verification_required', 'Verify with the provider.')
        error.provider_voice_id = 'remote-verification'
        raise error
    p.clone = needs_verification
    failed = e.create_candidate(pid, u['id'])
    assert failed['status'] == 'failed'
    assert failed['provider_voice_id'] == 'remote-verification'
    assert e.active_voice(pid) is None
    assert p.speeches == []
    assert s.one('SELECT provider_voice_id FROM voice_cleanup_jobs WHERE id=?', (failed['id'],))['provider_voice_id'] == 'remote-verification'
    retry = e.create_candidate(pid, u['id'])
    assert retry['id'] == failed['id']
    assert retry['status'] == 'failed'
    assert p.speeches == []
    p.verify_ready = lambda ident: None  # Adapter independently confirms provider verification.
    retry = e.create_candidate(pid, u['id'])
    assert retry['id'] == failed['id']
    assert retry['status'] == 'ready'
    assert retry['provider_voice_id'] == 'remote-verification'


def test_consent_revoked_during_clone_is_not_previewed_and_remote_is_removed(voice):
    s, e, p, pid, u, o, c = voice
    reviewed(voice)
    original = p.clone
    def revoke_after_upload(name, samples):
        remote = original(name, samples)
        s.execute('UPDATE consents SET withdrawn_at=? WHERE id=?', (stamp(), c))
        return remote
    p.clone = revoke_after_upload
    failed = e.create_candidate(pid, u['id'])
    assert failed['status'] == 'failed'
    assert failed['provider_voice_id'] is None
    assert failed['error']['code'] == 'source_blocked'
    assert p.speeches == []
    assert p.deleted == ['remote-1']


def test_append_only_audio_attachment_is_eligible_without_turn_mutation(voice):
    s, e, p, pid, u, o, c = voice
    turn, audio, _ = add_turn(voice, audio_on_turn=False)
    assert e.ingest_turn(turn) is None
    s.execute('INSERT INTO turn_audio_attachments(id,profile_id,turn_id,audio_id,consent_id,created_at) VALUES(?,?,?,?,?,?)', (uuid.uuid4().hex, pid, turn, audio, c, stamp()))
    sample = e.ingest_turn(turn)
    assert sample['audio_id'] == audio
    assert sample['eligibility'] == 'pending'
    e.review_sample(sample['id'], u['id'], trainer_only=True, clean_speech=True)
    candidate = e.create_candidate(pid, u['id'])
    assert candidate['status'] == 'ready'
    assert s.one('SELECT audio_id FROM conversation_turns WHERE id=?', (turn,))['audio_id'] is None


def test_holdout_attachment_hash_blocks_copied_training_audio(voice):
    s, e, p, pid, u, o, c = voice
    turn, audio, _ = add_turn(voice, split='holdout', audio_on_turn=False)
    s.execute('INSERT INTO turn_audio_attachments(id,profile_id,turn_id,audio_id,consent_id,created_at) VALUES(?,?,?,?,?,?)', (uuid.uuid4().hex, pid, turn, audio, c, stamp()))
    raw = (s.audio_dir / (audio + '.wav')).read_bytes()
    copied, _, _ = add_turn(voice, raw=raw)
    assert e.ingest_turn(copied)['eligibility'] == 'rejected'


def test_progressive_candidates_select_recent_bounded_speech(voice):
    s, e, p, pid, u, o, c = voice
    _, oldest = reviewed(voice, seed=1)
    _, middle = reviewed(voice, seed=2)
    _, newest = reviewed(voice, seed=3)
    candidate = e.create_candidate(pid, u['id'])
    assert candidate['status'] == 'ready'
    assert {v['audio_id'] for v in candidate['source_manifest']['samples']} == {middle, newest}
    assert oldest not in {v['audio_id'] for v in candidate['source_manifest']['samples']}
    assert len(p.clones[0][1]) == 2


def test_many_short_samples_respect_provider_sample_limit(voice):
    s, e, p, pid, u, o, c = voice
    for seed in range(31):
        reviewed(voice, seed=seed, seconds=2)
    candidate = e.create_candidate(pid, u['id'])
    assert candidate['status'] == 'ready'
    assert len(candidate['source_manifest']['samples']) == 30
    assert candidate['source_manifest']['duration_seconds'] == 60
