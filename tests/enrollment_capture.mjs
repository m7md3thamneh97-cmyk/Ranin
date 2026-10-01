// Synthetic browser-media lifecycle tests. No physical microphone or provider calls.
// Run: node --test tests/enrollment_capture.mjs
import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {workflowFailure} from '../studio/static/enrollment-errors.js';
import {copy} from '../studio/static/enrollment-copy.js';

const {ChunkQueue, InterviewCapture} = await import('../studio/static/enrollment-capture.js');

test('voice preparation shows measured audible shortfall while preserving the saved-recording explanation', () => {
  const error={detail:{code:'insufficient_audio',message:'Need more audio',details:{selected_active_ms:49000,minimum_active_ms:60000,decoded_source_ms:123000}}};
  for(const lang of ['en','ar']){
    const text=workflowFailure(error,key=>copy[lang][key],'creatingVoice');
    assert.ok(text.includes('49')&&text.includes('60'));
    assert.ok(!text.includes('{seconds}')&&!text.includes('{minimum}'));
    assert.notEqual(text,copy[lang].noExamples);
  }
});

test('provider and invalid synthesized-audio failures never request more contributor speech', () => {
  const t=key=>copy.en[key];
  for(const code of ['voice_preview_failed','invalid_voice_preview','preview_operation_failed']){
    assert.equal(workflowFailure({detail:{code,message:'Audio sample decoding failed'}},t,'makingSamples'),copy.en.sampleFailed);
  }
  assert.equal(workflowFailure({detail:'Generated audio could not be decoded'},t,'makingSamples'),copy.en.sampleFailed);
  assert.equal(workflowFailure({message:'empty_preview'},t,'makingSamples'),copy.en.sampleFailed);
  assert.equal(workflowFailure({detail:{code:'voice_clone_failed',message:'Speech provider rejected audio'}},t,'creatingVoice'),copy.en.cloneFailed);
});

test('missing decoder, corrupt saved audio and preparation timeout are distinct from shortfall', () => {
  const t=key=>copy.en[key];
  assert.equal(workflowFailure({detail:{code:'decoder_unavailable'}},t),copy.en.audioCheckUnavailable);
  assert.equal(workflowFailure({detail:{code:'invalid_audio'}},t),copy.en.audioReadFailed);
  assert.equal(workflowFailure({detail:{code:'audio_prepare_timeout'}},t),copy.en.audioCheckTimeout);
  assert.equal(workflowFailure({detail:{code:'audio_sequence_gap'}},t),copy.en.saveFirst);
  assert.equal(workflowFailure({detail:{code:'insufficient_audio',details:{decoded_source_ms:0,rejected_reasons:{invalid_audio:20}}}},t),copy.en.audioReadFailed);
});

test('uncertain provider operations and verified response evidence keep their own recovery paths', () => {
  const t=key=>copy.en[key];
  assert.equal(workflowFailure({detail:{code:'outcome_unknown',message:'Audio request timed out'}},t,'creatingVoice'),copy.en.outcomePending);
  assert.equal(workflowFailure({detail:'Confirmed evidence is required'},t,'buildingAgent'),copy.en.noExamples);
  assert.equal(workflowFailure({detail:'Need more clear contributor audio before preparing the voice'},t,'creatingVoice'),copy.en.moreSpeech);
});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const turn = () => new Promise(resolve => setImmediate(resolve));
const until = async (condition, message = 'condition did not become true') => {
  for (let i = 0; i < 100; i++) {
    if (condition()) return;
    await turn();
  }
  assert.fail(message);
};
const checksum = async blob => createHash('sha256').update(Buffer.from(await blob.arrayBuffer())).digest('hex');
const recording = text => new Blob([text], {type: 'audio/webm;codecs=opus'});
const captures = [];
afterEach(async () => {
  for (const capture of captures.splice(0)) {
    capture.queue.suspend();
    capture.stopLocal();
  }
  await turn();
});

function fakeStream() {
  const track = {stops: 0, stop() { this.stops++; }};
  return {track, active: true, getTracks: () => [track], getAudioTracks: () => [track]};
}

