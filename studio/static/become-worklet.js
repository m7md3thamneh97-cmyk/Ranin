// Only the provided local microphone source is connected to this processor.
// AudioContext timestamps describe actual PCM spans, rather than wall time.
class BecomingPCM extends AudioWorkletProcessor {
  constructor() { super(); this.buffer = new Float32Array(2048); this.offset = 0; this.start = 0; this.active = true; this.port.onmessage = ({data}) => { if (data === 'stop') { this.active = false; this.flush(); this.port.postMessage({stopped:true}); } }; }
  flush() { if (this.offset) { this.port.postMessage({samples:this.buffer.slice(0,this.offset),start:this.start,end:this.start+this.offset/sampleRate}); this.offset=0; } }
  process(inputs,outputs) {
    for (const channel of outputs[0] || []) channel.fill(0);
    const input=inputs[0]?.[0];
    if (this.active && input) for (let i=0;i<input.length;i++) { if (!this.offset) this.start=currentTime+i/sampleRate; this.buffer[this.offset++]=input[i]; if (this.offset===this.buffer.length) this.flush(); }
    return true;
  }
}
registerProcessor('becoming-pcm',BecomingPCM);
