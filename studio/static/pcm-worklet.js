// Capture mono PCM as delivered by the browser. No agent audio is mixed in.
class PCMCollector extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.offset = 0;
    this.active = true;
    this.port.onmessage = ({data}) => {
      if (data === 'stop') {
        this.active = false;
        if (this.offset) this.port.postMessage({samples: this.buffer.slice(0, this.offset)});
        this.port.postMessage({stopped: true});
      }
    };
  }
  process(inputs, outputs) {
    // Explicitly output silence: the contributor should not hear delayed feedback.
    for (const channel of outputs[0] || []) channel.fill(0);
    const input = inputs[0]?.[0];
    if (this.active && input) {
      for (const sample of input) {
        this.buffer[this.offset++] = sample;
        if (this.offset === this.buffer.length) {
          this.port.postMessage({samples: this.buffer.slice()});
          this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-collector', PCMCollector);