class FakeRecorder {
  static instances = [];
  static isTypeSupported() { return true; }
  constructor(stream, options = {}) {
    this.stream = stream;
    this.mimeType = options.mimeType || 'audio/webm';
    this.state = 'inactive';
    this.payload = recording('synthetic microphone bytes');
    FakeRecorder.instances.push(this);
  }
  start() { this.state = 'recording'; }
  stop() {
    if (this.state !== 'recording') return;
    this.state = 'inactive';
    queueMicrotask(() => {
      this.ondataavailable?.({data: this.payload});
      this.onstop?.();
    });
  }
}

class FakePeer {
  static instances = [];
  constructor() {
    this.connectionState = 'new';
    this.closed = false;
    this.channel = {readyState: 'open', send() {}, close() { this.readyState = 'closed'; }};
    FakePeer.instances.push(this);
  }
  addTrack() {}
  createDataChannel() { return this.channel; }
  async createOffer() { return {type: 'offer', sdp: 'synthetic-offer'}; }
  async setLocalDescription(value) { this.localDescription = value; }
  async setRemoteDescription(value) { this.remoteDescription = value; }
  close() { this.closed = true; this.connectionState = 'closed'; }
  disconnect() { this.connectionState = 'disconnected'; this.onconnectionstatechange?.(); }
}

test('sequence reservations survive a stale server refresh while an upload is in flight', async () => {
  const held = deferred(), sent = [];
  const queue = new ChunkQueue({sessionId: 'synthetic-session', nextSeq: 4, hash: checksum,
    upload: async item => { sent.push(item); if (sent.length === 1) await held.promise; return {seq: item.seq}; }});
  queue.add(recording('first'), 3000);
  const flushing = queue.flush();
  await until(() => sent.length === 1);
  queue.syncNextSeq(4);
  queue.add(recording('second'), 3000);
  assert.equal(queue.nextSeq, 6, 'refresh must not reuse an in-flight sequence');
  held.resolve();
  await flushing;
  await queue.flush();
  assert.deepEqual(sent.map(item => item.seq), [4, 5]);
  assert.equal(queue.hasPending, false);
});

test('a failed chunk retains the exact bytes, checksum and sequence for an explicit retry', async () => {
  const sent = [], blob = recording('retained synthetic audio');
  let fail = true;
  const queue = new ChunkQueue({sessionId: 'synthetic-session', hash: checksum,
    upload: async item => { sent.push({...item}); if (fail) throw Error('synthetic network outage'); return {seq: item.seq}; }});
  queue.add(blob, 3000);
  await queue.flush().catch(() => {});
  assert.equal(queue.hasPending, true);
  assert.equal(queue.pendingBytes, blob.size);
  fail = false;
  await queue.retry();
  assert.equal(sent.length, 2);
  assert.equal(sent[0].seq, sent[1].seq);
  assert.equal(sent[0].blob, blob);
  assert.equal(sent[1].blob, blob);
  assert.equal(sent[0].checksum, await checksum(blob));
  assert.equal(sent[1].checksum, sent[0].checksum);
  assert.equal(queue.hasPending, false);
  assert.equal(queue.pendingBytes, 0);
});

test('an incorrect acknowledgement cannot discard a local chunk', async () => {
  const queue = new ChunkQueue({sessionId: 'synthetic-session', hash: checksum,
    upload: async item => ({seq: item.seq + 1})});
  queue.add(recording('must remain pending'), 3000);
  assert.equal(await queue.flush(), false);
  assert.equal(queue.hasPending, true);
  assert.equal(queue.items[0].error.message, 'audio_ack_missing');
});

test('concurrent flush requests serialize uploads and cannot double-send a chunk', async () => {
  const held = deferred(), sent = [];
  const queue = new ChunkQueue({sessionId: 'synthetic-session', hash: checksum,
    upload: async item => { sent.push(item.seq); await held.promise; return {seq: item.seq}; }});
  queue.add(recording('first'), 3000);
  queue.add(recording('second'), 3000);
  const first = queue.flush(), second = queue.flush();
  assert.equal(first, second);
  await until(() => sent.length === 1);
  held.resolve();
  assert.equal(await first, true);
  assert.deepEqual(sent, [0, 1]);
});

