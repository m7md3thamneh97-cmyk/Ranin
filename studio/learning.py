"""Evidence-backed personal learning. Model output proposes; human evidence rules.

The immutable snapshots are review artifacts. Every runtime compilation rechecks
the original evidence and consent, including when compiling an older snapshot.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException

from .scenarios import BY_ID, SCENARIOS


DOMAIN_POLICY = [
    'You are Raneen, an AI representative in an internal real-estate simulation; do not claim to be the human trainer.',
    'Personal speaking style never overrides truthfulness, consent, privacy, or verified business facts.',
    'No live inventory, current prices, contact source, appointment booking, payment or CRM action is available unless a verified tool result explicitly supplies it.',
    'Never invent availability, guarantee investment returns, disclose third-party private information, or claim an unexecuted action succeeded.',
    'Respect a request to stop contact. Ask one useful question at a time and preserve confirmed facts across interruptions and language changes.',
]
CATEGORIES = {'language', 'delivery', 'conversation', 'behavior', 'real_estate_behavior', 'avoid'}
_KEY = re.compile(r'^[a-z][a-z0-9_.-]{0,95}$')
_FORBIDDEN_KEYS = {'inventory', 'availability', 'current_price', 'price', 'market_fact', 'phone_number', 'contact_source', 'booking', 'payment', 'identity', 'system_prompt', 'domain_policy'}
_SKILL_LABELS = {
    'qualification.first_move': 'كيف تبدأ الحديث وتفهم احتياج العميل',
    'price_objection.first_move': 'كيف تتعامل مع اعتراض العميل على السعر',
    'handoff.trigger': 'متى تحول الحديث إلى شخص مختص',
    'response.length': 'طول ردودك وطريقتك في الاختصار',
    'preferred_language': 'اختيار اللغة أثناء الحديث',
    'code_switching': 'الانتقال بين العربية والإنجليزية',
    'explicit_correction': 'التعديل اللي طلبته على طريقة الرد',
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return uuid.uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _rows(db, query, args=()):
    return [dict(r) for r in db.execute(query, args).fetchall()]


def _one(db, query, args=()):
    row = db.execute(query, args).fetchone()
    return dict(row) if row else None


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for field in ('value_json', 'snapshot_json', 'normalized_rule_json'):
        if field in result:
            result[field.removesuffix('_json')] = json.loads(result.pop(field))
    if 'conflict' in result:
        result['conflict'] = bool(result['conflict'])
    return result


class LearningEngine:
    def __init__(self, store, provider):
        self.store = store
        self.provider = provider

    def _profile(self, db, profile_id, actor_id=None):
        profile = _one(db, 'SELECT * FROM profiles WHERE id=?', (profile_id,))
        if not profile:
            raise HTTPException(404, 'Profile not found.')
        if actor_id is not None and profile['owner_id'] != actor_id:
            raise HTTPException(403, 'Only the contributor can confirm or correct their personal model.')
        return profile

    def _current_consent(self, db, profile_id):
        consent = _one(db, 'SELECT * FROM consents WHERE profile_id=? ORDER BY rowid DESC LIMIT 1', (profile_id,))
        if not consent or not consent['collection'] or consent['withdrawn_at']:
            raise HTTPException(409, 'Active contributor collection consent is required for learning.')
        return consent

    def _consented(self, db, profile_id, consent_id):
        consent = _one(db, 'SELECT * FROM consents WHERE id=? AND profile_id=?', (consent_id, profile_id))
        return bool(consent and consent['collection'] and not consent['withdrawn_at'])

    def _source_turn(self, db, turn_id, profile_id=None, *, teaching=False):
        turn = _one(db, '''SELECT t.*, s.mode AS session_mode, s.split AS session_split,
            s.scenario_id AS scenario_id, s.consent_id AS session_consent_id
            FROM conversation_turns t JOIN teaching_sessions s ON s.id=t.session_id WHERE t.id=?''', (turn_id,))
        if not turn:
            raise HTTPException(404, 'Evidence turn not found.')
        if profile_id and turn['profile_id'] != profile_id:
            raise HTTPException(422, 'Evidence belongs to a different contributor.')
        self._current_consent(db, turn['profile_id'])
        if not self._consented(db, turn['profile_id'], turn['consent_id']) or not self._consented(db, turn['profile_id'], turn['session_consent_id']):
            raise HTTPException(409, 'The original evidence consent is no longer active.')
        if turn['role'] != 'trainer' or turn['transcript_state'] not in ('final', 'verified') or not turn['transcript'].strip():
            raise HTTPException(422, 'Learning requires a completed, nonempty trainer turn.')
        if turn['session_split'] != 'train' or (turn['scenario_id'] in BY_ID and BY_ID[turn['scenario_id']]['split'] != 'train'):
            raise HTTPException(409, 'Holdout evidence cannot enter personal learning or runtime retrieval.')
        if teaching:
            metadata = json.loads(turn.get('provider_metadata_json') or '{}')
            if metadata.get('capture_mode', turn['session_mode']) != 'teaching':
                raise HTTPException(409, 'Simulation customer speech is not a personal demonstration. Record an explicit correction instead.')
        return turn

    def _normalize(self, proposal):
        if not isinstance(proposal, dict):
            raise HTTPException(422, 'A personal rule must be a JSON object.')
        category = proposal.get('category')
        key = proposal.get('key')
        if category not in CATEGORIES or not isinstance(key, str) or not _KEY.fullmatch(key):
            raise HTTPException(422, 'Personal rules require a supported category and a lowercase behavioral key.')
        if key.split('.')[0] in _FORBIDDEN_KEYS:
            raise HTTPException(422, 'Business facts and system policy are not personal learning rules.')
        if 'value' not in proposal or proposal['value'] is None:
            raise HTTPException(422, 'Personal rules require a nonempty value.')
        try:
            encoded = _json(proposal['value'])
            confidence = float(proposal.get('confidence', .65))
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(422, 'Personal rule values must be finite JSON data.')
        if len(encoded) > 6000 or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise HTTPException(422, 'Personal rule value or confidence exceeds its limits.')
        if proposal['value'] in ('', [], {}):
            raise HTTPException(422, 'Personal rules require a nonempty value.')
        return {'category': category, 'key': key, 'value_json': encoded, 'confidence': confidence}

    def _evidence_valid(self, db, observation, *, allow_superseded=False):
        if observation['state'] == 'rejected' or (observation['superseded_at'] and not allow_superseded):
            return False
        if not self._consented(db, observation['profile_id'], observation['consent_id']):
            return False
        if observation['source_turn_id']:
            try:
                source = self._source_turn(db, observation['source_turn_id'], observation['profile_id'])
            except HTTPException:
                return False
            # Ordinary observations were admitted only by analyze_turn while the
            # source was teaching evidence. A mutable session mode must not
            # retroactively turn that evidence into simulated customer speech.
            return True
        source = _one(db, 'SELECT * FROM examples WHERE id=? AND profile_id=?', (observation['source_example_id'], observation['profile_id']))
        if not source or source['status'] != 'approved' or not self._consented(db, source['profile_id'], source['consent_id']):
            return False
        payload = json.loads(source['payload'])
        scenario = BY_ID.get(payload.get('scenario_id'))
        return bool(scenario and scenario['split'] == 'train' and payload.get('split', 'train') == 'train')

    def _eligible_observations(self, db, profile_id):
        try:
            self._current_consent(db, profile_id)
        except HTTPException:
            return []
        return [o for o in _rows(db, 'SELECT * FROM observations WHERE profile_id=? ORDER BY created_at,id', (profile_id,)) if self._evidence_valid(db, o)]

    @staticmethod
    def _strength(observation):
        if observation['state'] == 'locked':
            return 70
        return {'explicit_rule': 70, 'correction': 60, 'confirmation': 50, 'demonstration': 35, 'observation': 20, 'inference': 10}[observation['evidence_kind']]

    def _sync_hypotheses(self, db, profile_id):
        observations = self._eligible_observations(db, profile_id)
        grouped = defaultdict(list)
        for observation in observations:
            grouped[(observation['category'], observation['key'], observation['value_json'])].append(observation)
        stamp = _now()
        db.execute("UPDATE hypotheses SET state='rejected',evidence_count=0,conflict=0,updated_at=? WHERE profile_id=?", (stamp, profile_id))
        candidates = []
        for (category, key, value), group in grouped.items():
            source_ids = {o['source_turn_id'] or o['source_example_id'] for o in group}
            count = len(source_ids)
            precedence = max(self._strength(o) for o in group)
            if count > 1 and precedence == 35:
                precedence = 40
            elif count > 1 and precedence == 20:
                precedence = 30
            state = 'locked' if precedence == 70 else 'confirmed' if precedence >= 50 else 'tentative'
            confidence = 1.0 if precedence >= 50 else min(.9, max(o['confidence'] for o in group) + .05 * (count - 1))
            existing = _one(db, 'SELECT * FROM hypotheses WHERE profile_id=? AND category=? AND key=? AND value_json=?', (profile_id, category, key, value))
            ident = existing['id'] if existing else _id()
            if existing:
                db.execute('UPDATE hypotheses SET confidence=?,evidence_count=?,state=?,precedence=?,updated_at=? WHERE id=?', (confidence, count, state, precedence, stamp, ident))
            else:
                db.execute('INSERT INTO hypotheses(id,profile_id,category,key,value_json,confidence,evidence_count,state,precedence,conflict,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,?,?)', (ident, profile_id, category, key, value, confidence, count, state, precedence, stamp, stamp))
            db.execute('DELETE FROM hypothesis_observations WHERE hypothesis_id=?', (ident,))
            db.executemany('INSERT INTO hypothesis_observations VALUES(?,?)', [(ident, o['id']) for o in group])
            candidate = dict(id=ident, profile_id=profile_id, category=category, key=key, value=json.loads(value), confidence=confidence, evidence_count=count, state=state, precedence=precedence, conflict=False, latest=max(o['created_at'] for o in group), observations=group)
            candidates.append(candidate)
        by_key = defaultdict(list)
        for candidate in candidates:
            by_key[(candidate['category'], candidate['key'])].append(candidate)
        for group in by_key.values():
            highest = max(c['precedence'] for c in group)
            top = [c for c in group if c['precedence'] == highest]
            # A newer explicit correction is intentionally authoritative. Equally
            # strong ordinary evidence is a contradiction, never a majority vote.
            unresolved = len(top) > 1 and highest != 60
            for candidate in group:
                candidate['conflict'] = unresolved or (len(group) > 1 and candidate['precedence'] < highest)
                db.execute('UPDATE hypotheses SET conflict=? WHERE id=?', (int(candidate['conflict']), candidate['id']))
        return candidates

    def _insert_observation(self, db, profile_id, source, normalized, *, kind='observation', state='observed', example=False):
        ident, stamp = _id(), _now()
        db.execute('''INSERT OR IGNORE INTO observations
            (id,profile_id,source_turn_id,source_example_id,consent_id,category,key,value_json,confidence,state,evidence_kind,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (ident, profile_id, None if example else source['id'], source['id'] if example else None, source['consent_id'], normalized['category'], normalized['key'], normalized['value_json'], normalized['confidence'], state, kind, stamp))
        field = 'source_example_id' if example else 'source_turn_id'
        return _one(db, f'SELECT * FROM observations WHERE {field}=? AND category=? AND key=? AND value_json=? AND evidence_kind=?', (source['id'], normalized['category'], normalized['key'], normalized['value_json'], kind))

    def analyze_turn(self, turn_id):
        with self.store.db() as db:
            source = self._source_turn(db, turn_id, teaching=True)
            previously = _one(db, 'SELECT * FROM learning_analyses WHERE source_turn_id=?', (turn_id,))
        if previously:
            return self._result(source['profile_id'], source['session_id'], idempotent=True)
        context = self.compile_context(source['profile_id'])
        context['source_scenario_id'] = source['scenario_id']
        context['source_role'] = 'trainer'
        context['source_turn_id'] = source['id']
        context['source_consent_id'] = source['consent_id']
        proposals = self.provider.analyze(source['transcript'], context)
        if not isinstance(proposals, list) or len(proposals) > 20:
            raise HTTPException(422, 'Learning provider returned an invalid observation list.')
        normalized = [self._normalize(p) for p in proposals]
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Revalidate after the provider call: withdrawal or a finalized turn
            # cannot race an external inference into the personal model.
            source = self._source_turn(db, turn_id, teaching=True)
            if not _one(db, 'SELECT * FROM learning_analyses WHERE source_turn_id=?', (turn_id,)):
                for proposal in normalized:
                    self._insert_observation(db, source['profile_id'], source, proposal)
                db.execute('INSERT INTO learning_analyses VALUES(?,?,?)', (turn_id, source['profile_id'], _now()))
                self._create_version(db, source['profile_id'])
        return self._result(source['profile_id'], source['session_id'])

    def record_correction(self, profile_id, session_id, target_turn_id, correction_transcript, normalized_rule, source_turn_id=None, locked=False, actor_id=None):
        if not source_turn_id:
            raise HTTPException(422, 'A correction must cite its original completed trainer turn.')
        normalized = self._normalize(normalized_rule)
        normalized['confidence'] = 1.0
        rule_json = _json({'category': normalized['category'], 'key': normalized['key'], 'value': json.loads(normalized['value_json'])})
        idempotent = False
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._profile(db, profile_id, actor_id)
            source = self._source_turn(db, source_turn_id, profile_id)
            target = _one(db, 'SELECT * FROM conversation_turns WHERE id=? AND profile_id=? AND session_id=?', (target_turn_id, profile_id, session_id))
            if source['session_id'] != session_id or not target or target['role'] != 'raneen':
                raise HTTPException(422, 'Correction evidence and the Raneen response must belong to the same session.')
            if correction_transcript.strip() != source['transcript'].strip():
                raise HTTPException(422, 'Correction text must preserve the exact original trainer transcript.')
            if source['turn_index'] <= target['turn_index']:
                raise HTTPException(422, 'A correction must follow the response being corrected.')
            existing = _one(db, 'SELECT * FROM corrections WHERE source_turn_id=? AND target_turn_id=? AND normalized_rule_json=?', (source_turn_id, target_turn_id, rule_json))
            if existing:
                # The transaction protects replay receipts against a concurrent
                # new correction. Replaying an older receipt changes nothing.
                correction_id = existing['id']
                idempotent = True
                applied = any(o['id'] == existing['observation_id'] for o in self._eligible_observations(db, profile_id))
                conflicts_locked = False
            else:
                existing_locked = [o for o in self._eligible_observations(db, profile_id) if o['category'] == normalized['category'] and o['key'] == normalized['key'] and o['state'] == 'locked']
                conflicts_locked = any(o['value_json'] != normalized['value_json'] for o in existing_locked)
                if locked and conflicts_locked:
                    raise HTTPException(409, 'This key already has a different locked rule. A correction cannot silently replace it.')
                kind, state = ('explicit_rule', 'locked') if locked else ('correction', 'confirmed')
                observation = self._insert_observation(db, profile_id, source, normalized, kind=kind, state=state)
                correction_id = _id()
                db.execute('INSERT INTO corrections VALUES(?,?,?,?,?,?,?,?,?,?)', (correction_id, profile_id, session_id, target_turn_id, source_turn_id, source['consent_id'], source['transcript'], rule_json, observation['id'], _now()))
                if not conflicts_locked:
                    # Old snapshots retain their original decision for audit.
                    for old in self._eligible_observations(db, profile_id):
                        if old['id'] != observation['id'] and old['category'] == normalized['category'] and old['key'] == normalized['key'] and self._strength(old) <= self._strength(observation) and old['value_json'] != normalized['value_json']:
                            db.execute('UPDATE observations SET superseded_at=? WHERE id=?', (_now(), old['id']))
                self._create_version(db, profile_id)
                applied = not conflicts_locked
        result = self._result(profile_id, session_id, idempotent=idempotent)
        result.update(correction_id=correction_id, applied=applied, blocked_by_locked_rule=conflicts_locked)
        if not idempotent:
            result['events'].insert(0, {'type': 'learning.correction_recorded', 'correction_id': correction_id})
        return result

    def confirm_hypothesis(self, hypothesis_id, source_turn_id, locked=False, actor_id=None):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            hypothesis = _one(db, 'SELECT * FROM hypotheses WHERE id=?', (hypothesis_id,))
            if not hypothesis:
                raise HTTPException(404, 'Hypothesis not found.')
            profile_id = hypothesis['profile_id']
            self._profile(db, profile_id, actor_id)
            source = self._source_turn(db, source_turn_id, profile_id)
            if hypothesis['state'] == 'rejected':
                raise HTTPException(409, 'Rejected or withdrawn evidence cannot be confirmed.')
            proposal = {'category': hypothesis['category'], 'key': hypothesis['key'], 'value_json': hypothesis['value_json'], 'confidence': 1.0}
            for old in self._eligible_observations(db, profile_id):
                if old['category'] == hypothesis['category'] and old['key'] == hypothesis['key'] and old['state'] == 'locked' and old['value_json'] != hypothesis['value_json']:
                    raise HTTPException(409, 'A confirmation cannot replace a different locked rule.')
            observation = self._insert_observation(db, profile_id, source, proposal, kind='explicit_rule' if locked else 'confirmation', state='locked' if locked else 'confirmed')
            self._create_version(db, profile_id)
        result = self._result(profile_id, source['session_id'])
        result['confirmation_observation_id'] = observation['id']
        return result

    def import_examples(self, profile_id):
        """Import only independently reviewed, exact, train demonstrations."""
        with self.store.db() as db:
            self._profile(db, profile_id)
            self._current_consent(db, profile_id)
            examples = _rows(db, "SELECT * FROM examples WHERE profile_id=? AND status='approved'", (profile_id,))
        imported = 0
        for example in examples:
            payload = json.loads(example['payload'])
            scenario = BY_ID.get(payload.get('scenario_id'))
            if not scenario or scenario['split'] != 'train' or payload.get('split', 'train') != 'train' or not payload.get('transcript_verified'):
                continue
            with self.store.db() as db:
                if not self._consented(db, profile_id, example['consent_id']) or _one(db, 'SELECT id FROM observations WHERE source_example_id=?', (example['id'],)):
                    continue
            context = self.compile_context(profile_id)
            context.update(source_scenario_id=scenario['id'], source_role='trainer', source_kind='reviewed_demonstration', source_example_id=example['id'], source_consent_id=example['consent_id'])
            proposals = self.provider.analyze(payload['response_text'], context)
            if not isinstance(proposals, list) or len(proposals) > 20:
                raise HTTPException(422, 'Learning provider returned an invalid observation list.')
            normalized = [self._normalize(p) for p in proposals]
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                self._current_consent(db, profile_id)
                current = _one(db, "SELECT * FROM examples WHERE id=? AND status='approved'", (example['id'],))
                if not current or not self._consented(db, profile_id, current['consent_id']):
                    continue
                for proposal in normalized:
                    self._insert_observation(db, profile_id, current, proposal, kind='demonstration', example=True)
                imported += 1
                self._create_version(db, profile_id)
        result = self._result(profile_id)
        result['imported_examples'] = imported
        return result

    def _representative_examples(self, db, profile_id):
        examples = []
        for source in _rows(db, "SELECT * FROM examples WHERE profile_id=? AND status='approved' ORDER BY created,id", (profile_id,)):
            payload = json.loads(source['payload'])
            scenario = BY_ID.get(payload.get('scenario_id'))
            if not self._consented(db, profile_id, source['consent_id']) or not scenario or scenario['split'] != 'train' or payload.get('split', 'train') != 'train' or not payload.get('transcript_verified'):
                continue
            examples.append({**payload, 'id': source['id'], 'consent_id': source['consent_id']})
        return examples[-12:]

    def _snapshot(self, db, profile_id):
        candidates = self._sync_hypotheses(db, profile_id)
        grouped = defaultdict(list)
        for candidate in candidates:
            grouped[(candidate['category'], candidate['key'])].append(candidate)
        snapshot = {'language': {}, 'delivery': {}, 'conversation': {}, 'real_estate_behavior': {}, 'confirmed_rules': [], 'avoid': [], 'representative_examples': self._representative_examples(db, profile_id), 'selected_rules': [], 'conflicts': []}
        for (category, key), group in sorted(grouped.items()):
            strongest = max(c['precedence'] for c in group)
            top = sorted([c for c in group if c['precedence'] == strongest], key=lambda c: (c['latest'], c['id']), reverse=True)
            if len(top) > 1 and strongest != 60:
                snapshot['conflicts'].append({'category': category, 'key': key, 'hypothesis_ids': [c['id'] for c in top], 'values': [c['value'] for c in top]})
                continue
            winner = top[0]
            evidence = winner['observations']
            rule = {k: winner[k] for k in ('category', 'key', 'value', 'confidence', 'state', 'precedence')}
            rule.update(hypothesis_id=winner['id'], evidence_count=winner['evidence_count'], source_observation_ids=[o['id'] for o in evidence], source_turn_ids=sorted({o['source_turn_id'] for o in evidence if o['source_turn_id']}), source_example_ids=sorted({o['source_example_id'] for o in evidence if o['source_example_id']}), consent_ids=sorted({o['consent_id'] for o in evidence}), evidence_kind=max(evidence, key=self._strength)['evidence_kind'])
            snapshot['selected_rules'].append(rule)
            if category == 'avoid':
                snapshot['avoid'].append(rule)
            else:
                section = 'real_estate_behavior' if category == 'behavior' else category
                snapshot[section][key] = rule['value']
            if rule['state'] in ('confirmed', 'locked'):
                snapshot['confirmed_rules'].append(rule)
        return snapshot

    def _create_version(self, db, profile_id):
        self._profile(db, profile_id)
        self._current_consent(db, profile_id)
        snapshot = self._snapshot(db, profile_id)
        encoded = _json(snapshot)
        previous = _one(db, 'SELECT * FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC LIMIT 1', (profile_id,))
        if previous and previous['snapshot_json'] == encoded:
            return _decode(previous)
        ident = _id()
        number = previous['version_number'] + 1 if previous else 1
        db.execute('INSERT INTO agent_profile_versions VALUES(?,?,?,?,?,?,?,?)', (ident, profile_id, number, 'draft', previous['id'] if previous else None, encoded, _now(), None))
        return _decode(_one(db, 'SELECT * FROM agent_profile_versions WHERE id=?', (ident,)))

    def create_version(self, profile_id):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._create_version(db, profile_id)

    def versions(self, profile_id):
        with self.store.db() as db:
            self._profile(db, profile_id)
            return [_decode(v) for v in _rows(db, 'SELECT * FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC', (profile_id,))]

    def compile_context(self, profile_id, profile_version_id=None):
        with self.store.db() as db:
            profile = self._profile(db, profile_id)
            self._current_consent(db, profile_id)
            if profile_version_id:
                version = _one(db, 'SELECT * FROM agent_profile_versions WHERE id=? AND profile_id=?', (profile_version_id, profile_id))
                if not version:
                    raise HTTPException(404, 'Personal profile version not found for this contributor.')
            else:
                version = _one(db, 'SELECT * FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC LIMIT 1', (profile_id,))
            snapshot = json.loads(version['snapshot_json']) if version else {'selected_rules': [], 'representative_examples': [], 'conflicts': []}
            rules = []
            excluded = 0
            for rule in snapshot.get('selected_rules', []):
                sources = []
                for observation_id in rule.get('source_observation_ids', []):
                    observation = _one(db, 'SELECT * FROM observations WHERE id=? AND profile_id=?', (observation_id, profile_id))
                    if observation and self._evidence_valid(db, observation, allow_superseded=True):
                        sources.append(observation)
                if not sources:
                    excluded += 1
                    continue
                # Recompute source claims so a partially withdrawn hypothesis
                # cannot retain an inflated evidence count or false confirmation.
                valid_rule = dict(rule)
                strength = max(self._strength(o) for o in sources)
                valid_rule.update(source_observation_ids=[o['id'] for o in sources], source_turn_ids=sorted({o['source_turn_id'] for o in sources if o['source_turn_id']}), source_example_ids=sorted({o['source_example_id'] for o in sources if o['source_example_id']}), consent_ids=sorted({o['consent_id'] for o in sources}), evidence_count=len({o['source_turn_id'] or o['source_example_id'] for o in sources}), state='locked' if strength == 70 else 'confirmed' if strength >= 50 else 'tentative', precedence=strength)
                rules.append(valid_rule)
            allowed_examples = {e['id']: e for e in self._representative_examples(db, profile_id)}
            examples = [allowed_examples[e['id']] for e in snapshot.get('representative_examples', []) if e.get('id') in allowed_examples]
            # Do not expose stale materialized sections through a side channel.
            active_conflicts = []
            for conflict in snapshot.get('conflicts', []):
                values, hypothesis_ids = [], []
                for hypothesis_id in conflict.get('hypothesis_ids', []):
                    evidence = _rows(db, '''SELECT o.* FROM observations o JOIN hypothesis_observations h
                        ON h.observation_id=o.id WHERE h.hypothesis_id=? AND o.profile_id=?''', (hypothesis_id, profile_id))
                    valid = [o for o in evidence if self._evidence_valid(db, o, allow_superseded=True)]
                    if valid:
                        values.append(json.loads(valid[0]['value_json']))
                        hypothesis_ids.append(hypothesis_id)
                if len(values) > 1:
                    active_conflicts.append({'category': conflict['category'], 'key': conflict['key'], 'values': values, 'hypothesis_ids': hypothesis_ids})
            filtered = {'language': {}, 'delivery': {}, 'conversation': {}, 'real_estate_behavior': {}, 'confirmed_rules': [], 'avoid': [], 'selected_rules': rules, 'representative_examples': examples, 'conflicts': active_conflicts}
            for rule in rules:
                if rule['category'] == 'avoid':
                    filtered['avoid'].append(rule)
                else:
                    section = 'real_estate_behavior' if rule['category'] == 'behavior' else rule['category']
                    filtered[section][rule['key']] = rule['value']
                if rule['state'] in ('confirmed', 'locked'):
                    filtered['confirmed_rules'].append(rule)
            context = {'profile_version_id': version['id'] if version else None, 'profile': {k: profile[k] for k in ('id', 'name', 'dialect', 'style_notes')}, 'snapshot': filtered, 'personal_rules': rules, 'representative_examples': examples, 'domain_policy': list(DOMAIN_POLICY), 'provenance': {'profile_id': profile_id, 'version_id': version['id'] if version else None, 'source_turn_ids': sorted({t for r in rules for t in r['source_turn_ids']}), 'source_example_ids': sorted({e for r in rules for e in r['source_example_ids']})}, 'excluded_evidence_count': excluded}
        # A private voice enrollment has a stricter, separate evidence boundary.
        # The ordinary research profile contract remains unchanged.
        from .enrollment_learning import filter_learning_context
        return filter_learning_context(self.store, profile_id, context)

    def next_question(self, profile_id, session_id=None):
        with self.store.db() as db:
            self._profile(db, profile_id)
            self._current_consent(db, profile_id)
            candidates = self._sync_hypotheses(db, profile_id)
            sessions = _rows(db, '''SELECT DISTINCT s.scenario_id,s.consent_id,t.consent_id AS turn_consent_id
                FROM teaching_sessions s JOIN conversation_turns t ON t.session_id=s.id
                WHERE s.profile_id=? AND s.split='train' AND t.role='trainer'
                AND t.transcript_state IN ('final','verified') AND TRIM(t.transcript)!=''
                AND (json_extract(t.provider_metadata_json,'$.capture_mode')='teaching'
                    OR (json_extract(t.provider_metadata_json,'$.capture_mode') IS NULL AND s.mode='teaching'))''', (profile_id,))
            sessions = [s for s in sessions if self._consented(db, profile_id, s['consent_id']) and self._consented(db, profile_id, s['turn_consent_id'])]
            recent_correction = _one(db, 'SELECT * FROM corrections WHERE profile_id=? ORDER BY created_at DESC LIMIT 1', (profile_id,))
        unresolved = defaultdict(list)
        for candidate in candidates:
            if candidate['conflict']:
                unresolved[(candidate['category'], candidate['key'])].append(candidate)
        for (category, key), group in sorted(unresolved.items()):
            values = [c['value'] for c in candidates if c['category'] == category and c['key'] == key]
            if len(values) > 1:
                label = _SKILL_LABELS.get(key, 'التعامل مع هذا الموقف')
                return {'action': 'challenge_contradiction', 'reason': 'Independent trainer evidence disagrees; ask for the condition that distinguishes it.', 'category': category, 'key': key, 'values': values, 'question': f'لاحظت طريقتين مختلفتين عندك في {label}. متى تستخدم كل طريقة؟'}
        if recent_correction:
            rule = json.loads(recent_correction['normalized_rule_json'])
            active = any(c['category'] == rule['category'] and c['key'] == rule['key'] and c['value'] == rule['value'] for c in candidates)
            if active:
                return {'action': 'role_reverse', 'reason': 'A recent explicit correction should be tested in a fresh exchange.', 'key': rule['key'], 'correction_id': recent_correction['id'], 'question': 'خلنا نجرب مرة ثانية. أنت العميل وأنا أرد بالطريقة اللي وضحتها.'}
        tentative = sorted([c for c in candidates if c['state'] == 'tentative'], key=lambda c: (c['confidence'], c['evidence_count'], c['key']))
        if tentative:
            candidate = tentative[0]
            if candidate['key'] == 'price_objection.first_move':
                question = 'طيب إذا العميل قال إنه يقارنها بمشروع ثاني، شو بتسأله بعدها؟'
            else:
                label = _SKILL_LABELS.get(candidate['key'], 'التعامل مع هذا الموقف')
                question = f'أبي أتأكد إني فهمت طريقتك في {label}. ممكن تعطيني موقف تتصرف فيه بشكل مختلف؟'
            return {'action': 'test_hypothesis', 'reason': 'Test a tentative inference with a variation instead of treating it as a rule.', 'hypothesis_id': candidate['id'], 'key': candidate['key'], 'value': candidate['value'], 'question': question}
        covered = {s['scenario_id'] for s in sessions}
        priorities = ['purpose-01', 'uncertain-01', 'language-01', 'budget-01', 'tone-01', 'boundary-01', 'privacy-01', 'switch-01']
        for scenario_id in priorities:
            if scenario_id not in covered:
                scenario = BY_ID[scenario_id]
                return {'action': 'introduce_variation', 'reason': 'Explore an uncovered high-value real-estate decision.', 'scenario_id': scenario_id, 'family': scenario['family'], 'question': f'خلنا نجرب موقف: العميل يقول «{scenario["caller"]}». كيف ترد بطريقتك؟'}
        return {'action': 'continue_listening', 'reason': 'Let the trainer supply richer independent examples.', 'question': 'احكي لي عن موقف صعب مع عميل، وكيف قررت تتعامل معه.'}

    def learning_state(self, profile_id):
        with self.store.db() as db:
            self._profile(db, profile_id)
            observations = [_decode(o) for o in self._eligible_observations(db, profile_id)]
            candidates = self._sync_hypotheses(db, profile_id)
            hypotheses = [{k: v for k, v in c.items() if k not in ('observations', 'latest')} for c in candidates]
            corrections = [_decode(c) for c in _rows(db, 'SELECT * FROM corrections WHERE profile_id=? ORDER BY created_at', (profile_id,)) if any(o['source_turn_id'] == c['source_turn_id'] for o in observations)]
            versions = _rows(db, '''SELECT id,profile_id,version_number,status,parent_version_id,created_at,approved_at
                FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC''', (profile_id,))
            latest = _decode(_one(db, 'SELECT * FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC LIMIT 1', (profile_id,)))
        planner = self.next_question(profile_id)
        return {'profile_id': profile_id, 'observations': observations, 'hypotheses': hypotheses, 'corrections': corrections, 'versions': versions, 'profile_version': latest, 'profile_version_id': latest['id'] if latest else None, 'planner': planner, 'next_question': planner, 'provider': getattr(self.provider, 'name', self.provider.__class__.__name__)}

    def _result(self, profile_id, session_id=None, idempotent=False):
        result = self.learning_state(profile_id)
        result['idempotent'] = idempotent
        result['events'] = [{'type': 'learning.observation_created', 'observation_id': o['id']} for o in result['observations']]
        result['events'] += [{'type': 'learning.hypothesis_updated', 'hypothesis_id': h['id']} for h in result['hypotheses']]
        if result['profile_version']:
            result['events'].append({'type': 'learning.profile_version_created', 'profile_version_id': result['profile_version']['id']})
        if idempotent:
            result['events'] = []
        return result
