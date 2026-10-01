"""Progressive, consent-gated voice versions backed by original Studio audio.

Signal analysis proposes samples; it never establishes who spoke or proves speech.
Only the profile owner can confirm trainer-only clean speech and approve a clone.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from .providers import ProviderError
from .scenarios import BY_ID

PREVIEW_TEXT = (
    'مرحباً، أنا رنين، مساعد ذكاء اصطناعي. هذه معاينة للصوت الذي اخترته. '
    'خلنا نفهم هدفك أول، وبعدها نتكلم عن الخيارات المناسبة لك.'
)
PROVIDER = 'elevenlabs'
STALE_BUILD_SECONDS = 600
MAX_PROVIDER_SAMPLES = 30
MAX_PROVIDER_AUDIO_BYTES = 20 * 1024 * 1024
MAX_CLONE_ATTEMPTS = 3
UNKNOWN_CLONE_ERROR = dict(code='voice_clone_outcome_unknown', message='The provider creation outcome is uncertain. Reconcile it before creating another clone.')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _object(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


class VoiceEngine:
    def __init__(self, store, provider, *, minimum_seconds=60):
        if not math.isfinite(float(minimum_seconds)) or float(minimum_seconds) < 60:
            raise ValueError('Voice candidates require at least 60 reviewed seconds.')
        self.store = store
        self.provider = provider
        self.minimum_seconds = float(minimum_seconds)
        self.preview_dir = store.root / 'voice-previews'
        self.preview_dir.mkdir(exist_ok=True)

    def _profile(self, profile_id, actor_id=None):
        profile = self.store.one('SELECT * FROM profiles WHERE id=?', (profile_id,))
        if not profile:
            raise HTTPException(404, 'Profile not found.')
        if actor_id is not None and profile['owner_id'] != actor_id:
            raise HTTPException(403, 'Only the voice owner can perform this action.')
        return profile

    def _consent(self, consent_id, profile_id):
        row = self.store.one('SELECT * FROM consents WHERE id=? AND profile_id=?', (consent_id, profile_id))
        if not row or not row['collection'] or not row['voice_export'] or row['withdrawn_at']:
            raise HTTPException(409, 'Active voice scope is required for every source recording and turn.')
        return row

    def _current_consent(self, profile_id):
        row = self.store.one('SELECT * FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1', (profile_id,))
        if not row:
            raise HTTPException(409, 'Active voice consent is required.')
        return self._consent(row['id'], profile_id)

    def _authorization(self, consent_id, profile_id):
        row = self.store.one(
            'SELECT * FROM provider_authorizations WHERE profile_id=? AND consent_id=? '
            "AND provider=? AND scope='voice_clone' ORDER BY rowid DESC LIMIT 1",
            (profile_id, consent_id, PROVIDER),
        )
        if not row or not row['self_attestation'] or row['withdrawn_at']:
            raise HTTPException(409, 'Separate active ElevenLabs voice-clone authorization is required for every source consent.')
        return row

    def _turn(self, turn_id):
        row = self.store.one(
            'SELECT t.*, t.audio_id AS source_audio_id, COALESCE(t.audio_id,ta.audio_id) AS attached_audio_id, '
            'ta.consent_id AS attachment_consent_id, s.profile_id AS session_profile_id, s.consent_id AS session_consent_id, '
            's.split AS session_split, s.mode AS session_mode, s.scenario_id AS session_scenario_id FROM conversation_turns t '
            'JOIN teaching_sessions s ON s.id=t.session_id LEFT JOIN turn_audio_attachments ta ON ta.turn_id=t.id WHERE t.id=?', (turn_id,),
        )
        if not row:
            raise HTTPException(404, 'Conversation turn not found.')
        row['audio_id'] = row.pop('attached_audio_id')
        return row

    def _holdout_sha(self, sha256):
        # Inspect hashes rather than identities: uploading a holdout WAV again must
        # not turn it into a training sample, even under a different audio ID.
        for example in self.store.all('SELECT payload FROM examples'):
            payload = _object(example['payload'])
            scenario = BY_ID.get(payload.get('scenario_id'), {})
            if scenario.get('split') != 'holdout' or not payload.get('audio_id'):
                continue
            audio = self.store.one('SELECT sha256 FROM audio WHERE id=?', (payload['audio_id'],))
            if audio and audio['sha256'] == sha256:
                return True
        turns = self.store.all(
            'SELECT s.split,s.scenario_id,t.provider_metadata_json FROM conversation_turns t '
            'LEFT JOIN turn_audio_attachments ta ON ta.turn_id=t.id '
            'JOIN audio a ON a.id=COALESCE(t.audio_id,ta.audio_id) '
            'JOIN teaching_sessions s ON s.id=t.session_id WHERE a.sha256=?', (sha256,),
        )
        return any(_object(t['provider_metadata_json']).get('split', t['split']) == 'holdout'
                   or BY_ID.get(t['scenario_id'], {}).get('split') == 'holdout' for t in turns)

    def _source(self, sample):
        turn = self._turn(sample['source_turn_id'])
        audio = self.store.one('SELECT * FROM audio WHERE id=?', (sample['audio_id'],))
        if not audio or audio['profile_id'] != sample['profile_id']:
            raise HTTPException(409, 'Source recording no longer belongs to this profile.')
        if turn['session_profile_id'] != sample['profile_id'] or turn.get('profile_id', sample['profile_id']) != sample['profile_id']:
            raise HTTPException(409, 'Source turn no longer belongs to this profile.')
        if turn['audio_id'] != audio['id'] or turn['role'] != 'trainer':
            raise HTTPException(409, 'Only the original trainer recording is eligible.')
        if turn['transcript_state'] not in ('final', 'verified') or not turn['transcript'].strip():
            raise HTTPException(409, 'A finalized trainer transcript is required.')
        capture = _object(turn['provider_metadata_json'])
        if capture.get('capture_mode', turn['session_mode']) != 'teaching':
            raise HTTPException(409, 'Only trainer speech captured during teaching is eligible for a voice clone.')
        if (capture.get('split', turn['session_split']) != 'train' or BY_ID.get(turn['session_scenario_id'], {}).get('split') == 'holdout'
                or self._holdout_sha(audio['sha256'])):
            raise HTTPException(409, 'Held-out audio must not enter the voice provider.')
        consent_ids = {audio['consent_id'], turn.get('consent_id') or turn['session_consent_id']}
        if not turn['source_audio_id'] and turn['attachment_consent_id']:
            consent_ids.add(turn['attachment_consent_id'])
        for consent_id in consent_ids:
            self._consent(consent_id, sample['profile_id'])
        stats = _object(audio['stats'])
        duration = stats.get('duration_seconds')
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
            raise HTTPException(409, 'Recording has no valid signal duration.')
        if stats.get('flags') != []:
            raise HTTPException(409, 'Recording failed deterministic signal checks.')
        path = self.store.audio_dir / f"{audio['id']}.wav"
        if not path.is_file():
            raise HTTPException(409, 'Original recording is unavailable.')
        return turn, audio, stats, consent_ids

    def ingest_turn(self, turn_id, actor_id=None):
        """Automatically accumulate metadata; human review remains mandatory."""
        turn = self._turn(turn_id)
        self._profile(turn['session_profile_id'], actor_id)
        if turn['role'] != 'trainer' or not turn['audio_id'] or turn['transcript_state'] not in ('final', 'verified'):
            return None
        existing = self.store.one('SELECT * FROM voice_samples WHERE source_turn_id=?', (turn_id,))
        if existing:
            return self._sample_public(existing)
        ident = uuid.uuid4().hex
        sample = dict(id=ident, profile_id=turn['session_profile_id'], audio_id=turn['audio_id'], source_turn_id=turn_id)
        reason = None
        quality = dict(signal_only=True, speech_confirmed=False, trainer_only=False, desired_style=False)
        try:
            _, audio, stats, _ = self._source(sample)
            quality['signal'] = stats
            duplicate = self.store.one(
                'SELECT v.id FROM voice_samples v JOIN audio a ON a.id=v.audio_id '
                'WHERE v.profile_id=? AND a.sha256=? LIMIT 1', (sample['profile_id'], audio['sha256']),
            )
            if duplicate:
                reason = 'Exact audio already belongs to a voice sample.'
        except HTTPException as exc:
            reason = str(exc.detail)
        try:
            self.store.execute(
                'INSERT INTO voice_samples(id,profile_id,audio_id,source_turn_id,eligibility,rejection_reason,quality_json,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (ident, sample['profile_id'], sample['audio_id'], turn_id, 'rejected' if reason else 'pending', reason, _json(quality), _now()),
            )
        except sqlite3.IntegrityError:
            previous = self.store.one('SELECT * FROM voice_samples WHERE profile_id=? AND audio_id=?', (sample['profile_id'], sample['audio_id']))
            if previous:
                return self._sample_public(previous)
            raise
        self.store.audit(actor_id, 'voice.sample_rejected' if reason else 'voice.sample_pending', ident, {'reason': reason})
        return self._sample_public(self.store.one('SELECT * FROM voice_samples WHERE id=?', (ident,)))

    def _sample_public(self, row):
        result = dict(row)
        result['quality'] = _object(result.pop('quality_json'))
        return result

    def list_samples(self, profile_id):
        self._profile(profile_id)
        return [self._sample_public(r) for r in self.store.all('SELECT * FROM voice_samples WHERE profile_id=? ORDER BY created_at,id', (profile_id,))]

    def review_sample(self, sample_id, actor_id, *, trainer_only, clean_speech, desired_style=True, notes=''):
        row = self.store.one('SELECT * FROM voice_samples WHERE id=?', (sample_id,))
        if not row:
            raise HTTPException(404, 'Voice sample not found.')
        self._profile(row['profile_id'], actor_id)
        self._current_consent(row['profile_id'])
        _, _, stats, _ = self._source(row)
        if row['eligibility'] == 'rejected' and row['rejection_reason'] == 'Exact audio already belongs to a voice sample.':
            raise HTTPException(409, 'Duplicate audio cannot be promoted.')
        approved = trainer_only is True and clean_speech is True and desired_style is True
        quality = dict(signal_only=True, signal=stats, trainer_only=trainer_only is True,
                       speech_confirmed=clean_speech is True, desired_style=desired_style is True, review_notes=str(notes)[:2000])
        self.store.execute(
            'UPDATE voice_samples SET eligibility=?,rejection_reason=?,quality_json=?,reviewed_by=?,reviewed_at=? WHERE id=?',
            ('eligible' if approved else 'rejected', None if approved else 'Owner did not confirm clean trainer-only speech in the desired style.',
             _json(quality), actor_id, _now(), sample_id),
        )
        self.store.audit(actor_id, 'voice.sample_accepted' if approved else 'voice.sample_rejected', sample_id)
        return self._sample_public(self.store.one('SELECT * FROM voice_samples WHERE id=?', (sample_id,)))

    def _manifest(self, profile_id):
        current = self._current_consent(profile_id)
        current_auth = self._authorization(current['id'], profile_id)
        samples, seen, total, total_bytes = [], set(), 0.0, 0
        target_seconds = max(self.minimum_seconds, 120)
        for sample in self.store.all("SELECT * FROM voice_samples WHERE profile_id=? AND eligibility='eligible' ORDER BY created_at DESC,id DESC", (profile_id,)):
            quality = _object(sample['quality_json'])
            if not sample['reviewed_at'] or not all(quality.get(k) is True for k in ('trainer_only', 'speech_confirmed', 'desired_style')):
                continue
            try:
                _, audio, stats, source_consents = self._source(sample)
                authorizations = [self._authorization(c, profile_id) for c in sorted(source_consents)]
            except HTTPException:
                # Historical revoked or newly held-out samples cannot be uploaded.
                continue
            if audio['sha256'] in seen:
                continue
            recording_bytes = (self.store.audio_dir / f"{audio['id']}.wav").stat().st_size
            if total_bytes + recording_bytes > MAX_PROVIDER_AUDIO_BYTES:
                continue
            seen.add(audio['sha256'])
            total += stats['duration_seconds']
            total_bytes += recording_bytes
            samples.append(dict(sample_id=sample['id'], audio_id=audio['id'], source_turn_id=sample['source_turn_id'],
                                sha256=audio['sha256'], duration_seconds=stats['duration_seconds'],
                                consent_ids=sorted(source_consents), authorization_ids=[a['id'] for a in authorizations]))
            if len(samples) >= MAX_PROVIDER_SAMPLES or total >= target_seconds:
                break
        if total < self.minimum_seconds:
            raise HTTPException(409, f'At least {self.minimum_seconds:g} seconds of reviewed, authorized trainer speech are required.')
        return dict(schema_version='raneen-voice-manifest-v1', profile_id=profile_id, provider=PROVIDER,
                    current_consent_id=current['id'], current_authorization_id=current_auth['id'],
                    duration_seconds=round(total, 3), duration_limitation='Signal duration estimate; owner confirmed clean speech.',
                    selection_policy='Newest eligible reviewed speech up to a two-minute target, subject to provider limits.',
                    audio_bytes=total_bytes, samples=samples)

    def _validate_manifest(self, profile_id, manifest):
        self._profile(profile_id)
        current = self._current_consent(profile_id)
        self._authorization(current['id'], profile_id)
        self._consent(manifest['current_consent_id'], profile_id)
        authorization_ids = {manifest['current_authorization_id']}
        seen = set()
        for source in manifest['samples']:
            row = self.store.one('SELECT * FROM voice_samples WHERE id=? AND profile_id=?', (source['sample_id'], profile_id))
            if not row or row['eligibility'] != 'eligible':
                raise HTTPException(409, 'A source sample is no longer eligible.')
            quality = _object(row['quality_json'])
            if not row['reviewed_at'] or not all(quality.get(k) is True for k in ('trainer_only', 'speech_confirmed', 'desired_style')):
                raise HTTPException(409, 'A source sample is no longer human-confirmed.')
            _, audio, _, source_consents = self._source(row)
            path = self.store.audio_dir / f"{audio['id']}.wav"
            if hashlib.sha256(path.read_bytes()).hexdigest() != audio['sha256']:
                raise HTTPException(409, 'Original audio checksum changed; candidate blocked.')
            if (row['audio_id'] != source['audio_id'] or row['source_turn_id'] != source['source_turn_id']
                    or sorted(source_consents) != source['consent_ids']
                    or audio['sha256'] != source['sha256'] or audio['sha256'] in seen):
                raise HTTPException(409, 'Source manifest no longer matches its original audio.')
            seen.add(audio['sha256'])
            for consent_id in source_consents:
                self._authorization(consent_id, profile_id)
            authorization_ids.update(source['authorization_ids'])
        for auth_id in authorization_ids:
            auth = self.store.one('SELECT * FROM provider_authorizations WHERE id=? AND profile_id=?', (auth_id, profile_id))
            if not auth or auth['withdrawn_at'] or not auth['self_attestation'] or auth['provider'] != PROVIDER or auth['scope'] != 'voice_clone':
                raise HTTPException(409, 'Authorization for this voice version has been withdrawn.')

    def _version_public(self, row):
        result = dict(row)
        result['source_manifest'] = _object(result.pop('source_manifest_json'))
        result['error'] = _object(result.pop('error_json', None)) or None
        result['preview_available'] = result['status'] in ('ready', 'approved') and (self.preview_dir / f"{result['id']}.mp3").is_file()
        return result

    def get_version(self, voice_id):
        row = self.store.one('SELECT * FROM voice_versions WHERE id=?', (voice_id,))
        if not row:
            raise HTTPException(404, 'Voice version not found.')
        return self._version_public(row)

    def list_versions(self, profile_id):
        self._profile(profile_id)
        return [self._version_public(r) for r in self.store.all('SELECT * FROM voice_versions WHERE profile_id=? ORDER BY version_number DESC', (profile_id,))]

    def _record_remote_custody(self, voice_id, profile, remote_id, operation_id=None):
        # Provider creation can finish after concurrent local deletion. Keep the
        # returned remote identifier independently of the profile FK immediately.
        stamp = _now()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if operation_id:
                receipt = db.execute('SELECT state,provider_voice_id FROM voice_provider_operations WHERE id=?', (operation_id,)).fetchone()
                if (not receipt or (receipt['state'] not in ('dispatching', 'outcome_unknown')
                                    and not (receipt['state'] == 'succeeded' and receipt['provider_voice_id'] == remote_id))):
                    raise HTTPException(409, 'This provider operation was already reconciled to a different result.')
            custody = db.execute('SELECT provider_voice_id,status FROM voice_cleanup_jobs WHERE id=?', (voice_id,)).fetchone()
            if custody and custody['status'] != 'deleted' and custody['provider_voice_id'] != remote_id:
                raise HTTPException(409, 'A different remote resource is already recorded for this voice candidate.')
            db.execute(
                "INSERT INTO voice_cleanup_jobs(id,profile_id,owner_id,provider,provider_voice_id,status,created_at,updated_at) VALUES(?,?,?,?,?,'retained',?,?) "
                "ON CONFLICT(id) DO UPDATE SET provider_voice_id=excluded.provider_voice_id,status='retained',updated_at=excluded.updated_at WHERE voice_cleanup_jobs.status='deleted'",
                (voice_id, profile['id'], profile['owner_id'], PROVIDER, remote_id, stamp, stamp),
            )
            if operation_id:
                db.execute("UPDATE voice_provider_operations SET state='succeeded',provider_voice_id=?,updated_at=? WHERE id=?", (remote_id, stamp, operation_id))
            db.execute('UPDATE voice_versions SET provider_voice_id=?,updated_at=? WHERE id=?', (remote_id, stamp, voice_id))
            if not db.execute('SELECT id FROM profiles WHERE id=?', (profile['id'],)).fetchone():
                db.execute("UPDATE voice_cleanup_jobs SET status='pending',updated_at=? WHERE id=?", (stamp, voice_id))

    def _new_clone_operation(self, db, version, profile, manifest, state='prepared'):
        attempt = db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM voice_provider_operations WHERE voice_version_id=?', (version['id'],)).fetchone()[0]
        if attempt > MAX_CLONE_ATTEMPTS:
            raise HTTPException(429, 'The three-attempt allowance for this candidate is used.')
        ident, stamp = uuid.uuid4().hex, _now()
        name = f"Raneen {profile['id'][:8]} v{version['version_number']} op{ident[:12]}"
        db.execute('INSERT INTO voice_provider_operations(id,voice_version_id,profile_id,owner_id,provider,attempt,state,provider_name,manifest_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (ident, version['id'], profile['id'], profile['owner_id'], PROVIDER, attempt, state, name,
                    hashlib.sha256(_json(manifest).encode()).hexdigest(), stamp, stamp))
        return dict(db.execute('SELECT * FROM voice_provider_operations WHERE id=?', (ident,)).fetchone())

    def _claim_clone_operation(self, voice_id, profile, manifest):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            version = dict(db.execute('SELECT * FROM voice_versions WHERE id=?', (voice_id,)).fetchone())
            operation = db.execute('SELECT * FROM voice_provider_operations WHERE voice_version_id=? ORDER BY attempt DESC LIMIT 1', (voice_id,)).fetchone()
            if operation and operation['state'] in ('dispatching', 'outcome_unknown'):
                raise HTTPException(409, UNKNOWN_CLONE_ERROR)
            pending = db.execute("SELECT id FROM voice_provider_operations WHERE profile_id=? AND state IN ('dispatching','outcome_unknown') LIMIT 1", (profile['id'],)).fetchone()
            if pending:
                raise HTTPException(409, UNKNOWN_CLONE_ERROR)
            if not operation:
                # Older failed candidates predate durable receipts. Their absence
                # of a returned provider ID is not evidence that nothing was made.
                operation = self._new_clone_operation(db, version, profile, manifest, 'outcome_unknown')
                return operation, False
            operation = dict(operation)
            if operation['state'] == 'succeeded' and operation['provider_voice_id']:
                db.execute('UPDATE voice_versions SET provider_voice_id=? WHERE id=?', (operation['provider_voice_id'], voice_id))
                return operation, False
            if operation['state'] != 'prepared':
                operation = self._new_clone_operation(db, version, profile, manifest)
            db.execute("UPDATE voice_provider_operations SET state='dispatching',updated_at=? WHERE id=? AND state='prepared'", (_now(), operation['id']))
            operation['state'] = 'dispatching'
            return operation, True

    @staticmethod
    def _confirmed_rejection(error):
        if getattr(error, 'uncertain', False):
            return False
        if getattr(error, 'upstream_status', None) in {400, 401, 402, 403, 404, 413, 415, 422, 429}:
            return True
        return getattr(error, 'code', None) in {
            'voice_provider_not_configured', 'voice_samples_invalid', 'voice_samples_too_large',
            'provider_input_invalid', 'provider_authentication_failed', 'provider_rate_limited',
            'learning_studio_disabled', 'learning_studio_not_enabled',
        }

    def list_clone_operations(self, profile_id):
        self._profile(profile_id)
        return self.store.all('SELECT * FROM voice_provider_operations WHERE profile_id=? ORDER BY created_at,attempt', (profile_id,))

    def recover_pending_clones(self):
        """Run at process startup, never while another process is dispatching jobs."""
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            operations = db.execute("SELECT * FROM voice_provider_operations WHERE state='dispatching'").fetchall()
            for op in operations:
                db.execute("UPDATE voice_provider_operations SET state='outcome_unknown',updated_at=? WHERE id=?", (_now(), op['id']))
                db.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(UNKNOWN_CLONE_ERROR), _now(), op['voice_version_id']))
            for version in db.execute("SELECT * FROM voice_versions WHERE status='building'").fetchall():
                op = db.execute('SELECT * FROM voice_provider_operations WHERE voice_version_id=? ORDER BY attempt DESC LIMIT 1', (version['id'],)).fetchone()
                error = (dict(code='voice_build_interrupted', message='The voice preview build was interrupted; resume this candidate.')
                         if version['provider_voice_id'] or (op and op['state'] == 'prepared') else UNKNOWN_CLONE_ERROR)
                db.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(error), _now(), version['id']))

    def reconcile_clone(self, voice_id, actor_id, *, provider_voice_id=None, confirmed_not_created=False, notes=''):
        """Explicit operator reconciliation; never guess absence after a timeout.

        A found resource must match the operation's unique tagged name through the
        provider's read-only reconciliation adapter. Absence requires an explicit
        owner statement that the provider account was checked, plus evidence notes.
        """
        op = self.store.one('SELECT * FROM voice_provider_operations WHERE voice_version_id=? ORDER BY attempt DESC LIMIT 1', (voice_id,))
        if not op:
            raise HTTPException(404, 'Voice creation operation not found.')
        if op['owner_id'] != actor_id:
            raise HTTPException(403, 'Only the voice owner can reconcile this operation.')
        if op['state'] != 'outcome_unknown':
            raise HTTPException(409, 'Only an uncertain creation outcome requires reconciliation.')
        if bool(provider_voice_id) == (confirmed_not_created is True) or len(str(notes).strip()) < 8:
            raise HTTPException(422, 'Provide one checked provider result and reconciliation evidence notes.')
        stamp = _now()
        if confirmed_not_created is True:
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute("UPDATE voice_provider_operations SET state='reconciled_not_created',reconciled_at=?,reconciliation_notes=?,updated_at=? WHERE id=? AND state='outcome_unknown'", (stamp, str(notes)[:2000], stamp, op['id'])).rowcount
                if not changed:
                    raise HTTPException(409, 'This creation outcome was already reconciled.')
                db.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(dict(code='voice_clone_reconciled_absent', message='Provider account checked: no resource was created. This candidate may be explicitly retried.')), stamp, voice_id))
            self.store.audit(actor_id, 'voice.clone_reconciled_absent', voice_id, {'operation_id': op['id']})
            return dict(state='reconciled_not_created', operation_id=op['id'])
        if not isinstance(provider_voice_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', provider_voice_id):
            raise HTTPException(422, 'A valid checked provider voice identifier is required.')
        verifier = getattr(self.provider, 'reconcile_created_voice', None)
        if not callable(verifier):
            raise HTTPException(409, 'This provider cannot validate a found clone; manual reconciliation remains required.')
        version = self.store.one('SELECT * FROM voice_versions WHERE id=?', (voice_id,))
        profile = self.store.one('SELECT * FROM profiles WHERE id=?', (op['profile_id'],))
        manifest = _object(version['source_manifest_json']) if version else None
        if version and hashlib.sha256(_json(manifest).encode()).hexdigest() != op['manifest_sha256']:
            raise HTTPException(409, 'The uncertain operation no longer matches its source manifest.')
        verifier(provider_voice_id, op['provider_name'])
        self._record_remote_custody(voice_id, profile or {'id': op['profile_id'], 'owner_id': actor_id}, provider_voice_id, op['id'])
        self.store.execute('UPDATE voice_provider_operations SET reconciled_at=?,reconciliation_notes=? WHERE id=?', (stamp, str(notes)[:2000], op['id']))
        try:
            if not profile or not version:
                raise HTTPException(410, 'The local profile was deleted.')
            self._validate_manifest(op['profile_id'], manifest)
        except HTTPException:
            self.store.execute("UPDATE voice_cleanup_jobs SET status='pending',updated_at=? WHERE id=?", (_now(), voice_id))
            cleanup = self.retry_external_cleanup(op['profile_id'])
            self.store.audit(actor_id, 'voice.clone_reconciled_cleanup', voice_id, {'operation_id': op['id']})
            return dict(state='cleanup_required', operation_id=op['id'], cleanup=cleanup)
        self.store.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(dict(code='voice_preview_required', message='The found private clone is reconciled; resume this candidate to create its fixed preview.')), _now(), voice_id))
        self.store.audit(actor_id, 'voice.clone_reconciled_found', voice_id, {'operation_id': op['id']})
        return self.get_version(voice_id)

    def resolve_approved_enrollment_voice(self, enrollment_id, actor_id):
        """Read-only adapter for an explicitly bound enrollment, without cloning,
        importing research samples, or automatically changing an active voice."""
        if os.getenv('RANEEN_VOICE_ENROLLMENT_ENABLED', '0').strip() != '1':
            raise HTTPException(404, 'Voice enrollment is not enabled on this release.')
        if not isinstance(enrollment_id, str) or not re.fullmatch(r'[0-9a-f]{32}', enrollment_id):
            raise HTTPException(404, 'Enrollment not found.')
        try:
            row = self.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,))
        except sqlite3.OperationalError:
            raise HTTPException(404, 'Enrollment is unavailable.') from None
        if not row:
            raise HTTPException(404, 'Enrollment not found.')
        if row['owner_id'] != actor_id:
            raise HTTPException(403, 'This enrollment belongs to another account.')
        if row['revoked_at']:
            raise HTTPException(410, 'Enrollment consent was withdrawn.')
        scope = _object(row['consent_json'])
        if row['consent_version'] != 'voice-enrollment-v1' or not all(scope.get(k) is True for k in ('recording', 'external_processing', 'voice_cloning', 'private_preview', 'self_attestation')):
            raise HTTPException(409, 'Current explicit enrollment scopes are required.')
        remote_id = row['voice_id']
        if row['voice_state'] != 'ready' or not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', remote_id):
            raise HTTPException(409, 'An actual ready enrollment voice is required.')
        version = self.store.one('SELECT * FROM enrollment_voice_versions WHERE session_id=? ORDER BY version DESC LIMIT 1', (enrollment_id,))
        if not version or version['provider'] != PROVIDER or version['provider_voice_id'] != remote_id or version['state'] != 'ready':
            raise HTTPException(409, 'The enrollment voice does not match its ready version.')
        serial = version['sample_manifest']
        manifest = _object(serial)
        if (not manifest or hashlib.sha256(serial.encode()).hexdigest() != version['manifest_digest']
                or not isinstance(manifest.get('chunks'), list) or not 1 <= len(manifest['chunks']) <= 128):
            raise HTTPException(409, 'Enrollment source manifest is unavailable or changed.')
        operation = self.store.one("SELECT * FROM enrollment_operations WHERE session_id=? AND kind='voice_clone' AND provider_id=? AND state='succeeded' ORDER BY created DESC,rowid DESC LIMIT 1", (enrollment_id, remote_id))
        detail = _object(operation['detail']) if operation else {}
        if (not operation or detail.get('requires_verification') is not False or detail.get('voice_version_id') != version['id']
                or detail.get('manifest_digest') != version['manifest_digest']):
            raise HTTPException(409, 'A verified completed clone operation is required.')
        if self.store.one("SELECT id FROM enrollment_operations WHERE session_id=? AND kind='voice_clone' AND state IN ('dispatching','outcome_unknown') LIMIT 1", (enrollment_id,)):
            raise HTTPException(409, 'Enrollment clone outcome still requires reconciliation.')
        if self.store.one("SELECT id FROM enrollment_operations WHERE session_id=? AND kind='cleanup_voice' AND provider_id=? LIMIT 1", (enrollment_id, remote_id)):
            raise HTTPException(409, 'This enrollment voice is scheduled for removal.')
        approved = self.store.one('SELECT approved FROM enrollment_voice_approvals WHERE session_id=? AND voice_id=?', (enrollment_id, remote_id))
        if not approved:
            raise HTTPException(409, 'Explicit enrollment voice approval is required.')
        root = (self.store.root / 'enrollments' / enrollment_id).resolve()
        if not root.is_relative_to(self.store.root.resolve()):
            raise HTTPException(409, 'Enrollment storage is unavailable.')
        seen = set()
        for source in manifest['chunks']:
            if not isinstance(source, dict) or type(source.get('seq')) is not int or source['seq'] in seen:
                raise HTTPException(409, 'Enrollment source references are invalid.')
            seen.add(source['seq'])
            chunk = self.store.one('SELECT * FROM enrollment_chunks WHERE session_id=? AND seq=?', (enrollment_id, source['seq']))
            if not chunk or chunk['role'] != 'contributor' or chunk['sha256'] != source.get('sha256'):
                raise HTTPException(409, 'Enrollment source does not belong to this consenting speaker.')
            path = Path(chunk['path']).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != chunk['byte_count']:
                raise HTTPException(409, 'Original enrollment source is unavailable.')
            with path.open('rb') as recording:
                if hashlib.file_digest(recording, 'sha256').hexdigest() != chunk['sha256']:
                    raise HTTPException(409, 'Original enrollment audio checksum changed.')
        previews = self.store.all("SELECT detail FROM enrollment_operations WHERE session_id=? AND kind='voice_preview' AND provider_id=? AND state='succeeded'", (enrollment_id, remote_id))
        ready = set()
        for preview in previews:
            metadata = _object(preview['detail'])
            kind = metadata.get('kind')
            quality = metadata.get('quality')
            if kind not in {'question', 'number', 'correction'} or not isinstance(quality, dict):
                continue
            path = (root / 'previews' / f'{remote_id}-{kind}.mp3').resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            with path.open('rb') as sample:
                if hashlib.file_digest(sample, 'sha256').hexdigest() == metadata.get('sha256'):
                    ready.add(kind)
        if ready != {'question', 'number', 'correction'}:
            raise HTTPException(409, 'All three freshly synthesized enrollment previews must be preserved and approved.')
        return dict(id=version['id'], source='enrollment', status='approved', provider=PROVIDER,
                    provider_voice_id=remote_id, enrollment_session_id=enrollment_id,
                    enrollment_voice_version_id=version['id'], manifest_digest=version['manifest_digest'],
                    approved_at=approved['approved'], preview_available=True)

    def create_candidate(self, profile_id, actor_id, *, idempotency_key=None):
        profile = self._profile(profile_id, actor_id)
        manifest = self._manifest(profile_id)
        key = idempotency_key or hashlib.sha256(_json(manifest).encode()).hexdigest()
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise HTTPException(422, 'Use an idempotency key of 1 to 128 characters.')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM voice_versions WHERE profile_id=? AND idempotency_key=?', (profile_id, key)).fetchone()
            if row:
                row = dict(row)
                verification_pending = bool(row['provider_voice_id'] and _object(row['error_json']).get('code') in ('voice_verification_required', 'provider_response_invalid'))
                if row['status'] in ('ready', 'approved', 'rejected'):
                    self._validate_manifest(profile_id, _object(row['source_manifest_json']))
                    return self._version_public(row)
                if row['status'] == 'building':
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row['updated_at'])).total_seconds()
                    if elapsed < STALE_BUILD_SECONDS:
                        raise HTTPException(409, 'This voice candidate is already building.')
                operation = db.execute('SELECT * FROM voice_provider_operations WHERE voice_version_id=? ORDER BY attempt DESC LIMIT 1', (row['id'],)).fetchone()
                if not row['provider_voice_id'] and (not operation or operation['state'] in ('dispatching', 'outcome_unknown')):
                    if not operation:
                        operation = self._new_clone_operation(db, row, profile, _object(row['source_manifest_json']), 'outcome_unknown')
                    else:
                        db.execute("UPDATE voice_provider_operations SET state='outcome_unknown',updated_at=? WHERE id=?", (_now(), operation['id']))
                    db.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(UNKNOWN_CLONE_ERROR), _now(), row['id']))
                    row.update(status='failed', error_json=_json(UNKNOWN_CLONE_ERROR))
                    return self._version_public(row)
                # Retry uses the same immutable source manifest and any provider ID
                # already returned by clone, avoiding duplicate clones after TTS failure.
                manifest = _object(row['source_manifest_json'])
                db.execute("UPDATE voice_versions SET status='building',error_json=NULL,updated_at=? WHERE id=?", (_now(), row['id']))
                ident = row['id']
            else:
                if db.execute("SELECT id FROM voice_provider_operations WHERE profile_id=? AND state IN ('dispatching','outcome_unknown') LIMIT 1", (profile_id,)).fetchone():
                    raise HTTPException(409, UNKNOWN_CLONE_ERROR)
                verification_pending = False
                ident = uuid.uuid4().hex
                version = db.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM voice_versions WHERE profile_id=?', (profile_id,)).fetchone()[0]
                stamp = _now()
                db.execute(
                    'INSERT INTO voice_versions(id,profile_id,version_number,provider,source_manifest_json,status,idempotency_key,preview_text,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,'building',?,?,?,?)",
                    (ident, profile_id, version, PROVIDER, _json(manifest), key, PREVIEW_TEXT, stamp, stamp),
                )
                self._new_clone_operation(db, dict(id=ident, version_number=version), profile, manifest)
        self.store.audit(actor_id, 'voice.candidate_building', ident)
        operation = None
        try:
            self._validate_manifest(profile_id, manifest)
            row = self.store.one('SELECT * FROM voice_versions WHERE id=?', (ident,))
            cleanup = self.store.one('SELECT status FROM voice_cleanup_jobs WHERE id=?', (ident,))
            if cleanup and cleanup['status'] == 'pending':
                self.retry_external_cleanup(profile_id)
                cleanup = self.store.one('SELECT status FROM voice_cleanup_jobs WHERE id=?', (ident,))
                if cleanup['status'] == 'pending':
                    raise HTTPException(409, 'Pending provider cleanup must finish before rebuilding this candidate.')
                row = self.store.one('SELECT * FROM voice_versions WHERE id=?', (ident,))
            provider_id = row['provider_voice_id']
            if not provider_id:
                files = []
                for source in manifest['samples']:
                    raw = (self.store.audio_dir / f"{source['audio_id']}.wav").read_bytes()
                    if hashlib.sha256(raw).hexdigest() != source['sha256']:
                        raise HTTPException(409, 'Original audio checksum changed; candidate blocked.')
                    files.append((source['audio_id'] + '.wav', raw, 'audio/wav'))
                # Authorization is checked immediately before any external upload.
                self._validate_manifest(profile_id, manifest)
                operation, dispatch = self._claim_clone_operation(ident, profile, manifest)
                if dispatch:
                    provider_id = self.provider.clone(operation['provider_name'], files)
                    if not isinstance(provider_id, str) or not provider_id.strip():
                        raise ValueError('Voice provider returned no identifier.')
                    self._record_remote_custody(ident, profile, provider_id, operation['id'])
                elif operation['state'] == 'succeeded':
                    provider_id = operation['provider_voice_id']
                else:
                    raise HTTPException(409, UNKNOWN_CLONE_ERROR)
            self._validate_manifest(profile_id, manifest)
            if verification_pending:
                verifier = getattr(self.provider, 'verify_ready', None)
                if not callable(verifier):
                    raise ProviderError(409, 'voice_verification_required', 'Provider speaker verification must be confirmed before retrying this voice.')
                verifier(provider_id)
            preview = self.provider.synthesize(provider_id, PREVIEW_TEXT)
            if not isinstance(preview, bytes) or not preview:
                raise ValueError('Voice provider returned no preview audio.')
            self._validate_manifest(profile_id, manifest)
            preview_path = self.preview_dir / f'{ident}.mp3'
            temporary = self.preview_dir / f'{ident}.tmp'
            temporary.write_bytes(preview)
            temporary.replace(preview_path)
            self.store.execute("UPDATE voice_versions SET status='ready',error_json=NULL,updated_at=? WHERE id=?", (_now(), ident))
            self.store.audit(actor_id, 'voice.candidate_ready', ident)
        except Exception as exc:
            # Never persist provider response bodies, credentials, or exception text.
            remote_id = getattr(exc, 'provider_voice_id', None)
            if isinstance(remote_id, str) and remote_id.strip():
                # A provider may create a voice before requesting its own identity
                # verification. Keep custody for cleanup and resume without cloning
                # again; this does not make that voice ready or approved.
                self._record_remote_custody(ident, profile, remote_id, operation['id'] if operation else None)
            if isinstance(exc, HTTPException):
                error = dict(code='source_blocked', message=str(exc.detail))
                self.store.execute("UPDATE voice_cleanup_jobs SET status='pending',updated_at=? WHERE id=? AND status='retained'", (_now(), ident))
                self.retry_external_cleanup(profile_id)
            else:
                code = str(getattr(exc, 'code', 'voice_provider_failed'))[:80]
                error = dict(code=code, message='Voice candidate could not be built. Check provider availability and retry this candidate.')
            if operation:
                receipt = self.store.one('SELECT * FROM voice_provider_operations WHERE id=?', (operation['id'],))
                if receipt['state'] == 'dispatching':
                    known = self._confirmed_rejection(exc)
                    self.store.execute('UPDATE voice_provider_operations SET state=?,updated_at=? WHERE id=?', ('rejected' if known else 'outcome_unknown', _now(), operation['id']))
                    if not known:
                        error = dict(UNKNOWN_CLONE_ERROR)
            self.store.execute("UPDATE voice_versions SET status='failed',error_json=?,updated_at=? WHERE id=?", (_json(error), _now(), ident))
            self.store.audit(actor_id, 'voice.candidate_failed', ident, {'code': error['code']})
        return self.get_version(ident)

    def approve(self, voice_id, actor_id):
        row = self.get_version(voice_id)
        self._profile(row['profile_id'], actor_id)
        self._validate_manifest(row['profile_id'], row['source_manifest'])
        if row['status'] == 'approved':
            return row
        if row['status'] != 'ready' or not row['provider_voice_id'] or not row['preview_available']:
            raise HTTPException(409, 'Only a ready candidate with its fixed preview can be approved.')
        stamp = _now()
        self.store.execute("UPDATE voice_versions SET status='approved',approved_at=?,updated_at=? WHERE id=? AND status='ready'", (stamp, stamp, voice_id))
        self.store.audit(actor_id, 'voice.version_approved', voice_id)
        return self.get_version(voice_id)

    def reject(self, voice_id, actor_id, notes=''):
        row = self.get_version(voice_id)
        self._profile(row['profile_id'], actor_id)
        if row['status'] == 'approved':
            raise HTTPException(409, 'Approved voice versions are immutable; build and approve a new version.')
        if row['status'] == 'building':
            raise HTTPException(409, 'A building candidate cannot be rejected yet.')
        self.store.execute("UPDATE voice_versions SET status='rejected',rejection_notes=?,updated_at=? WHERE id=?", (str(notes)[:2000], _now(), voice_id))
        self.store.audit(actor_id, 'voice.version_rejected', voice_id)
        return self.get_version(voice_id)

    def preview(self, voice_id):
        row = self.get_version(voice_id)
        self._validate_manifest(row['profile_id'], row['source_manifest'])
        if row['status'] not in ('ready', 'approved') or not row['preview_available']:
            raise HTTPException(409, 'A ready voice preview is required.')
        return (self.preview_dir / f'{voice_id}.mp3').read_bytes()

    def active_voice(self, profile_id):
        self._profile(profile_id)
        row = self.store.one("SELECT * FROM voice_versions WHERE profile_id=? AND status='approved' ORDER BY version_number DESC LIMIT 1", (profile_id,))
        if not row:
            return None
        self._validate_manifest(profile_id, _object(row['source_manifest_json']))
        if not row['provider_voice_id']:
            raise HTTPException(409, 'The previously approved remote voice is unavailable.')
        return self._version_public(row)

    def synthesize(self, voice_id, text):
        row = self.get_version(voice_id)
        self._validate_manifest(row['profile_id'], row['source_manifest'])
        if row['status'] != 'approved' or not row['provider_voice_id']:
            raise HTTPException(409, 'An approved voice is required for simulation speech.')
        if not isinstance(text, str) or not text.strip() or len(text) > 6000:
            raise HTTPException(422, 'Speech requires text of 1 to 6000 characters.')
        return self.provider.synthesize(row['provider_voice_id'], text)

    def delete_external_for_profile(self, profile_id):
        """Best-effort provider cleanup with an explicit result; local deletion alone
        cannot be reported as deletion of previously uploaded provider data."""
        profile = self._profile(profile_id)
        for row in self.store.all('SELECT id,provider_voice_id FROM voice_versions WHERE profile_id=? AND provider_voice_id IS NOT NULL', (profile_id,)):
            self.store.execute(
                "INSERT OR IGNORE INTO voice_cleanup_jobs(id,profile_id,owner_id,provider,provider_voice_id,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                (row['id'], profile_id, profile['owner_id'], PROVIDER, row['provider_voice_id'], _now(), _now()),
            )
        self.store.execute("UPDATE voice_cleanup_jobs SET status='pending',updated_at=? WHERE profile_id=? AND status='retained'", (_now(), profile_id))
        results = self.retry_external_cleanup(profile_id)
        results.extend(dict(id=op['id'], status='external_reconciliation_required') for op in self.store.all("SELECT id FROM voice_provider_operations WHERE profile_id=? AND state IN ('dispatching','outcome_unknown')", (profile_id,)))
        self.delete_local_previews(profile_id)
        return results

    def retry_external_cleanup(self, profile_id):
        results = []
        for row in self.store.all("SELECT * FROM voice_cleanup_jobs WHERE profile_id=? AND status!='retained' ORDER BY created_at,id", (profile_id,)):
            if row['status'] == 'deleted':
                results.append(dict(id=row['id'], status='deleted'))
                continue
            status = 'pending'
            try:
                self.provider.delete(row['provider_voice_id'])
                status = 'deleted'
            except Exception:
                pass
            self.store.execute('UPDATE voice_cleanup_jobs SET status=?,attempts=attempts+1,updated_at=? WHERE id=?', (status, _now(), row['id']))
            if status == 'deleted':
                self.store.execute('UPDATE voice_versions SET provider_voice_id=NULL WHERE id=?', (row['id'],))
                self.store.execute("UPDATE voice_provider_operations SET state='cleanup_deleted',updated_at=? WHERE voice_version_id=? AND provider_voice_id=?", (_now(), row['id'], row['provider_voice_id']))
            results.append(dict(id=row['id'], status='deleted' if status == 'deleted' else 'external_deletion_failed'))
        return results

    def delete_local_previews(self, profile_id):
        for row in self.store.all('SELECT id FROM voice_versions WHERE profile_id=?', (profile_id,)):
            (self.preview_dir / f"{row['id']}.mp3").unlink(missing_ok=True)
            (self.preview_dir / f"{row['id']}.tmp").unlink(missing_ok=True)
