"""Synthetic provider streams only; these tests make no paid or network calls."""
import asyncio
import json

import pytest

from studio.app import Store, now
from studio.enrollment import SCHEMA as BASE_SCHEMA
from studio.enrollment_evidence import EvidenceService, RealtimeEvidenceBridge


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    with store.db() as db:
        db.executescript(BASE_SCHEMA)
    owner = store.create_user("Synthetic owner", "admin")
    for sid in ("session-one", "session-two"):
        store.execute("INSERT INTO enrollment_sessions(id,owner_id,state,consent_version,consent_json,created,updated) VALUES(?,?,?,?,?,?,?)", (sid, owner["id"], "collecting", "synthetic", '{"external_processing":true}', now(), now()))
        store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (sid, "rtc_" + sid, "open", now(), now()))
    service = EvidenceService(store)
    return store, service, "session-one", "rtc_session-one"


def audio(service, sid, call, item, text):
    service.mark_audio(sid, call, item)
    service.mark_audio(sid, call, item, committed=True)
    return service.record_transcript(sid, call, item, text)


def propose(service, sid, call, tool="tool-one", replaces="", interpretation="Confirm the corrected budget."):
    return service.propose(sid, call, tool, {"kind": "decision_rule", "situation": "A caller corrects a number.", "interpretation": interpretation, "change_condition": "Ask again if uncertain.", "replaces_id": replaces})


def approved(service, sid, call, suffix="one", replaces=""):
    audio(service, sid, call, "source-" + suffix, "My synthetic answer is to confirm the corrected budget.")
    result = propose(service, sid, call, "tool-" + suffix, replaces)
    assert result["ok"]
    assert service.verify_readback(sid, call, result["challenge_nonce"], result["challenge_text"])
    assert audio(service, sid, call, "confirmation-" + suffix, "Yes, save this.")["status"] == "confirmed"
    return result["evidence_id"]


def test_requires_provider_vad_commit_and_complete_transcript(env):
    store, service, sid, call = env
    assert service.record_transcript(sid, call, "forged", "Yes, save this.") is None
    service.mark_audio(sid, call, "forged", committed=True)
    assert service.record_transcript(sid, call, "forged", "Yes, save this.") is None
    assert store.all("SELECT * FROM enrollment_transcripts") == []
    assert not propose(service, sid, call)["ok"]


def test_readback_and_fresh_explicit_human_phrase_are_both_required(env):
    _, service, sid, call = env
    audio(service, sid, call, "demo", "Check whether the correction changed the budget.")
    proposal = propose(service, sid, call)
    assert audio(service, sid, call, "early-yes", "Yes, save this.") is None
    assert not service.verify_readback(sid, call, proposal["challenge_nonce"], "Something else. Yes, save this.")
    assert not service.verify_readback(sid, call, proposal["challenge_nonce"], "Confirm the corrected budget.")
    assert service.verify_readback(sid, call, proposal["challenge_nonce"], proposal["challenge_text"])
    assert audio(service, sid, call, "ambiguous", "Yes but that number is wrong") is None
    assert service.confirmed_rows(sid) == []
    assert audio(service, sid, call, "explicit", "Yes, save this!")["status"] == "confirmed"
    assert len(service.confirmed_rows(sid)) == 1


def test_speech_started_before_readback_cannot_confirm_when_transcript_arrives_late(env):
    _, service, sid, call = env
    audio(service, sid, call, "demo", "Confirm the revised amount.")
    proposal = propose(service, sid, call)
    service.mark_audio(sid, call, "too-early")
    assert service.verify_readback(sid, call, proposal["challenge_nonce"], proposal["challenge_text"])
    service.mark_audio(sid, call, "too-early", committed=True)
    assert service.record_transcript(sid, call, "too-early", "Yes save this") is None
    assert service.confirmed_rows(sid) == []