test('queue capacity rejects overflow without advancing its sequence', () => {
  const queue = new ChunkQueue({sessionId: 'synthetic-session', hash: checksum,
    upload: async item => ({seq: item.seq}), maxChunks: 2, maxBytes: 10});
  queue.add(recording('12345'), 3000);
  assert.throws(() => queue.add(recording('123456'), 3000), /audio_buffer_full/);
  assert.equal(queue.nextSeq, 1);
  queue.add(recording('12345'), 3000);
  assert.throws(() => queue.add(recording(''), 3000), /audio_buffer_full/);
  assert.equal(queue.nextSeq, 2);
  assert.equal(queue.pendingBytes, 10);
});

test('a timed out upload aborts but retains the chunk for explicit retry', async () => {
  let signal;
  const queue = new ChunkQueue({sessionId: 'synthetic-session', hash: checksum, timeoutMs: 5,
    upload: async (_item, abortSignal) => { signal = abortSignal; return new Promise(() => {}); }});
  queue.add(recording('retained after timeout'), 3000);
  assert.equal(await queue.flush(), false);
  assert.equal(signal.aborted, true);
  assert.equal(queue.items[0].error.message, 'upload_timeout');
  assert.equal(queue.hasPending, true);
});

test('revocation during checksum calculation prevents a late upload and retains local bytes', async () => {
  const hashing = deferred(), blob = recording('cancelled before dispatch');
  let uploads = 0, hashStarted = false;
  const queue = new ChunkQueue({sessionId: 'synthetic-session',
    hash: () => { hashStarted = true; return hashing.promise; },
    upload: async item => { uploads++; return {seq: item.seq}; },
  });
  queue.add(blob, 3000);
  const flushing = queue.flush();
  await until(() => hashStarted);
  queue.suspend();
  hashing.resolve(await checksum(blob));
  assert.equal(await flushing, false);
  assert.equal(uploads, 0);
  assert.equal(queue.items[0].blob, blob);
  assert.equal(queue.pendingBytes, blob.size);
  assert.equal(queue.hasPending, true);
});

function harness(overrides = {}) {
  const errors = [], streams = [], states = [];
  const capture = new InterviewCapture({
    sessionId: 'synthetic-session', Recorder: FakeRecorder, Peer: FakePeer,
    hash: checksum, segmentMs: 60000,
    mediaDevices: {getUserMedia: async () => { const stream = fakeStream(); streams.push(stream); return stream; }},
    upload: async item => ({seq: item.seq}),
    openConnection: async () => 'synthetic-answer',
    closeConnection: async () => true,
    onError: error => errors.push(error),
    onState: value => states.push(value.state),
    ...overrides,
  });
  captures.push(capture);
  return {capture, errors, streams, states};
}

test('double start while permission is pending shares one microphone and one connection', async () => {
  const permission = deferred(), stream = fakeStream();
  let requests = 0;
  const {capture} = harness({mediaDevices: {getUserMedia: () => { requests++; return permission.promise; }}});
  const peerCount = FakePeer.instances.length;
  const first = capture.start(), second = capture.start();
  assert.equal(first, second);
  assert.equal(requests, 1);
  permission.resolve(stream);
  assert.equal(await first, true);
  assert.equal(FakePeer.instances.length, peerCount + 1);
  assert.equal(capture.state, 'live');
  await capture.pause();
});

test('microphone permission resolving after cancellation is stopped and never connected', async () => {
  const permission = deferred(), stream = fakeStream();
  const {capture} = harness({mediaDevices: {getUserMedia: () => permission.promise}});
  const peerCount = FakePeer.instances.length;
  const starting = capture.start();
  await capture.pause();
  permission.resolve(stream);
  assert.equal(await starting, false);
  assert.equal(stream.track.stops, 1);
  assert.equal(FakePeer.instances.length, peerCount);
  assert.equal(capture.state, 'paused');
  assert.equal(capture.stream, null);
});

