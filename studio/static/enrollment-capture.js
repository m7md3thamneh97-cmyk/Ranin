// Contributor-only capture. An acknowledged chunk is the unit of saved progress.
// The queue is deliberately memory-only; a reload cannot recover unacknowledged audio.
export async function sha256(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export class ChunkQueue {
  constructor({sessionId, nextSeq = 0, upload, hash = sha256, onChange = () => {}, onAck = () => {}, maxChunks = 8, maxBytes = 1024 * 1024, timeoutMs = 12000}) {
    Object.assign(this, {sessionId, upload, hash, onChange, onAck, maxChunks, maxBytes, timeoutMs});
    this.nextSeq = nextSeq;
    this.items = [];
    this.running = null;
    this.suspended = false;
  }
  get pendingBytes() { return this.items.reduce((sum, item) => sum + item.blob.size, 0); }
  get hasPending() { return this.items.length > 0; }
  syncNextSeq(value) { this.nextSeq = Math.max(this.nextSeq, Number(value) || 0); }
  mayRecord() { return !this.suspended && this.items.length < this.maxChunks - 2 && this.pendingBytes < this.maxBytes - 160 * 1024; }
  add(blob, durationMs) {
    if (this.items.length >= this.maxChunks || this.pendingBytes + blob.size > this.maxBytes) throw new Error("audio_buffer_full");
    const item = {sessionId: this.sessionId, seq: this.nextSeq++, blob, durationMs, checksum: null, error: null};
    this.items.push(item);
    this.onChange(this);
    return item;
  }
  suspend() { this.suspended = true; this.activeAbort?.abort(); }
  retry() { this.suspended = false; return this.flush(); }
  flush() {
    if (this.running) return this.running;
    const run = async () => {
      while (this.items.length && !this.suspended) {
        const item = this.items[0];
        let timer;
        const abort = new AbortController();
        this.activeAbort = abort;
        try {
          item.checksum ||= await this.hash(item.blob);
          // Revocation may happen while hashing a just-finished segment.
          // Never dispatch its upload after the queue has been suspended.
          if (this.suspended) return false;
          const response = await Promise.race([
            this.upload(item, abort.signal),
            new Promise((_, reject) => { timer = setTimeout(() => { abort.abort(); reject(new Error("upload_timeout")); }, this.timeoutMs); }),
          ]);
          if (response?.seq !== item.seq) throw new Error("audio_ack_missing");
          this.items.shift();
          this.onAck(item);
        } catch (error) {
          item.error = error;
          this.onChange(this);
          return false;
        } finally { clearTimeout(timer); if (this.activeAbort === abort) this.activeAbort = null; }
        this.onChange(this);
      }
      return !this.hasPending;
    };
    this.running = run().finally(() => { this.running = null; this.onChange(this); });
    return this.running;
  }
}

export class InterviewCapture {
  constructor({sessionId, nextSeq = 0, upload, openConnection, closeConnection, mediaDevices = globalThis.navigator?.mediaDevices, Recorder = globalThis.MediaRecorder, Peer = globalThis.RTCPeerConnection, hash, onState = () => {}, onError = () => {}, onEvent = () => {}, onAck = () => {}, onRemote = () => {}, segmentMs = 3000, timeoutMs = 12000, disconnectGraceMs = 5000}) {
    Object.assign(this, {sessionId, openConnection, closeConnection, mediaDevices, Recorder, Peer, onState, onError, onEvent, onRemote, segmentMs, disconnectGraceMs});
    this.state = "paused";
    this.epoch = 0;
    this.recording = false;
    this.starting = null;
    this.pausing = null;
    this.finalChunk = Promise.resolve();
    this.unsavedFinal = null;
    this.missingFinals = new Set();
    this.uncertainTail = false;
    this.queue = new ChunkQueue({sessionId, nextSeq, upload, hash, timeoutMs, onAck, onChange: () => this.onState(this)});
  }
  get hasPending() { return this.queue.hasPending || Boolean(this.unsavedFinal) || this.uncertainTail; }
  get uncertainTail() { return this._uncertainTail || this.missingFinals.size > 0; }
  set uncertainTail(value) { this._uncertainTail = value; if (!value) this.missingFinals.clear(); }
  setState(value) { this.state = value; this.onState(this); }
  mimeType() {
    for (const mime of ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"]) {
      if (this.Recorder?.isTypeSupported(mime)) return mime;
    }
    throw new Error("recording_unsupported");
  }
  start() {
    if (this.starting) return this.starting;
    if (this.state === "live") return Promise.resolve(true);
    if (this.pausing || this.hasPending) return Promise.reject(new Error("save_audio_first"));
    const epoch = ++this.epoch;
    const run = async () => {
      if (!this.mediaDevices?.getUserMedia || !this.Recorder || !this.Peer) throw new Error("recording_unsupported");
      const mime = this.mimeType();
      this.setState("connecting");
      let stream;
      try {
        // This is a live conversation: speaker playback must not re-enter VAD
        // and cancel the interviewer or contaminate the contributor recording.
        // Keep gain control off to avoid amplifying room noise between answers.
        stream = await this.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false}, video: false});
        if (epoch !== this.epoch) { stream.getTracks().forEach((track) => track.stop()); return false; }
        this.stream = stream;
        const pc = new this.Peer();
        this.pc = pc;
        pc.ontrack = (event) => { if (epoch === this.epoch) this.onRemote(event.streams[0]); };
        stream.getAudioTracks().forEach((track) => pc.addTrack(track, stream));
        const dc = pc.createDataChannel("oai-events");
        this.dc = dc;
        dc.onmessage = (event) => {
          if (epoch !== this.epoch) return;
          let parsed;
          try { parsed = JSON.parse(event.data); } catch { return; }
          Promise.resolve().then(() => this.onEvent(parsed, this)).catch((error) => this.onError(error));
        };
        dc.onopen = () => { if (epoch === this.epoch) Promise.resolve().then(() => this.onEvent({type: "raneen.connected"}, this)).catch((error) => this.onError(error)); };
        dc.onerror = () => { if (epoch === this.epoch) this.pause().then(() => this.onError(new Error("connection_lost"))).catch(this.onError); };
        pc.onconnectionstatechange = () => {
          if (epoch !== this.epoch) return;
          if (pc.connectionState === "disconnected") {
            // A transient ICE disruption can recover on this same connection.
            // Keep bounded audio capture running without creating another call.
            this.disconnectTimer ??= setTimeout(() => {
              this.disconnectTimer = null;
              if (epoch === this.epoch && pc.connectionState === "disconnected") {
                this.pause().then(() => this.onError(new Error("connection_lost"))).catch(this.onError);
              }
            }, this.disconnectGraceMs);
            return;
          }
          clearTimeout(this.disconnectTimer);
          this.disconnectTimer = null;
          if (["failed", "closed"].includes(pc.connectionState)) {
            this.pause().then(() => this.onError(new Error("connection_lost"))).catch(this.onError);
          }
        };
        const offer = await pc.createOffer();
        if (epoch !== this.epoch) return false;
        await pc.setLocalDescription(offer);
        if (epoch !== this.epoch) return false;
        this.connectionAttempted = true;
        const sdp = await this.openConnection(offer.sdp);
        if (epoch !== this.epoch) { await this.closeConnection().catch(this.onError); return false; }
        await pc.setRemoteDescription({type: "answer", sdp});
        if (epoch !== this.epoch) return false;
        this.recording = true;
        this.setState("live");
        this.startSegment(mime);
        return this.state === "live";
      } catch (error) {
        this.stopLocal();
        if (this.connectionAttempted) await this.closeConnection().catch(this.onError);
        throw error;
      }
    };
    const pending = run().finally(() => {
      if (this.starting === pending) this.starting = null;
      this.onState(this);
    });
    this.starting = pending;
    return pending;
  }
  send(event) { if (this.dc?.readyState === "open") this.dc.send(JSON.stringify(event)); }
  startSegment(mime) {
    try { this.beginSegment(mime); }
    catch (error) {
      this.uncertainTail = true;
      this.onError(error);
      this.pause().catch(this.onError);
    }
  }
  beginSegment(mime) {
    if (!this.recording || !this.stream) return;
    if (!this.queue.mayRecord()) {
      this.pause().then(() => this.onError(new Error("audio_backpressure"))).catch(this.onError);
      return;
    }
    const parts = [];
    const recorder = new this.Recorder(this.stream, {mimeType: mime, audioBitsPerSecond: 128000});
    const epoch = this.epoch;
    const started = performance.now();
    let segmentTimer;
    this.recorder = recorder;
    let finish;
    const final = this.finalChunk = new Promise((resolve) => { finish = resolve; });
    recorder.ondataavailable = (event) => { if (event.data?.size) parts.push(event.data); };
    recorder.onerror = () => { this.uncertainTail = true; this.pause().then(() => this.onError(new Error("recording_failed"))).catch(this.onError); };
    recorder.onstop = () => {
      // A browser may deliver an old final event after pause timed out. It must
      // not cancel a new segment's timer or start a second recorder on resume.
      clearTimeout(segmentTimer);
      const blob = new Blob(parts, {type: mime});
      const durationMs = Math.max(250, Math.min(15000, Math.round(performance.now() - started)));
      try {
        if (blob.size) {
          this.queue.add(blob, durationMs);
          this.queue.flush().then((saved) => {
            if (!saved && this.recording) this.pause().then(() => this.onError(new Error("audio_not_saved"))).catch(this.onError);
          }).catch(this.onError);
        } else {
          this.uncertainTail = true;
          this.onError(new Error("recording_failed"));
          this.pause().catch(this.onError);
        }
      } catch (error) {
        // Preserve the final blob if the browser produced unexpectedly large output.
        this.unsavedFinal = {blob, durationMs};
        this.onError(error);
        this.pause().catch(this.onError);
      } finally { this.missingFinals.delete(final); finish(); this.onState(this); }
      if (this.recording && this.recorder === recorder && epoch === this.epoch) this.startSegment(mime);
    };
    try { recorder.start(); }
    catch (error) { finish(); throw error; }
    segmentTimer = setTimeout(() => {
      if (recorder.state !== "recording") return;
      try { recorder.stop(); }
      catch (error) {
        this.uncertainTail = true;
        finish();
        this.onError(error);
        this.pause().catch(this.onError);
      }
    }, this.segmentMs);
    this.timer = segmentTimer;
  }
  stopLocal() {
    ++this.epoch;
    this.recording = false;
    clearTimeout(this.timer);
    clearTimeout(this.disconnectTimer);
    this.disconnectTimer = null;
    const final = this.finalChunk;
    if (this.recorder?.state === "recording") {
      try { this.recorder.stop(); }
      catch (error) { this.uncertainTail = true; this.onError(error); }
    }
    // Track release must not wait for network, recorder completion, or hangup.
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    const dc = this.dc;
    const pc = this.pc;
    this.dc = null;
    this.pc = null;
    dc?.close();
    pc?.close();
    this.onRemote(null);
    this.setState("paused");
    return final;
  }
  pause({save = true} = {}) {
    const final = this.stopLocal();
    if (!save) this.queue.suspend();
    if (this.pausing) return this.pausing;
    const hangup = this.connectionAttempted ? this.closeConnection().catch((error) => { this.onError(error); return false; }) : Promise.resolve(true);
    this.connectionAttempted = false;
    let timer;
    const finish = async () => {
      try {
        await Promise.race([final, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("final_audio_pending")), 3000); })]);
      } catch (error) { this.missingFinals.add(final); this.onError(error); }
      finally { clearTimeout(timer); }
      const saved = save ? await this.queue.flush() : false;
      await hangup;
      return saved && !this.hasPending;
    };
    this.pausing = finish().finally(() => { this.pausing = null; this.onState(this); });
    return this.pausing;
  }
  async retry() {
    await this.queue.retry();
    if (this.unsavedFinal && this.queue.mayRecord()) {
      const {blob, durationMs} = this.unsavedFinal;
      this.queue.add(blob, durationMs);
      this.unsavedFinal = null;
    }
    return await this.queue.flush() && !this.hasPending;
  }
  acknowledgeMissingTail() {
    // Only missing browser output can be acknowledged. Real queued bytes stay held.
    if (this.queue.hasPending || this.unsavedFinal || this.stream || this.pausing) return false;
    this.uncertainTail = false;
    this.onState(this);
    return true;
  }
}
