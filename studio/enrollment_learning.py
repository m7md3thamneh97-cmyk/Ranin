"""Server-only bridge from reviewed spoken enrollment to personal learning.

An enrollment's disclosed permission is represented internally for the learning
engine, without granting research exports, commercial use, or arbitrary text
ingestion. Only provider-audio evidence with an exact spoken confirmation crosses
this boundary. The generic learning HTTP API is not an enrollment write path.
"""
from __future__ import annotations

import copy
import json
import uuid

from fastapi import HTTPException

from .app import now, uid
from .enrollment_evidence import AFFIRMATIONS, EvidenceService, normalized
from .providers import LocalLearningProvider
from .scenarios import BY_ID


SCHEMA_VERSION = 'raneen-backend-v1'
# Fits the existing realtime instruction budget as well as the model adapter;
# selection is explicit in the manifest and every omitted source remains saved.
CONTEXT_BYTE_LIMIT = 24_000
MAX_CONTEXT_RULES = 12


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _stable(*parts):
    return uuid.uuid5(uuid.NAMESPACE_URL, 'raneen:enrollment-learning:' + ':'.join(str(p) for p in parts)).hex


def _one(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None


def _has_table(store, name):
    return bool(store.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _binding(store, profile_id):
    if not _has_table(store, 'enrollment_learning_bindings'):
        return None
    return store.one('SELECT * FROM enrollment_learning_bindings WHERE profile_id=?', (profile_id,))


def _active_enrollment(row, *, external=False):
    if not row or row['revoked_at']:
        raise HTTPException(410, 'Enrollment consent has been withdrawn.')
    scopes = json.loads(row['consent_json'])
    if row['consent_version'] != 'voice-enrollment-v1' or not scopes.get('self_attestation') or not scopes.get('recording'):
        raise HTTPException(409, 'This learning session needs its original disclosed enrollment consent.')
    if external and (not scopes.get('external_processing') or not scopes.get('private_preview')):
        raise HTTPException(403, 'This enrollment does not permit external private agent learning.')
    return scopes


def check_enrollment_binding(store, profile_id, user_id=None):
    """Return an active private binding, or None for an ordinary research profile."""
    binding = _binding(store, profile_id)
    if not binding:
        return None
    enrollment = store.one('SELECT * FROM enrollment_sessions WHERE id=?', (binding['enrollment_id'],))
    _active_enrollment(enrollment)
    if binding['revoked_at']:
        raise HTTPException(410, 'Enrollment learning consent has been withdrawn.')
    if user_id is not None and enrollment['owner_id'] != user_id:
        raise HTTPException(403, 'This private enrollment belongs to another contributor.')
    source = store.one('SELECT * FROM consents WHERE id=? AND profile_id=?', (binding['consent_id'], profile_id))
    if not source or not source['collection'] or source['withdrawn_at'] or source['version'] != enrollment['consent_version']:
        raise HTTPException(410, 'The enrollment learning permission is no longer active.')
    return binding


def _trusted(store, evidence_id, enrollment_id, binding, *, allow_superseded=False):
    statuses = ('confirmed', 'superseded') if allow_superseded else ('confirmed',)
    evidence = store.one('''SELECT e.*,p.source_ordinal,p.confirmation_ordinal,p.call_id AS provenance_call_id,
        p.confirmed_method,p.spoken_after_ordinal,p.replaces_id AS provenance_replaces_id
        FROM enrollment_evidence e JOIN enrollment_evidence_provenance p ON p.evidence_id=e.id
        WHERE e.id=? AND e.session_id=?''', (evidence_id, enrollment_id))
    if not evidence or evidence['status'] not in statuses or evidence['confirmed_method'] != 'trusted_audio_exact_phrase':
        return None
    source = store.one('SELECT * FROM enrollment_audio_turns WHERE ordinal=? AND session_id=?', (evidence['source_ordinal'], enrollment_id))
    confirmation = store.one('SELECT * FROM enrollment_audio_turns WHERE ordinal=? AND session_id=?', (evidence['confirmation_ordinal'], enrollment_id))
    if not source or not confirmation or not source['committed'] or not confirmation['committed'] or not source['transcript'] or not confirmation['transcript']:
        return None
    if source['call_id'] != evidence['provenance_call_id'] or confirmation['call_id'] != evidence['provenance_call_id']:
        return None
    if evidence['spoken_after_ordinal'] is None or not (source['ordinal'] <= evidence['spoken_after_ordinal'] < confirmation['ordinal']):
        return None
    if normalized(confirmation['transcript']) not in AFFIRMATIONS or evidence['confirmation_transcript'] != confirmation['transcript']:
        return None
    payload = json.loads(evidence['payload'])
    if payload.get('source_transcript') != source['transcript'] or evidence['source_item_id'] != source['call_id'] + ':' + source['item_id']:
        return None
    if not all(isinstance(payload.get(k), str) and payload[k].strip() for k in ('situation', 'interpretation', 'change_condition')):
        return None
    mode = store.one('SELECT * FROM enrollment_learning_audio_modes WHERE source_ordinal=? AND enrollment_id=?', (source['ordinal'], enrollment_id))
    if mode:
        capture_mode = mode['capture_mode']
        if mode['call_id'] != source['call_id'] or mode['item_id'] != source['item_id']:
            return None
    elif source['created'] <= binding['created_at']:
        # Before this integration, there were no learning simulation tools.
        # Existing provider-reviewed evidence remains usable; newly captured
        # evidence without a server mode stamp fails closed.
        capture_mode = 'teaching'
    else:
        return None
    target = store.one('SELECT * FROM enrollment_learning_correction_targets WHERE evidence_id=? AND enrollment_id=?', (evidence_id, enrollment_id))
    if capture_mode == 'simulation' and not target:
        return None
    return {'evidence': evidence, 'payload': payload, 'source': source, 'confirmation': confirmation, 'capture_mode': capture_mode, 'target': target}


def _mapped_evidence(store, binding):
    result = {}
    for mapping in store.all('SELECT * FROM enrollment_learning_evidence WHERE profile_id=? AND enrollment_id=?', (binding['profile_id'], binding['enrollment_id'])):
        trusted = _trusted(store, mapping['evidence_id'], binding['enrollment_id'], binding)
        if trusted:
            result[mapping['observation_id']] = (mapping, trusted)
    return result


def _rebuild_snapshot(context, rules, demonstrations):
    snapshot = {'language': {}, 'delivery': {}, 'conversation': {}, 'real_estate_behavior': {}, 'confirmed_rules': [], 'avoid': [], 'selected_rules': rules, 'representative_examples': [], 'conflicts': []}
    for rule in rules:
        if rule['category'] == 'avoid':
            snapshot['avoid'].append(rule)
        else:
            section = 'real_estate_behavior' if rule['category'] == 'behavior' else rule['category']
            snapshot[section][rule['key']] = rule['value']
        if rule['state'] in ('confirmed', 'locked'):
            snapshot['confirmed_rules'].append(rule)
    context.update(snapshot=snapshot, personal_rules=rules, representative_examples=[], enrollment_demonstrations=demonstrations)
    context['provenance'] = {'profile_id': context['profile']['id'], 'version_id': context.get('profile_version_id'),
        'source_turn_ids': sorted({x for r in rules for x in r['source_turn_ids']}), 'source_example_ids': [],
        'enrollment_evidence_ids': sorted({x['evidence_id'] for x in demonstrations})}


def filter_learning_context(store, profile_id, context):
    """Fail closed on provenance and report bounded selection instead of hiding it."""
    binding = check_enrollment_binding(store, profile_id)
    if not binding:
        return context
    context = copy.deepcopy(context)
    valid = _mapped_evidence(store, binding)
    selected = []
    for original in context.get('personal_rules', []):
        sources = [valid[x] for x in original.get('source_observation_ids', []) if x in valid]
        if not sources:
            continue
        rule = dict(original)
        rule.update(source_observation_ids=[m['observation_id'] for m, _ in sources],
            source_turn_ids=sorted({m['source_turn_id'] for m, _ in sources}), source_example_ids=[],
            consent_ids=[binding['consent_id']], evidence_count=len({t['source']['ordinal'] for _, t in sources}),
            enrollment_evidence_ids=[m['evidence_id'] for m, _ in sources])
        selected.append((rule, sources))
    selected.sort(key=lambda pair: (pair[0]['precedence'], max(x[0]['imported_at'] for x in pair[1]), pair[0]['key']), reverse=True)
    all_evidence_ids = sorted({m['evidence_id'] for _, sources in selected for m, _ in sources})
    selected = selected[:MAX_CONTEXT_RULES]
    while True:
        rules = [r for r, _ in selected]
        demonstrations = []
        for _, sources in selected:
            # Multiple independent sources support a hypothesis; one recent
            # exact demonstration per rule keeps the voice runtime bounded.
            mapping, trusted = max(sources, key=lambda source: source[0]['imported_at'])
            demonstrations.append({'evidence_id': mapping['evidence_id'], 'source_turn_id': mapping['source_turn_id'],
                'consent_id': binding['consent_id'], 'source_transcript': trusted['source']['transcript'],
                'situation': trusted['payload']['situation'], 'interpretation': trusted['payload']['interpretation'],
                'change_condition': trusted['payload']['change_condition'], 'confirmation_method': 'trusted_audio_exact_phrase'})
        _rebuild_snapshot(context, rules, demonstrations)
        included = sorted({eid for rule in rules for eid in rule['enrollment_evidence_ids']})
        context['enrollment_learning'] = {'schema_version': SCHEMA_VERSION, 'enrollment_id': binding['enrollment_id'],
            'teaching_session_id': binding['teaching_session_id'], 'authorization_scope': 'private_voice_enrollment',
            'selection': {'strategy': 'strongest_rules_then_recent_confirmed_evidence', 'available_evidence_count': len(all_evidence_ids),
                'selected_evidence_ids': included, 'omitted_evidence_ids': sorted(set(all_evidence_ids) - set(included)),
                'representative_demonstration_ids': [d['evidence_id'] for d in demonstrations],
                'capacity_limited': len(included) < len(all_evidence_ids), 'maximum_rules': MAX_CONTEXT_RULES}}
        if len(_json(context).encode()) <= CONTEXT_BYTE_LIMIT:
            return context
        if not selected:
            raise HTTPException(409, 'The enrollment context exceeds its supported capacity; no silent truncation was applied.')
        selected.pop()


def authorize_learning_context(store, profile_id, context):
    binding = check_enrollment_binding(store, profile_id)
    if not binding:
        return False
    enrollment = store.one('SELECT * FROM enrollment_sessions WHERE id=?', (binding['enrollment_id'],))
    _active_enrollment(enrollment, external=True)
    valid = _mapped_evidence(store, binding)
    allowed_turns = {mapping['source_turn_id'] for mapping, _ in valid.values()} | {mapping['confirmation_turn_id'] for mapping, _ in valid.values()}
    requested_turns = set(context.get('provenance', {}).get('source_turn_ids', []))
    if context.get('source_turn_id'):
        requested_turns.add(context['source_turn_id'])
    if context.get('source_consent_id') not in (None, binding['consent_id']):
        raise HTTPException(409, 'Provider evidence belongs to a different enrollment permission.')
    linked_context = context.get('enrollment_learning')
    if linked_context and linked_context.get('enrollment_id') != binding['enrollment_id']:
        raise HTTPException(409, 'The provider context belongs to a different enrollment.')
    for rule in context.get('personal_rules', []):
        if not set(rule.get('source_observation_ids', [])) <= set(valid) or not rule.get('source_observation_ids'):
            raise HTTPException(409, 'Only trusted, spoken-confirmed enrollment evidence can enter this agent.')
        requested_turns.update(rule.get('source_turn_ids', []))
        if set(rule.get('consent_ids', [])) != {binding['consent_id']}:
            raise HTTPException(409, 'The personal rule belongs to a different enrollment permission.')
        for observation_id in rule['source_observation_ids']:
            observation = store.one('SELECT category,key,value_json,state FROM observations WHERE id=? AND profile_id=?', (observation_id, profile_id))
            if not observation or observation['state'] == 'rejected' or observation['category'] != rule.get('category') or observation['key'] != rule.get('key') or json.loads(observation['value_json']) != rule.get('value'):
                raise HTTPException(409, 'The personal rule differs from its trusted enrollment evidence.')
    if not requested_turns <= allowed_turns or context.get('source_example_id') or context.get('representative_examples'):
        raise HTTPException(409, 'This provider context contains evidence outside the private enrollment.')
    for demonstration in context.get('enrollment_demonstrations', []):
        matched = [(m, t) for m, t in valid.values() if m['evidence_id'] == demonstration.get('evidence_id')]
        if not matched or matched[0][1]['source']['transcript'] != demonstration.get('source_transcript') or matched[0][0]['source_turn_id'] != demonstration.get('source_turn_id') or demonstration.get('consent_id') != binding['consent_id']:
            raise HTTPException(409, 'The enrollment demonstration is not an exact trusted source.')
        if any(matched[0][1]['payload'][key] != demonstration.get(key) for key in ('situation', 'interpretation', 'change_condition')):
            raise HTTPException(409, 'The enrollment interpretation differs from its spoken review.')
    return True


class EnrollmentLearningBridge:
    def __init__(self, store, learning, simulation, evidence_service=None):
        self.store, self.learning, self.simulation = store, learning, simulation
        self.evidence = evidence_service or EvidenceService(store)
        self.normalizer = LocalLearningProvider()

    def ensure_binding(self, enrollment_id):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            enrollment = _one(db, 'SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,))
            if not enrollment:
                raise HTTPException(404, 'Enrollment not found.')
            scopes = _active_enrollment(enrollment)
            existing = _one(db, 'SELECT * FROM enrollment_learning_bindings WHERE enrollment_id=?', (enrollment_id,))
            if existing:
                if existing['revoked_at']:
                    raise HTTPException(410, 'Enrollment learning was revoked.')
                return {**existing, 'session_id': existing['teaching_session_id'], 'contract': SCHEMA_VERSION}
            profile_id, consent_id, session_id, stamp = uid(), uid(), uid(), now()
            db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?,?)', (profile_id, enrollment['owner_id'], 'Private voice enrollment', 'Natural contributor dialect', 'behavior', '', stamp))
            # The exact grant and origin are retained; the export booleans are
            # deliberately false. This is not a research or commercial license.
            db.execute('INSERT INTO consents VALUES(?,?,?,?,?,?,?,?,?)', (consent_id, profile_id, enrollment['consent_version'], int(bool(scopes['recording'])), 0, 0, scopes.get('text', 'Private voice enrollment consent'), stamp, None))
            db.execute('''INSERT INTO teaching_sessions(id,profile_id,consent_id,status,mode,split,realtime_provider,started_at,created_at)
                VALUES(?,?,?,'active','teaching','train','enrollment_realtime',?,?)''', (session_id, profile_id, consent_id, enrollment['created'], stamp))
            db.execute('INSERT INTO enrollment_learning_bindings VALUES(?,?,?,?,?,NULL)', (enrollment_id, profile_id, consent_id, session_id, stamp))
            self._event(db, session_id, 'session.started', enrollment_id, {'enrollment_id': enrollment_id, 'profile_id': profile_id})
            result = _one(db, 'SELECT * FROM enrollment_learning_bindings WHERE enrollment_id=?', (enrollment_id,))
            return {**result, 'session_id': session_id, 'contract': SCHEMA_VERSION}

    def _active(self, enrollment_id):
        binding = self.ensure_binding(enrollment_id)
        check_enrollment_binding(self.store, binding['profile_id'])
        return binding

    def _event(self, db, session_id, kind, identity, payload):
        db.execute('INSERT OR IGNORE INTO session_events(id,session_id,type,payload_json,created_at) VALUES(?,?,?,?,?)', (_stable(session_id, kind, identity), session_id, kind, _json(payload), now()))

    def note_mode(self, enrollment_id, call_id, item_id, mode=None):
        binding = self._active(enrollment_id)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.evidence._active(db, enrollment_id, call_id):
                return None
            source = _one(db, 'SELECT * FROM enrollment_audio_turns WHERE session_id=? AND call_id=? AND item_id=?', (enrollment_id, call_id, item_id))
            if not source:
                return None
            session = _one(db, 'SELECT * FROM teaching_sessions WHERE id=?', (binding['teaching_session_id'],))
            capture_mode = session['mode']
            if mode is not None and mode != capture_mode:
                raise HTTPException(409, 'Capture mode must follow the server-owned teaching session.')
            db.execute('INSERT OR IGNORE INTO enrollment_learning_audio_modes VALUES(?,?,?,?,?,?)', (source['ordinal'], enrollment_id, call_id, item_id, capture_mode, now()))
            return capture_mode

    def note_correction(self, enrollment_id, evidence_id, simulation_id=None):
        binding = self._active(enrollment_id)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            evidence = _one(db, "SELECT * FROM enrollment_evidence WHERE id=? AND session_id=? AND status='pending'", (evidence_id, enrollment_id))
            if not evidence:
                raise HTTPException(409, 'A correction must be a pending spoken-review proposal from this enrollment.')
            if simulation_id:
                simulation = _one(db, 'SELECT * FROM simulation_runs WHERE id=? AND session_id=?', (simulation_id, binding['teaching_session_id']))
            else:
                simulation = _one(db, 'SELECT * FROM simulation_runs WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT 1', (binding['teaching_session_id'],))
            if not simulation:
                raise HTTPException(409, 'Run a simulation before correcting its response.')
            provenance = _one(db, 'SELECT source_ordinal FROM enrollment_evidence_provenance WHERE evidence_id=? AND session_id=?', (evidence_id, enrollment_id))
            source = _one(db, 'SELECT created FROM enrollment_audio_turns WHERE ordinal=? AND session_id=?', (provenance['source_ordinal'], enrollment_id)) if provenance else None
            if not source or source['created'] <= simulation['created_at']:
                raise HTTPException(409, 'The spoken correction must follow the simulation response it corrects.')
            db.execute('INSERT OR IGNORE INTO enrollment_learning_correction_targets VALUES(?,?,?,?,?)', (evidence_id, enrollment_id, simulation['id'], simulation['turn_id'], now()))
            prior = _one(db, 'SELECT * FROM enrollment_learning_correction_targets WHERE evidence_id=?', (evidence_id,))
            if prior['simulation_id'] != simulation['id']:
                raise HTTPException(409, 'This spoken correction already refers to an earlier simulation.')
            return prior

    def _turn(self, db, binding, source, capture_mode):
        ident = _stable(binding['enrollment_id'], 'audio', source['ordinal'])
        existing = _one(db, 'SELECT * FROM conversation_turns WHERE id=?', (ident,))
        if existing:
            return existing
        index = db.execute('SELECT COALESCE(MAX(turn_index),-1)+1 FROM conversation_turns WHERE session_id=?', (binding['teaching_session_id'],)).fetchone()[0]
        metadata = {'capture_mode': capture_mode, 'source': 'trusted_enrollment_audio', 'enrollment_id': binding['enrollment_id'], 'source_ordinal': source['ordinal'], 'provider_call_id': source['call_id'], 'provider_item_id': source['item_id'], 'synthetic': False}
        db.execute('''INSERT INTO conversation_turns(id,session_id,profile_id,consent_id,turn_index,role,transcript,transcript_state,started_at,ended_at,provider_metadata_json,external_id,created_at)
            VALUES(?,?,?,?,?,'trainer',?,'verified',?,?,?,?,?)''', (ident, binding['teaching_session_id'], binding['profile_id'], binding['consent_id'], index, source['transcript'], source['created'], source['created'], _json(metadata), 'enrollment-audio:' + str(source['ordinal']), source['created']))
        self._event(db, binding['teaching_session_id'], 'trainer.turn_completed', source['ordinal'], {'turn_id': ident, 'turn_index': index, 'enrollment_id': binding['enrollment_id']})
        return _one(db, 'SELECT * FROM conversation_turns WHERE id=?', (ident,))

    def _rule(self, trusted, prior=None, target_rules=None):
        payload = trusted['payload']
        # Normalize the exact reviewed interpretation locally, never a hidden
        # external inference. Unknown rules keep their actual confirmed wording.
        extraction_context = {'purpose': 'normalize_explicit_spoken_correction' if prior or trusted['target'] else 'reviewed_enrollment_evidence', 'correction_target_rules': target_rules or []}
        proposals = self.normalizer.analyze(payload['interpretation'], extraction_context)
        wanted = prior['rule_key'] if prior else None
        matching = [r for r in proposals if r['key'] == wanted]
        proposal = matching[0] if matching else proposals[0] if proposals else None
        if proposal and proposal['key'] != 'explicit_correction':
            category, key, value = proposal['category'], proposal['key'], proposal['value']
        else:
            category = {'decision_rule': 'behavior', 'style_preference': 'delivery', 'response_pattern': 'conversation'}[trusted['evidence']['kind']]
            key = 'enrollment.rule.' + (prior['root_evidence_id'] if prior else trusted['evidence']['id'])
            value = {k: payload[k] for k in ('situation', 'interpretation', 'change_condition')}
        if prior:
            category, key = prior['category'], prior['rule_key']
        return self.learning._normalize({'category': category, 'key': key, 'value': value, 'confidence': 1.0})

    def sync_trusted(self, enrollment_id):
        binding = self._active(enrollment_id)
        imported = []
        for row in self.evidence.confirmed_rows(enrollment_id):
            trusted = _trusted(self.store, row['id'], enrollment_id, binding)
            if not trusted:
                continue
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                enrollment = _one(db, 'SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,))
                _active_enrollment(enrollment)
                if _one(db, 'SELECT * FROM enrollment_learning_evidence WHERE evidence_id=?', (row['id'],)):
                    continue
                # Revalidate trust in the write transaction, including status.
                live = _one(db, 'SELECT status FROM enrollment_evidence WHERE id=? AND session_id=?', (row['id'], enrollment_id))
                if not live or live['status'] != 'confirmed':
                    continue
                source = self._turn(db, binding, trusted['source'], trusted['capture_mode'])
                confirmation = self._turn(db, binding, trusted['confirmation'], trusted['capture_mode'])
                replaces = trusted['evidence']['provenance_replaces_id']
                prior = _one(db, 'SELECT * FROM enrollment_learning_evidence WHERE evidence_id=? AND enrollment_id=?', (replaces, enrollment_id)) if replaces else None
                if replaces and not prior:
                    # Preserve the root key even when enrollment existed before
                    # integration and its first pattern was already superseded.
                    old = _trusted(self.store, replaces, enrollment_id, binding, allow_superseded=True)
                    if old:
                        prior_rule = self._rule(old)
                        prior = {'category': prior_rule['category'], 'rule_key': prior_rule['key'], 'root_evidence_id': replaces}
                    else:
                        raise HTTPException(409, 'The original confirmed correction evidence is unavailable.')
                target_rules = []
                if trusted['target']:
                    run = _one(db, 'SELECT * FROM simulation_runs WHERE id=? AND session_id=?', (trusted['target']['simulation_id'], binding['teaching_session_id']))
                    if not run:
                        raise HTTPException(409, 'The correction response belongs to a different enrollment.')
                    target_rules = json.loads(run['context_json']).get('personal_rules', [])
                normalized_rule = self._rule(trusted, prior, target_rules)
                kind = 'correction' if replaces or trusted['target'] else 'confirmation'
                observation = self.learning._insert_observation(db, binding['profile_id'], source, normalized_rule, kind=kind, state='confirmed')
                db.execute('INSERT INTO enrollment_learning_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)', (row['id'], enrollment_id, binding['profile_id'], source['id'], confirmation['id'], observation['id'], normalized_rule['category'], normalized_rule['key'], prior['root_evidence_id'] if prior else row['id'], replaces, now()))
                if kind == 'correction':
                    for old in self.learning._eligible_observations(db, binding['profile_id']):
                        if old['id'] != observation['id'] and old['category'] == normalized_rule['category'] and old['key'] == normalized_rule['key'] and old['state'] != 'locked' and self.learning._strength(old) <= 60 and old['value_json'] != normalized_rule['value_json']:
                            db.execute('UPDATE observations SET superseded_at=? WHERE id=?', (now(), old['id']))
                    if trusted['target']:
                        target_turn = _one(db, 'SELECT * FROM conversation_turns WHERE id=? AND session_id=?', (trusted['target']['target_turn_id'], binding['teaching_session_id']))
                        if not target_turn or source['turn_index'] <= target_turn['turn_index']:
                            raise HTTPException(409, 'Spoken correction must follow its actual simulation response.')
                        rule_json = _json({'category': normalized_rule['category'], 'key': normalized_rule['key'], 'value': json.loads(normalized_rule['value_json'])})
                        db.execute('INSERT OR IGNORE INTO corrections VALUES(?,?,?,?,?,?,?,?,?,?)', (_stable(enrollment_id, 'correction', row['id']), binding['profile_id'], binding['teaching_session_id'], target_turn['id'], source['id'], binding['consent_id'], source['transcript'], rule_json, observation['id'], now()))
                    self._event(db, binding['teaching_session_id'], 'learning.correction_recorded', row['id'], {'enrollment_evidence_id': row['id'], 'source_turn_id': source['id'], 'target_turn_id': trusted['target']['target_turn_id'] if trusted['target'] else None})
                version = self.learning._create_version(db, binding['profile_id'])
                db.execute('UPDATE teaching_sessions SET active_profile_version_id=? WHERE id=?', (version['id'], binding['teaching_session_id']))
                self._event(db, binding['teaching_session_id'], 'learning.observation_created', row['id'], {'observation_id': observation['id'], 'enrollment_evidence_id': row['id']})
                self._event(db, binding['teaching_session_id'], 'learning.profile_version_created', version['id'], {'profile_version_id': version['id']})
                imported.append(row['id'])
        return {'schema_version': SCHEMA_VERSION, 'imported_evidence_ids': imported, **self.journey(enrollment_id)}

    def context(self, enrollment_id):
        binding = self._active(enrollment_id)
        self.sync_trusted(enrollment_id)
        return self.learning.compile_context(binding['profile_id'])

    def planner(self, enrollment_id):
        binding = self._active(enrollment_id)
        planned = self.learning.next_question(binding['profile_id'], binding['teaching_session_id'])
        if planned['action'] != 'introduce_variation':
            return planned
        # Enrollment has one durable session, not one session per authored case.
        # Coverage therefore comes from confirmed source topics, never merely
        # from suggesting a probe or recording a simulated customer's answer.
        coverage = set()
        for mapping, trusted in _mapped_evidence(self.store, binding).values():
            key = mapping['rule_key']
            if key == 'qualification.first_move':
                coverage.add('purpose-01')
            if key in ('preferred_language', 'code_switching'):
                coverage.add('language-01')
            if key == 'response.length':
                coverage.add('tone-01')
            topic = normalized(trusted['payload']['situation'] + ' ' + trusted['payload']['interpretation'])
            topics = {'uncertain-01': ('inventory', 'availability', 'متوفر', 'المخزون'),
                'budget-01': ('corrected budget', 'budget correction', 'تصحيح الميزانية'),
                'boundary-01': ('stop contact', 'remove my number', 'وقف الاتصال', 'شيل رقمي'),
                'privacy-01': ('number source', 'gave my number', 'مصدر الرقم', 'منو عطاكم'),
                'switch-01': ('switch language', 'continue in english', 'تغيير اللغة')}
            for scenario_id, cues in topics.items():
                if any(normalized(cue) in topic for cue in cues):
                    coverage.add(scenario_id)
        for scenario_id in ('purpose-01', 'uncertain-01', 'language-01', 'budget-01', 'tone-01', 'boundary-01', 'privacy-01', 'switch-01', 'purpose-02'):
            if scenario_id not in coverage:
                scenario = BY_ID[scenario_id]
                return {'action': 'introduce_variation', 'reason': 'Explore a gap beyond the topics supported by spoken-confirmed evidence.', 'scenario_id': scenario_id, 'family': scenario['family'], 'question': f'خلنا نجرب موقف: العميل يقول «{scenario["caller"]}». كيف ترد بطريقتك؟', 'coverage_basis': 'spoken_confirmed_evidence'}
        return {'action': 'continue_listening', 'reason': 'Ask for a fresh professional example after the confirmed scenario topics are covered.', 'question': 'احكي لي عن موقف مختلف مع عميل، وشو اللي خلاك تغير طريقتك معه.'}

    def journey(self, enrollment_id):
        enrollment = self.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,))
        if not enrollment:
            raise HTTPException(404, 'Enrollment not found.')
        binding = self.store.one('SELECT * FROM enrollment_learning_bindings WHERE enrollment_id=?', (enrollment_id,))
        if enrollment['revoked_at'] or (binding and binding['revoked_at']):
            return {'schema_version': SCHEMA_VERSION, 'contract': SCHEMA_VERSION, 'state': 'revoked', 'enabled': False, 'profile_id': binding['profile_id'] if binding else None, 'session_id': binding['teaching_session_id'] if binding else None, 'profile_version_id': None}
        binding = binding or self.ensure_binding(enrollment_id)
        check_enrollment_binding(self.store, binding['profile_id'])
        session = self.store.one('SELECT * FROM teaching_sessions WHERE id=?', (binding['teaching_session_id'],))
        version = self.store.one('SELECT id,version_number FROM agent_profile_versions WHERE profile_id=? ORDER BY version_number DESC LIMIT 1', (binding['profile_id'],))
        return {'schema_version': SCHEMA_VERSION, 'contract': SCHEMA_VERSION, 'state': 'ready', 'enabled': True, 'profile_id': binding['profile_id'], 'session_id': binding['teaching_session_id'], 'profile_version_id': version['id'] if version else None, 'version_number': version['version_number'] if version else 0, 'mode': session['mode'], 'confirmed_evidence_count': len(_mapped_evidence(self.store, binding)), 'weight_training': False, 'consent_scope': 'private_voice_enrollment'}

    def start_simulation(self, enrollment_id, scenario_id=None, *, external_id=None):
        binding = self._active(enrollment_id)
        _active_enrollment(self.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,)), external=True)
        self.sync_trusted(enrollment_id)
        return self.simulation.start(binding['teaching_session_id'], scenario_id)

    def retry_simulation(self, enrollment_id, *, external_id=None):
        binding = self._active(enrollment_id)
        _active_enrollment(self.store.one('SELECT * FROM enrollment_sessions WHERE id=?', (enrollment_id,)), external=True)
        self.sync_trusted(enrollment_id)
        latest = self.store.one('SELECT id FROM simulation_runs WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT 1', (binding['teaching_session_id'],))
        if not latest:
            raise HTTPException(409, 'Run a simulation before retrying it.')
        return self.simulation.retry(latest['id'])

    def continue_teaching(self, enrollment_id):
        binding = self._active(enrollment_id)
        with self.store.db() as db:
            db.execute("UPDATE teaching_sessions SET mode='teaching' WHERE id=?", (binding['teaching_session_id'],))
            self._event(db, binding['teaching_session_id'], 'simulation.ended', _stable(enrollment_id, 'continue', now()), {'enrollment_id': enrollment_id})
        return self.journey(enrollment_id)

    def revoke(self, enrollment_id):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            enrollment = _one(db, 'SELECT revoked_at FROM enrollment_sessions WHERE id=?', (enrollment_id,))
            if not enrollment or not enrollment['revoked_at']:
                raise HTTPException(409, 'Revoke the original enrollment permission first.')
            binding = _one(db, 'SELECT * FROM enrollment_learning_bindings WHERE enrollment_id=?', (enrollment_id,))
            if binding:
                stamp = enrollment['revoked_at']
                db.execute('UPDATE enrollment_learning_bindings SET revoked_at=COALESCE(revoked_at,?) WHERE enrollment_id=?', (stamp, enrollment_id))
                db.execute('UPDATE consents SET withdrawn_at=COALESCE(withdrawn_at,?) WHERE id=?', (stamp, binding['consent_id']))
                db.execute("UPDATE teaching_sessions SET status='ended',ended_at=COALESCE(ended_at,?) WHERE id=?", (stamp, binding['teaching_session_id']))
                self._event(db, binding['teaching_session_id'], 'session.ended', enrollment_id, {'reason': 'enrollment_consent_revoked'})
        return self.journey(enrollment_id)


def install(app):
    app.state.enrollment_runtime = True
    app.state.enrollment_learning = EnrollmentLearningBridge(app.state.store, app.state.learning, app.state.simulation, app.state.enrollment_evidence)