test('a connection answer arriving after pause is closed again and cannot restart capture', async () => {
  const answer = deferred();
  let opened = false, closes = 0;
  const {capture, streams} = harness({
    openConnection: () => { opened = true; return answer.promise; },
    closeConnection: async () => { closes++; },
  });
  const recorderCount = FakeRecorder.instances.length;
  const starting = capture.start();
  await until(() => opened);
  const peer = capture.pc;
  await capture.pause();
  assert.equal(streams[0].track.stops, 1);
  assert.equal(peer.closed, true);
  answer.resolve('late synthetic answer');
  assert.equal(await starting, false);
  assert.equal(closes, 2, 'a late provider call must be closed after the first attempted hangup');
  assert.equal(peer.remoteDescription, undefined);
  assert.equal(capture.state, 'paused');
  assert.equal(FakeRecorder.instances.length, recorderCount);
});

test('pause releases tracks and peer immediately despite hanging audio and hangup requests', async () => {
  const upload = deferred(), hangup = deferred();
  const {capture, streams} = harness({
    upload: async item => { await upload.promise; return {seq: item.seq}; },
    closeConnection: () => hangup.promise,
  });
  await capture.start();
  const peer = capture.pc;
  const paused = capture.pause();
  assert.equal(streams[0].track.stops, 1);
  assert.equal(peer.closed, true);
  assert.equal(capture.stream, null);
  assert.equal(capture.state, 'paused');
  await until(() => capture.queue.hasPending);
  assert.equal(capture.hasPending, true, 'final bytes are visibly pending while the network is stuck');
  upload.resolve();
  hangup.resolve();
  assert.equal(await paused, true);
  assert.equal(capture.hasPending, false);
});

test('revoke-style pause releases local media and prevents final audio from being uploaded', async () => {
  let uploads = 0;
  const {capture, streams} = harness({upload: async item => { uploads++; return {seq: item.seq}; }});
  await capture.start();
  const peer = capture.pc;
  const pausing = capture.pause({save: false});
  assert.equal(streams[0].track.stops, 1);
  assert.equal(peer.closed, true);
  await pausing;
  assert.equal(uploads, 0);
  assert.equal(capture.queue.suspended, true);
  assert.equal(capture.queue.hasPending, true);
});

test('a sustained disconnect closes the old peer and allows a new connection after saved audio drains', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {capture, streams, errors} = harness();
  await capture.start();
  const oldPeer = capture.pc;
  oldPeer.disconnect();
  assert.equal(oldPeer.closed, false, 'a brief disruption must not hang up the call');
  t.mock.timers.tick(5000);
  assert.equal(oldPeer.closed, true);
  assert.equal(streams[0].track.stops, 1);
  await until(() => !capture.pausing);
  assert.equal(await capture.start(), true);
  assert.notEqual(capture.pc, oldPeer);
  assert.equal(capture.pc.closed, false);
  assert.equal(streams.length, 2);
  assert.equal(errors.some(error => error.message === 'connection_lost'), true);
  await capture.pause();
});

test('a transient disconnect recovers without stopping capture or creating another call', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']});
  let opens = 0, closes = 0;
  const {capture, streams, errors} = harness({
    openConnection: async () => { opens++; return 'synthetic-answer'; },
    closeConnection: async () => { closes++; return true; },
  });
  await capture.start();
  const peer = capture.pc, recorder = capture.recorder;
  peer.disconnect();
  t.mock.timers.tick(2000);
  peer.connectionState = 'connected';
  peer.onconnectionstatechange();
  t.mock.timers.tick(5000);
  assert.equal(capture.state, 'live');
  assert.equal(capture.pc, peer);
  assert.equal(capture.recorder, recorder);
  assert.equal(streams[0].track.stops, 0);
  assert.equal(opens, 1);
  assert.equal(closes, 0);
  assert.equal(errors.length, 0);
  await capture.pause();
});

test('pause cancels disconnect recovery so an old timer cannot end a resumed interview', async (t) => {
  t.mock.timers.enable({apis: ['setTimeout']});
  let closes = 0;
  const {capture, errors} = harness({closeConnection: async () => { closes++; return true; }});
  await capture.start();
  capture.pc.disconnect();
  await capture.pause();
  await capture.start();
  t.mock.timers.tick(5000);
  assert.equal(capture.state, 'live');
  assert.equal(closes, 1);
  assert.equal(errors.length, 0);
  await capture.pause();
});

