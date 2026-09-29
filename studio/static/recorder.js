export function encodeWav(chunks, sampleRate) {
  const length = chunks.reduce((n, part) => n + part.length, 0);
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const text = (start, value) => [...value].forEach((c, i) => view.setUint8(start + i, c.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, length * 2, true);
  let offset = 44;
  for (const chunk of chunks) for (const sample of chunk) {
    const s = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, Math.round(s < 0 ? s * 32768 : s * 32767), true); offset += 2;
  }
  return new Blob([buffer], {type: 'audio/wav'});
}

export class Recorder {
  constructor(onLevel) { this.onLevel = onLevel; this.active = false; this.starting = false; }
  async start() {
    if (this.active || this.starting) throw new Error('A recording is already active.');
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode)
      throw new Error('Use a current browser on localhost or HTTPS with microphone access.');
    this.starting = true;
    this.chunks = [];
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
      this.settings = this.stream.getAudioTracks()[0].getSettings();
      // Avoid storing persistent device identifiers in the dataset.
      delete this.settings.deviceId; delete this.settings.groupId;
      this.context = new AudioContext();
      await this.context.resume();
      await this.context.audioWorklet.addModule('/static/pcm-worklet.js');
      this.node = new AudioWorkletNode(this.context, 'pcm-collector', {numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
      this.node.port.onmessage = ({data}) => {
        if (data.samples) {
          this.chunks.push(data.samples);
          const sum = data.samples.reduce((v, x) => v + x*x, 0);
          this.onLevel?.(Math.sqrt(sum / data.samples.length));
        }
        if (data.stopped) this.resolveStop?.();
      };
      this.source = this.context.createMediaStreamSource(this.stream);
      this.source.connect(this.node); this.node.connect(this.context.destination);
      this.sampleRate = this.context.sampleRate;
      this.active = true;
    } catch (error) { await this.release(); throw error; }
    finally { this.starting = false; }
  }
  async stop() {
    if (!this.active) throw new Error('No recording is active.');
    this.active = false;
    let timer;
    try {
      await new Promise((resolve, reject) => {
        this.resolveStop = resolve;
        timer = setTimeout(() => reject(new Error('Audio capture did not finish cleanly. Please record again.')), 2000);
        this.node.port.postMessage('stop');
      });
      clearTimeout(timer);
      return {blob:encodeWav(this.chunks,this.sampleRate),settings:{...this.settings,encodedSampleRate:this.sampleRate,encodedChannels:1,encodedBits:16,capture:'AudioWorklet PCM; browser/device processing may still apply'}};
    } finally { clearTimeout(timer); this.resolveStop=null; await this.release(); }
  }
  async release() {
    this.stream?.getTracks().forEach(track=>track.stop());
    try {this.source?.disconnect();this.node?.disconnect();}catch{}
    if(this.context&&this.context.state!=='closed') await this.context.close();
    this.active=false;
  }
}