def test_arabic_is_preserved_and_confirmation_handles_punctuation_and_diacritics(env):
    _, service, sid, call = env
    source = "لا، مليون ونص، مش مليونين."
    audio(service, sid, call, "ar-demo", source)
    proposal = propose(service, sid, call, interpretation="أتأكد من الميزانية بعد التصحيح.")
    assert service.verify_readback(sid, call, proposal["challenge_nonce"], proposal["challenge_text"])
    assert audio(service, sid, call, "ar-confirm", "نَعَمْ، احْفَظْ هَذا.")["status"] == "confirmed"
    row = service.confirmed_rows(sid)[0]
    assert json.loads(row["payload"])["source_transcript"] == source


def test_confirmation_cannot_be_replayed_or_cross_session(env):
    store, service, sid, call = env
    ident = approved(service, sid, call)
    audio(service, sid, call, "second-source", "I ask about the location.")
    second = propose(service, sid, call, "second-tool")
    assert not service.verify_readback("session-two", "rtc_session-two", second["challenge_nonce"], second["challenge_text"])
    assert service.verify_readback(sid, call, second["challenge_nonce"], second["challenge_text"])
    assert service.record_transcript(sid, call, "confirmation-one", "Yes save this") is None
    assert [x["id"] for x in service.confirmed_rows(sid)] == [ident]
    assert not propose(service, "session-two", "rtc_session-two", replaces=ident)["ok"]


def test_correction_supersedes_only_after_approval_and_leaves_voice_unchanged(env):
    store, service, sid, call = env
    first = approved(service, sid, call)
    store.execute("UPDATE enrollment_sessions SET voice_id='voice-existing' WHERE id=?", (sid,))
    audio(service, sid, call, "correction", "I now confirm budget before location.")
    replacement = propose(service, sid, call, "correction-tool", first)
    assert [x["id"] for x in service.confirmed_rows(sid)] == [first]
    assert service.verify_readback(sid, call, replacement["challenge_nonce"], replacement["challenge_text"])
    audio(service, sid, call, "correction-confirm", "Yes save this")
    assert [x["id"] for x in service.confirmed_rows(sid)] == [replacement["evidence_id"]]
    assert store.one("SELECT status FROM enrollment_evidence WHERE id=?", (first,))["status"] == "superseded"
    assert store.one("SELECT voice_id FROM enrollment_sessions WHERE id=?", (sid,))["voice_id"] == "voice-existing"


def test_rejection_preserves_original_and_untrusted_legacy_rows_are_excluded(env):
    store, service, sid, call = env
    first = approved(service, sid, call)
    audio(service, sid, call, "correction", "Try a different interpretation.")
    replacement = propose(service, sid, call, "correction-tool", first)
    service.verify_readback(sid, call, replacement["challenge_nonce"], replacement["challenge_text"])
    assert audio(service, sid, call, "reject", "No")["status"] == "rejected"
    store.execute("INSERT INTO enrollment_evidence VALUES(?,?,?,?,?,'confirmed',?,?,?)", ("legacy", sid, "browser", "decision_rule", '{}', 'yes', now(), now()))
    assert [x["id"] for x in service.confirmed_rows(sid)] == [first]


def test_revocation_and_replaced_call_block_late_provider_events(env):
    store, service, sid, call = env
    audio(service, sid, call, "demo", "Check the number.")
    proposal = propose(service, sid, call)
    store.execute("UPDATE enrollment_sessions SET revoked_at=? WHERE id=?", (now(), sid))
    assert not service.verify_readback(sid, call, proposal["challenge_nonce"], proposal["challenge_text"])
    assert audio(service, sid, call, "late", "Yes save this") is None
    store.execute("UPDATE enrollment_sessions SET revoked_at=NULL WHERE id=?", (sid,))
    store.execute("UPDATE enrollment_realtime_calls SET call_id='rtc_replacement' WHERE session_id=?", (sid,))
    assert audio(service, sid, call, "late-again", "Yes save this") is None
    assert not propose(service, sid, call)["ok"]