test('the live interviewer sends and records only an echo-protected microphone stream', async () => {
  const stream = fakeStream(), sentTracks = [];
  let constraints;
  class RecordingPeer extends FakePeer {
    addTrack(track, source) { sentTracks.push({track, source}); }
  }
  const {capture} = harness({Peer: RecordingPeer,
    mediaDevices: {getUserMedia: async (requested) => { constraints = requested; return stream; }}});
  await capture.start();
  assert.equal(constraints.audio.echoCancellation, true);
  assert.equal(constraints.audio.noiseSuppression, true);
  assert.equal(constraints.audio.autoGainControl, false);
  assert.equal(constraints.video, false);
  assert.deepEqual(sentTracks, [{track: stream.track, source: stream}]);
  assert.equal(capture.recorder.stream, stream, 'record the protected mic, never the remote playback');
  await capture.pause();
});

test('slow uploads stop capture at a bounded backlog without dropping already captured bytes', async () => {
  const upload = deferred();
  const {capture, streams} = harness({upload: async item => { await upload.promise; return {seq: item.seq}; }});
  await capture.start();
  for (let i = 0; i < 6; i++) {
    capture.recorder.stop();
    await turn();
  }
  assert.equal(capture.state, 'paused');
  assert.equal(streams[0].track.stops, 1);
  assert.equal(capture.queue.items.length, 6);
  assert.equal(capture.queue.nextSeq, 6);
  assert.equal(capture.queue.pendingBytes <= capture.queue.maxBytes, true);
  upload.resolve();
  await until(() => !capture.pausing);
  assert.equal(capture.hasPending, false);
});

test('a failed final segment stays retryable and blocks resume until saved', async () => {
  let fail = true;
  const sent = [];
  const {capture} = harness({upload: async item => {
    sent.push({seq: item.seq, checksum: item.checksum, blob: item.blob});
    if (fail) throw Error('synthetic offline');
    return {seq: item.seq};
  }});
  await capture.start();
  assert.equal(await capture.pause(), false);
  assert.equal(capture.hasPending, true);
  await assert.rejects(capture.start(), /save_audio_first/);
  fail = false;
  assert.equal(await capture.retry(), true);
  assert.equal(capture.hasPending, false);
  assert.equal(sent.at(-1).blob, sent[0].blob);
  assert.equal(sent.at(-1).checksum, sent[0].checksum);
  assert.equal(sent.at(-1).seq, sent[0].seq);
});

test('permission denial leaves capture paused without creating a peer', async () => {
  const {capture} = harness({mediaDevices: {getUserMedia: async () => { throw new Error('NotAllowedError'); }}});
  const peerCount = FakePeer.instances.length;
  await assert.rejects(capture.start(), /NotAllowedError/);
  assert.equal(capture.state, 'paused');
  assert.equal(FakePeer.instances.length, peerCount);
  assert.equal(capture.stream, null);
});

test('async realtime event failures are reported without escaping as unhandled promises', async () => {
  const {capture, errors} = harness({onEvent: async () => { throw Error('synthetic event failure'); }});
  await capture.start();
  capture.dc.onmessage({data: JSON.stringify({type: 'synthetic.event'})});
  await until(() => errors.length > 0);
  assert.equal(errors[0].message, 'synthetic event failure');
  await capture.pause();
});

test('async connection-open failures are reported without escaping as unhandled promises', async () => {
  const {capture, errors} = harness({onEvent: async () => { throw Error('synthetic connection event failure'); }});
  await capture.start();
  capture.dc.onopen();
  await until(() => errors.length > 0);
  assert.equal(errors[0].message, 'synthetic connection event failure');
  await capture.pause();
});

test('an empty final recorder event cannot be reported as all audio saved', async () => {
  const {capture, errors, streams} = harness();
  await capture.start();
  capture.recorder.payload = recording('');
  assert.equal(await capture.pause(), false);
  assert.equal(streams[0].track.stops, 1);
  assert.equal(capture.hasPending, true, 'the UI must keep an unsaved-tail warning');
  assert.equal(errors.length > 0, true);
});

