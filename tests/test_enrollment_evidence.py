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
    bridge.consume(sid, call, {"type": "response.done", "response": {"status": "completed", "metadata": metadata, "output": [{"type": "message", "role": "assistant", "content": [{"type": "audio", "transcript": "Budget correction Confirm the corrected budget. When unclear, ask. Say Yes, save this."}]}]}})
    audio(service, sid, call, "confirm", "Yes save this")
    assert len(service.confirmed_rows(sid)) == 1


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