def test_proposal_idempotency_is_namespaced_and_one_per_source_turn(env):
    _, service, sid, call = env
    audio(service, sid, call, "demo", "My answer.")
    first = propose(service, sid, call)
    assert propose(service, sid, call)["evidence_id"] == first["evidence_id"]
    assert not propose(service, sid, call, "another-tool")["ok"]
    audio(service, "session-two", "rtc_session-two", "demo", "Another person's answer.")
    second = propose(service, "session-two", "rtc_session-two")
    assert second["evidence_id"] != first["evidence_id"]


def test_bridge_uses_provider_response_done_and_ignores_browser_style_tool_flags(env):
    store, service, sid, call = env
    async def failed(*_):
        raise AssertionError("no transport used")
    bridge = RealtimeEvidenceBridge(store, service, on_failure=failed)
    for event in ({"type": "input_audio_buffer.speech_started", "item_id": "audio-one"}, {"type": "input_audio_buffer.committed", "item_id": "audio-one"}, {"type": "conversation.item.input_audio_transcription.completed", "item_id": "audio-one", "transcript": "I confirm the corrected number."}):
        bridge.consume(sid, call, event)
    args = {"kind": "decision_rule", "situation": "Budget correction", "interpretation": "Confirm the corrected budget.", "change_condition": "When unclear, ask.", "replaces_id": ""}
    messages = bridge.consume(sid, call, {"type": "response.done", "response": {"status": "completed", "output": [{"type": "function_call", "name": "propose_evidence", "call_id": "tool-one", "arguments": json.dumps(args)}]}})
    metadata = messages[-1]["response"]["metadata"]
    bridge.consume(sid, call, {"type": "response.function_call_arguments.done", "name": "confirm_evidence", "arguments": '{"accepted":true}'})
    assert service.confirmed_rows(sid) == []
    bridge.consume(sid, call, {"type": "response.done", "response": {"id": "resp_verbatim_review", "status": "completed", "metadata": metadata, "output": [{"type": "message", "status": "completed", "role": "assistant", "content": [{"type": "audio", "transcript": "Budget correction Confirm the corrected budget. When unclear, ask. Say Yes, save this."}]}]}})
    audio(service, sid, call, "confirm", "Yes save this")
    assert len(service.confirmed_rows(sid)) == 1


@pytest.mark.parametrize('review,approval', [
    ('I check the corrected amount with the customer before continuing, and ask again if it is unclear. If that is correct, say Yes, save this.', 'Yes, save this.'),
    ('لو العميل عدل ميزانيته، اتأكد منه على الرقم الجديد قبل ما اكمل، واسأله مرة ثانية لو مو واضح. إذا هذا صحيح، قل نعم احفظ هذا.', 'نعم احفظ هذا'),
])
def test_paraphrased_provider_review_becomes_authority_only_after_fresh_human_audio(env, review, approval):
    store, service, sid, call = env
    audio(service, sid, call, 'demonstration', 'I ask the customer to repeat their corrected budget.')
    proposal = propose(service, sid, call)
    assert audio(service, sid, call, 'premature-approval', approval) is None
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], review, response_id='resp_actual_review')
    assert service.confirmed_rows(sid) == []
    assert service.pending_review(sid) == {'status': 'review_ready', 'confirmation_phrase': 'نعم احفظ هذا' if 'نعم' in review else 'Yes, save this', 'can_confirm': True}
    assert audio(service, sid, call, 'fresh-approval', approval)['status'] == 'confirmed'
    payload = json.loads(service.confirmed_rows(sid)[0]['payload'])
    assert payload['interpretation'] == review
    assert payload['draft_proposal']['interpretation'] == 'Confirm the corrected budget.'
    proof = payload['spoken_review']
    assert proof['response_id'] == 'resp_actual_review'
    assert proof['call_id'] == call and proof['challenge_nonce'] == proposal['challenge_nonce']
    assert proof['spoken_after_ordinal'] == 2 and proof['verbatim'] is False