test('failure to construct the next recorder stops the live microphone safely', async () => {
  class FailingRecorder extends FakeRecorder {
    static constructed = 0;
    constructor(...args) {
      if (++FailingRecorder.constructed === 2) throw Error('synthetic recorder constructor failure');
      super(...args);
    }
  }
  const {capture, errors, streams} = harness({Recorder: FailingRecorder});
  await capture.start();
  capture.recorder.stop();
  await until(() => capture.state === 'paused');
  assert.equal(streams[0].track.stops, 1);
  assert.equal(errors.length > 0, true);
});

test('failure to start the next recorder stops the live microphone safely', async () => {
  class FailingRecorder extends FakeRecorder {
    static started = 0;
    start() {
      if (++FailingRecorder.started === 2) throw Error('synthetic recorder start failure');
      super.start();
    }
  }
  const {capture, errors, streams} = harness({Recorder: FailingRecorder});
  await capture.start();
  capture.recorder.stop();
  await until(() => capture.state === 'paused');
  assert.equal(streams[0].track.stops, 1);
  assert.equal(errors.length > 0, true);
});

test('synchronous realtime event failures also reach the error handler', async () => {
  const {capture, errors} = harness({onEvent: () => { throw Error('synthetic synchronous event failure'); }});
  await capture.start();
  assert.doesNotThrow(() => capture.dc.onmessage({data: JSON.stringify({type: 'synthetic.event'})}));
  assert.doesNotThrow(() => capture.dc.onopen());
  await until(() => errors.length === 2);
  assert.equal(errors.every(error => error.message === 'synthetic synchronous event failure'), true);
  await capture.pause();
});

test('a delayed old final event after acknowledged loss cannot replace the resumed recorder or cancel its timer', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  class DelayedRecorder extends FakeRecorder {
    stop() { this.state = 'inactive'; }
    deliverFinal() {
      this.ondataavailable?.({data: this.payload});
      this.onstop?.();
    }
  }
  const {capture} = harness({Recorder: DelayedRecorder, segmentMs: 60000});
  await capture.start();
  const oldRecorder = capture.recorder;
  const firstPause = capture.pause();
  t.mock.timers.tick(3001);
  assert.equal(await firstPause, false);
  assert.equal(capture.uncertainTail, true);
  assert.equal(capture.acknowledgeMissingTail(), true);
  assert.equal(await capture.start(), true);
  const resumedRecorder = capture.recorder;
  const recorderCount = FakeRecorder.instances.length;
  oldRecorder.deliverFinal();
  await until(() => !capture.queue.hasPending);
  assert.equal(capture.recorder, resumedRecorder);
  assert.equal(FakeRecorder.instances.length, recorderCount, 'old onstop must not create a concurrent recorder');
  assert.equal(resumedRecorder.state, 'recording');
  t.mock.timers.tick(60001);
  assert.equal(resumedRecorder.state, 'inactive', 'resumed segment timer must still stop its recorder');
  const finalPause = capture.pause();
  resumedRecorder.deliverFinal();
  assert.equal(await finalPause, true);
});

test('a recovered late final stays pending until saved, then permits resume', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  class DelayedRecorder extends FakeRecorder {
    stop() { this.state = 'inactive'; }
    deliverFinal() { this.ondataavailable?.({data: this.payload}); this.onstop?.(); }
  }
  const saved = deferred();
  const {capture} = harness({Recorder: DelayedRecorder, segmentMs: 60000,
    upload: async item => { await saved.promise; return {seq: item.seq}; }});
  await capture.start();
  const recorder = capture.recorder;
  const paused = capture.pause();
  t.mock.timers.tick(3001);
  assert.equal(await paused, false);
  assert.equal(capture.uncertainTail, true);
  await assert.rejects(capture.start(), /save_audio_first/);

  recorder.deliverFinal();
  assert.equal(capture.hasPending, true, 'recovered bytes still need an upload acknowledgement');
  await assert.rejects(capture.start(), /save_audio_first/);
  saved.resolve();
  await capture.queue.flush();
  assert.equal(capture.uncertainTail, false, 'the recovered final event resolves its missing-output warning');
  assert.equal(capture.hasPending, false);
  assert.equal(await capture.start(), true, 'no missing-audio acknowledgement is needed after complete recovery');
  const resumedRecorder = capture.recorder;
  const finalPause = capture.pause();
  resumedRecorder.deliverFinal();
  assert.equal(await finalPause, true);
});

test('a real recorder error remains unresolved after its delayed final bytes arrive and save', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  class DelayedRecorder extends FakeRecorder {
    stop() { this.state = 'inactive'; }
    deliverFinal() { this.ondataavailable?.({data: this.payload}); this.onstop?.(); }
  }
  const {capture} = harness({Recorder: DelayedRecorder, segmentMs: 60000});
  await capture.start();
  const recorder = capture.recorder;
  recorder.onerror();
  const paused = capture.pausing;
  t.mock.timers.tick(3001);
  assert.equal(await paused, false);
  recorder.deliverFinal();
  await capture.queue.flush();

  assert.equal(capture.queue.hasPending, false);
  assert.equal(capture.uncertainTail, true, 'late bytes do not prove a failed recorder captured everything');
  assert.equal(capture.hasPending, true);
  await assert.rejects(capture.start(), /save_audio_first/);
  assert.equal(capture.acknowledgeMissingTail(), true, 'a genuine recording loss still requires acknowledgement');
  assert.equal(capture.hasPending, false);
});

test('an old recovered final cannot clear a different recorder final that is still missing', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  class DelayedRecorder extends FakeRecorder {
    stop() { this.state = 'inactive'; }
    deliverFinal() { this.ondataavailable?.({data: this.payload}); this.onstop?.(); }
  }
  const {capture} = harness({Recorder: DelayedRecorder, segmentMs: 60000});
  await capture.start();
  const oldRecorder = capture.recorder;
  const firstPause = capture.pause();
  t.mock.timers.tick(3001);
  assert.equal(await firstPause, false);
  assert.equal(capture.acknowledgeMissingTail(), true);
  await capture.start();
  const currentRecorder = capture.recorder;
  const secondPause = capture.pause();
  t.mock.timers.tick(3001);
  assert.equal(await secondPause, false);

  oldRecorder.deliverFinal();
  await capture.queue.flush();
  assert.equal(capture.queue.hasPending, false);
  assert.equal(capture.uncertainTail, true, 'recovering old audio cannot acknowledge a later recording loss');
  await assert.rejects(capture.start(), /save_audio_first/);
  currentRecorder.deliverFinal();
  await capture.queue.flush();
  assert.equal(capture.hasPending, false);
  assert.equal(await capture.start(), true);
  const resumedRecorder = capture.recorder;
  const finalPause = capture.pause();
  resumedRecorder.deliverFinal();
  assert.equal(await finalPause, true);
});

test('a throwing Recorder.stop cannot prevent immediate microphone and peer release', async () => {
  class ThrowingStopRecorder extends FakeRecorder {
    stop() { throw Error('synthetic recorder stop failure'); }
  }
  const {capture, streams, errors} = harness({Recorder: ThrowingStopRecorder});
  await capture.start();
  const peer = capture.pc;
  assert.doesNotThrow(() => capture.stopLocal());
  assert.equal(streams[0].track.stops, 1);
  assert.equal(peer.closed, true);
  assert.equal(capture.stream, null);
  assert.equal(capture.state, 'paused');
  assert.equal(capture.uncertainTail, true);
  assert.equal(errors.some(error => error.message === 'synthetic recorder stop failure'), true);
});

test('acknowledging missing recorder output cannot discard queued or retained real bytes', () => {
  const {capture} = harness();
  const blob = recording('must not be discarded');
  capture.uncertainTail = true;
  capture.queue.add(blob, 3000);
  assert.equal(capture.acknowledgeMissingTail(), false);
  assert.equal(capture.uncertainTail, true);
  assert.equal(capture.queue.items[0].blob, blob);
  assert.equal(capture.queue.pendingBytes, blob.size);

  const {capture: retainedCapture} = harness();
  retainedCapture.uncertainTail = true;
  retainedCapture.unsavedFinal = {blob, durationMs: 3000};
  assert.equal(retainedCapture.acknowledgeMissingTail(), false);
  assert.equal(retainedCapture.unsavedFinal.blob, blob);
  assert.equal(retainedCapture.hasPending, true);
});