def test_duplicate_review_cannot_move_boundary_or_replace_words_awaiting_approval(env):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I confirm the corrected amount.')
    proposal = propose(service, sid, call)
    review = 'I check the new amount before moving to another question. If that is correct, say Yes, save this.'
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], review, response_id='resp_first')
    service.mark_audio(sid, call, 'approval-started')
    original = store.one('SELECT payload FROM enrollment_evidence WHERE id=?', (proposal['evidence_id'],))['payload']
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], 'Another unrelated response. Say Yes, save this.', response_id='resp_duplicate')
    service.readback_outcome(sid, call, proposal['challenge_nonce'], 'readback_incomplete')
    assert store.one('SELECT payload FROM enrollment_evidence WHERE id=?', (proposal['evidence_id'],))['payload'] == original
    service.mark_audio(sid, call, 'approval-started', committed=True)
    assert service.record_transcript(sid, call, 'approval-started', 'Yes, save this.')['status'] == 'confirmed'


@pytest.mark.parametrize('review', [
    'Say Yes, save this.',
    'If that is correct, say Yes, save this. Otherwise, please correct me.',
    'إذا هذا صحيح، قل نعم احفظ هذا. وإذا لا، صحح لي.',
    'What would you ask the customer first? If that is correct, say Yes, save this.',
    'كيف ترد على العميل؟ إذا هذا صحيح، قل نعم احفظ هذا.',
    'Please confirm this proposal. Say Yes, save this.',
    'I am ready to save your example. Say Yes, save this.',
    'يرجى تاكيد هذا الملخص. قل نعم احفظ هذا.',
    'انا جاهز لحفظ مثالك. قل نعم احفظ هذا.',
    'راجع هذا الاقتراح معي. قل نعم احفظ هذا.',
    'اذا كان هذا ما تريده، قل نعم احفظ هذا.',
    'I would like your approval for the proposed response. Say Yes, save this.',
    'Please review my response carefully before saving it. Say Yes, save this.',
    'x' * 1401 + ' Yes, save this.',
])
def test_cue_only_question_only_or_unbounded_readback_cannot_unlock_confirmation(env, review):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I verify the changed budget.')
    proposal = propose(service, sid, call)
    assert not service.verify_readback(sid, call, proposal['challenge_nonce'], review, response_id='resp_invalid_review')
    assert service.pending_review(sid)['status'] == 'readback_mismatch'
    assert audio(service, sid, call, 'approval', 'Yes, save this.') is None
    assert store.one('SELECT spoken_after_ordinal FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['spoken_after_ordinal'] is None


@pytest.mark.parametrize('response_status,item_status,role,content_type,expected', [
    ('cancelled', 'completed', 'assistant', 'output_audio', 'readback_incomplete'),
    ('incomplete', 'completed', 'assistant', 'output_audio', 'readback_incomplete'),
    ('failed', 'completed', 'assistant', 'output_audio', 'readback_incomplete'),
    ('completed', 'incomplete', 'assistant', 'output_audio', 'readback_incomplete'),
    ('completed', 'completed', 'assistant', 'output_text', 'readback_mismatch'),
    ('completed', 'completed', 'user', 'output_audio', 'readback_mismatch'),
])
def test_bridge_requires_completed_assistant_audio_for_the_correlated_challenge(env, response_status, item_status, role, content_type, expected):
    store, service, sid, call = env
    async def failed(*_):
        raise AssertionError('No real transport is used.')
    bridge = RealtimeEvidenceBridge(store, service, on_failure=failed)
    audio(service, sid, call, 'demo', 'I verify the changed budget.')
    proposal = propose(service, sid, call)
    review = 'I confirm the changed amount before asking about location. If that is correct, say Yes, save this.'
    bridge.consume(sid, call, {'type': 'response.done', 'response': {
        'id': 'resp_review', 'status': response_status, 'metadata': {'raneen_challenge': proposal['challenge_nonce']},
        'output': [{'type': 'message', 'status': item_status, 'role': role, 'content': [{'type': content_type, 'transcript': review}]}],
    }})
    assert service.pending_review(sid) == {'status': expected, 'confirmation_phrase': 'Yes, save this', 'can_confirm': False}
    assert audio(service, sid, call, 'approval', 'Yes, save this.') is None


def test_paraphrased_review_requires_call_nonce_and_response_identity(env):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I verify the changed budget.')
    proposal = propose(service, sid, call)
    review = 'I confirm the changed amount before asking about location. If that is correct, say Yes, save this.'
    assert not service.verify_readback(sid, call, proposal['challenge_nonce'], review)
    assert not service.verify_readback(sid, 'another-call', proposal['challenge_nonce'], review, response_id='resp_wrong_call')
    assert not service.verify_readback('session-two', 'rtc_session-two', proposal['challenge_nonce'], review, response_id='resp_wrong_person')
    assert not service.verify_readback(sid, call, 'another-nonce', review, response_id='resp_wrong_nonce')
    assert service.confirmed_rows(sid) == []
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], review, response_id='resp_right')


def test_pending_review_projection_has_no_private_content_or_provider_ids(env):
    _, service, sid, call = env
    assert service.pending_review(sid) is None
    audio(service, sid, call, 'demo', 'Synthetic private content must stay server-side.')
    proposal = propose(service, sid, call)
    service.readback_outcome(sid, call, proposal['challenge_nonce'], 'Private provider error text')
    projection = service.pending_review(sid)
    assert projection == {'status': 'awaiting_readback', 'confirmation_phrase': 'Yes, save this', 'can_confirm': False}


@pytest.mark.parametrize('missing', ['response_id', 'item_status', 'audio_part_transcript'])
def test_correlated_provider_completion_cannot_use_legacy_or_partial_audio_fallback(env, missing):
    _, service, sid, call = env
    async def failed(*_):
        raise AssertionError('No real transport is used.')
    bridge = RealtimeEvidenceBridge(service.store, service, on_failure=failed)
    audio(service, sid, call, 'demo', 'I verify the changed budget.')
    proposal = propose(service, sid, call)
    response = {'id': 'resp_complete', 'status': 'completed', 'metadata': {'raneen_challenge': proposal['challenge_nonce']},
        'output': [{'type': 'message', 'status': 'completed', 'role': 'assistant',
            'content': [{'type': 'output_audio', 'transcript': proposal['challenge_text']}]}]}
    if missing == 'response_id':
        response.pop('id')
    elif missing == 'item_status':
        response['output'][0].pop('status')
    else:
        response['output'][0]['content'].append({'type': 'output_audio'})
    bridge.consume(sid, call, {'type': 'response.done', 'response': response})
    assert service.pending_review(sid)['can_confirm'] is False
    assert audio(service, sid, call, 'approval', 'Yes, save this.') is None


def test_repeat_tool_preserves_original_source_rotates_nonce_and_requires_new_completed_review(env):
    store, service, sid, call = env
    async def failed(*_):
        raise AssertionError('No real transport is used.')
    bridge = RealtimeEvidenceBridge(store, service, on_failure=failed)
    source = 'I confirm the customer\'s corrected amount before continuing.'
    audio(service, sid, call, 'original-demo', source)
    proposal = propose(service, sid, call)
    original = store.one('SELECT * FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))
    service.readback_outcome(sid, call, proposal['challenge_nonce'], 'readback_incomplete')
    audio(service, sid, call, 'repeat-request', 'Please repeat the unfinished review.')
    tool_event = {'type': 'response.done', 'response': {'status': 'completed', 'output': [
        {'type': 'function_call', 'name': 'repeat_pending_review', 'call_id': 'repeat-tool', 'arguments': '{}'}]}}
    messages = bridge.consume(sid, call, tool_event)
    new_nonce = messages[-1]['response']['metadata']['raneen_challenge']
    assert new_nonce != proposal['challenge_nonce']
    pending = store.one('SELECT * FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))
    assert pending['source_ordinal'] == original['source_ordinal']
    assert pending['challenge_text'] == original['challenge_text']
    payload = json.loads(store.one('SELECT payload FROM enrollment_evidence WHERE id=?', (proposal['evidence_id'],))['payload'])
    assert payload['source_transcript'] == source
    assert payload['repeat_review_tools'] == [{'tool_call_id': 'repeat-tool', 'request_ordinal': 2}]
    replay = bridge.consume(sid, call, tool_event)
    assert all(message['type'] != 'response.create' for message in replay)
    assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == new_nonce
    assert not service.verify_readback(sid, call, proposal['challenge_nonce'], proposal['challenge_text'], response_id='resp_late_old')
    assert audio(service, sid, call, 'premature-approval', 'Yes, save this.') is None
    bridge.consume(sid, call, {'type': 'response.done', 'response': {'id': 'resp_repeated', 'status': 'completed',
        'metadata': {'raneen_challenge': new_nonce}, 'output': [{'type': 'message', 'status': 'completed', 'role': 'assistant',
            'content': [{'type': 'output_audio', 'transcript': proposal['challenge_text']}]}]}})
    assert service.confirmed_rows(sid) == []
    assert audio(service, sid, call, 'fresh-approval', 'Yes, save this.')['status'] == 'confirmed'


def test_repeat_allows_only_one_new_challenge_per_human_request_and_twelve_per_example(env):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I confirm the amount.')
    proposal = propose(service, sid, call)
    nonce = proposal['challenge_nonce']
    for index in range(12):
        audio(service, sid, call, 'repeat-request-' + str(index), 'Please repeat the review.')
        repeated = service.repeat_pending_review(sid, call, 'repeat-tool-' + str(index))
        assert repeated['ok'] and repeated['challenge_nonce'] != nonce
        nonce = repeated['challenge_nonce']
        assert service.repeat_pending_review(sid, call, 'repeat-tool-' + str(index))['reused']
        assert not service.repeat_pending_review(sid, call, 'extra-tool-' + str(index))['ok']
        assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == nonce
    audio(service, sid, call, 'over-limit', 'Repeat the review again please.')
    assert not service.repeat_pending_review(sid, call, 'over-limit-tool')['ok']
    assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == nonce


@pytest.mark.parametrize('utterance', ['Yes, save this.', 'No', 'نعم احفظ هذا', 'لا'])
def test_confirmation_or_rejection_cannot_request_a_new_readback(env, utterance):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I confirm the amount.')
    proposal = propose(service, sid, call)
    assert not service.repeat_pending_review(sid, call, 'before-new-turn')['ok']
    audio(service, sid, call, 'not-repeat-request', utterance)
    assert not service.repeat_pending_review(sid, call, 'invalid-request')['ok']
    assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == proposal['challenge_nonce']


def test_repeat_requires_active_call_consent_and_a_completed_human_request(env):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I confirm the amount.')
    proposal = propose(service, sid, call)
    service.mark_audio(sid, call, 'uncommitted-request')
    assert not service.repeat_pending_review(sid, call, 'uncommitted-tool')['ok']
    audio(service, sid, call, 'valid-repeat-request', 'Repeat the summary please.')
    assert not service.repeat_pending_review(sid, 'wrong-call', 'wrong-call-tool')['ok']
    store.execute("UPDATE enrollment_sessions SET consent_json='{}' WHERE id=?", (sid,))
    assert not service.repeat_pending_review(sid, call, 'no-consent-tool')['ok']
    store.execute("UPDATE enrollment_sessions SET consent_json='{\"external_processing\":true}',revoked_at=? WHERE id=?", (now(), sid))
    assert not service.repeat_pending_review(sid, call, 'revoked-tool')['ok']
    assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == proposal['challenge_nonce']


def test_completed_review_cannot_be_repeated_or_unfrozen(env):
    store, service, sid, call = env
    audio(service, sid, call, 'demo', 'I confirm the amount.')
    proposal = propose(service, sid, call)
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], proposal['challenge_text'], response_id='resp_complete')
    before = store.one('SELECT * FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))
    audio(service, sid, call, 'late-repeat-request', 'Repeat the review please.')
    result = service.repeat_pending_review(sid, call, 'late-repeat-tool')
    assert not result['ok'] and 'already completed' in result['error']
    assert store.one('SELECT * FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],)) == before


@pytest.mark.parametrize('review', [
    'I ask “What is your budget?” first, because it tells me which homes fit. If that is correct, say Yes, save this.',
    'اول سؤال عندي “شو ميزانيتك؟” عشان اعرف اي عقار يناسب العميل. إذا هذا صحيح، قل نعم احفظ هذا.',
])
def test_explained_customer_question_is_a_substantive_review(env, review):
    _, service, sid, call = env
    audio(service, sid, call, 'demo', 'I ask the customer their budget.')
    proposal = propose(service, sid, call)
    assert service.verify_readback(sid, call, proposal['challenge_nonce'], review, response_id='resp_question_explanation')


def test_quoted_question_alone_still_cannot_unlock_review(env):
    _, service, sid, call = env
    audio(service, sid, call, 'demo', 'I ask the customer their budget.')
    proposal = propose(service, sid, call)
    assert not service.verify_readback(sid, call, proposal['challenge_nonce'], '“What is your budget?” Say Yes, save this.', response_id='resp_only_question')


@pytest.mark.parametrize('arguments', ['[]', 'null', '{"review":"invented"}'])
def test_repeat_voice_tool_has_no_client_supplied_review_or_parameters(env, arguments):
    store, service, sid, call = env
    async def failed(*_):
        raise AssertionError('No real transport is used.')
    bridge = RealtimeEvidenceBridge(store, service, on_failure=failed)
    audio(service, sid, call, 'demo', 'I confirm the amount.')
    proposal = propose(service, sid, call)
    audio(service, sid, call, 'request', 'Repeat the summary please.')
    outgoing = bridge.consume(sid, call, {'type': 'response.done', 'response': {'status': 'completed', 'output': [
        {'type': 'function_call', 'name': 'repeat_pending_review', 'call_id': 'invalid-tool', 'arguments': arguments}]}})
    assert not json.loads(outgoing[0]['item']['output'])['ok']
    assert not any((message.get('response') or {}).get('metadata') for message in outgoing)
    assert store.one('SELECT challenge_nonce FROM enrollment_evidence_provenance WHERE evidence_id=?', (proposal['evidence_id'],))['challenge_nonce'] == proposal['challenge_nonce']


def test_sideband_disconnect_hangs_up_and_explicit_close_does_not_repeat(env):
    store, service, sid, call = env
    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.closed = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item
        async def close(self):
            self.closed = True
        async def send(self, value):
            pass
    async def scenario():
        failures, connects, sockets = [], [], []
        async def connector(url, **kwargs):
            connects.append((url, kwargs))
            sock = Socket()
            sockets.append(sock)
            return sock
        async def failed(*args):
            failures.append(args)
        bridge = RealtimeEvidenceBridge(store, service, on_failure=failed, connector=connector)
        await bridge.attach(sid, call, "synthetic-private-key")
        assert connects[0][0].endswith("call_id=" + call)
        assert connects[0][1]["additional_headers"] == {"Authorization": "Bearer synthetic-private-key"}
        await sockets[0].queue.put(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert failures == [(sid, call)] and sockets[0].closed
        await bridge.attach(sid, call, "synthetic-private-key")
        await bridge.close(sid, "wrong-call")
        assert sid in bridge.tasks
        await bridge.close(sid, call)
        assert failures == [(sid, call)] and not bridge.tasks
    asyncio.run(scenario())
