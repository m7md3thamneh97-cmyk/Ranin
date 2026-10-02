import {encodeWav} from './recorder.js';
import {ChunkQueue} from './enrollment-capture.js';

// The provider's user/assistant events are conservative capture gates, not
// authenticated proof of a speaker's identity or of clone activation.
export class SpeakerGate {
  constructor({startGuardMs=150,assistantTailMs=450,holdbackMs=1000}={}) {
    Object.assign(this,{startGuardMs,assistantTailMs,holdbackMs});
    this.events=[{at:-Infinity,user:false,assistant:false,playback:false,userStarted:Infinity,assistantStopped:-Infinity,playbackStopped:-Infinity}];
  }
  reset(){this.events=[{at:-Infinity,user:false,assistant:false,playback:true,userStarted:Infinity,assistantStopped:-Infinity,playbackStopped:-Infinity}];}
  prune(at){while(this.events.length>2&&this.events[1].at<at-30000)this.events.shift();}
  playback(active,at){const previous=this.events.at(-1);if(previous.playback===active)return;at=Math.max(at,previous.at);this.events.push({...previous,at,playback:active,playbackStopped:active?previous.playbackStopped:at});this.prune(at);}
  update(message,at) {
    if (message.type!=='speech-update' || !['user','assistant'].includes(message.role) || !['started','stopped'].includes(message.status)) return;
    const previous=this.events.at(-1);at=Math.max(at,previous.at);const next={...previous,at};
    const active=message.status==='started';
    next[message.role]=active;
    if(message.role==='user'&&active&&!previous.user)next.userStarted=at;
    if(message.role==='assistant'&&!active)next.assistantStopped=at;
    this.events.push(next);
    // Keep the state before the recent window as well as every recent event.
    this.prune(at);
  }
  permits(start,end) {
    if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)return false;
    const state=[...this.events].reverse().find(event=>event.at<=start);
    if(!state||!state.user||state.assistant||state.playback||start-state.userStarted<this.startGuardMs||start-state.assistantStopped<this.assistantTailMs||start-state.playbackStopped<this.assistantTailMs)return false;
    // Reject a complete frame if the user stopped, or if assistant speech
    // starts inside it or during the delay used to catch late provider events.
    return !this.events.some(event=>event.at>start&&event.at<=end+this.holdbackMs&&(event.assistant||event.playback||(event.at<=end&&!event.user)));
  }
}

export function cleanFrame(samples,{minimumRms=.003,maximumClippedFraction=.01}={}) {
  if(!(samples instanceof Float32Array)||!samples.length)return false;
  let energy=0,clipped=0;
  for(const value of samples){if(!Number.isFinite(value))return false;energy+=value*value;if(Math.abs(value)>=.995)clipped++;}
  return Math.sqrt(energy/samples.length)>=minimumRms&&clipped/samples.length<=maximumClippedFraction;
}

export class BecomingCapture {
  constructor({sessionId,upload,onError=()=>{},onAck=()=>{},onChange=()=>{},mediaDevices=globalThis.navigator?.mediaDevices,Context=globalThis.AudioContext,Worklet=globalThis.AudioWorkletNode,now=()=>performance.now(),hash,segmentSamples=72000}={}) {
    Object.assign(this,{mediaDevices,Context,Worklet,now,onError,onChange,segmentSamples});
    this.gate=new SpeakerGate();this.frames=[];this.parts=[];this.partSamples=0;this.acceptedSamples=0;this.active=false;this.collecting=true;this.epoch=0;
    this.queue=new ChunkQueue({sessionId,upload:async(item,signal)=>{item.attempts=(item.attempts||0)+1;return upload(item,signal);},hash,maxChunks:8,maxBytes:1600000,onAck,onChange:()=>onChange(this)});
  }
  get durationSeconds(){return this.acceptedSamples/24000;}
  async start() {
    if(this.active||this.starting)throw new Error('capture_already_started');
    const epoch=++this.epoch;this.starting=true;
    try {
      if(!this.mediaDevices?.getUserMedia||!this.Context||!this.Worklet)throw new Error('recording_unsupported');
      // Echo cancellation reduces speaker leakage; gating still excludes all
      // known assistant speech and a conservative acoustic tail.
      const stream=await this.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:false},video:false});
      if(epoch!==this.epoch){stream.getTracks().forEach(track=>track.stop());return false;}
      this.stream=stream;
      const raw=stream.getAudioTracks()[0].getSettings?.()||{};
      this.settings={source:'isolated_microphone',speaker:'user',assistant_overlap:false,speech_gate:'vapi-user-speech',sample_rate:24000,encoded_channels:1,encoded_bits:16,speech_start_guard_ms:this.gate.startGuardMs,assistant_tail_guard_ms:this.gate.assistantTailMs,event_holdback_ms:this.gate.holdbackMs,browser_processing:{echo_cancellation:raw.echoCancellation??null,noise_suppression:raw.noiseSuppression??null,auto_gain_control:raw.autoGainControl??null,source_sample_rate:raw.sampleRate??null}};
      this.context=new this.Context({sampleRate:24000});
      if(this.context.sampleRate!==24000)throw new Error('sample_rate_unsupported');
      await this.context.resume();
      await this.context.audioWorklet.addModule('/static/become-worklet.js');
      if(epoch!==this.epoch){await this.release();return false;}
      this.clockOffset=this.now()-this.context.currentTime*1000;
      this.node=new this.Worklet(this.context,'becoming-pcm',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
      this.node.port.onmessage=({data})=>{if(data.samples&&this.active)this.ingest(data.samples,this.clockOffset+data.start*1000,this.clockOffset+data.end*1000);};
      this.source=this.context.createMediaStreamSource(stream);
      this.source.connect(this.node);this.node.connect(this.context.destination);
      this.active=true;
      this.drainTimer=setInterval(()=>this.drain(),80);
      this.retryTimer=setInterval(()=>this.retry(),2000);
      return true;
    } catch(error){await this.release();throw error;}
    finally{this.starting=false;}
  }
  event(message,at=this.now()){this.gate.update(message,at);}
  playback(active,at=this.now()){this.gate.playback(Boolean(active),at);}
  suspendGate(){this.setCollecting(false);this.gate.reset();}
  setCollecting(value){this.collecting=Boolean(value);if(!this.collecting){this.frames=[];this.parts=[];this.partSamples=0;}}
  ingest(samples,start,end){if(!this.active||!this.collecting)return;this.frames.push({samples,start,end});this.drain();}
  drain(){
    const cutoff=this.now()-this.gate.holdbackMs;
    while(this.frames.length&&this.frames[0].end<=cutoff){
      const frame=this.frames.shift();
      if(!this.gate.permits(frame.start,frame.end)||!cleanFrame(frame.samples))continue;
      if(!this.queue.mayRecord()){this.onError(new Error('audio_backpressure'));continue;}
      const remaining=125*24000-this.acceptedSamples;
      if(remaining<=0){this.setCollecting(false);break;}
      const samples=frame.samples.length>remaining?frame.samples.slice(0,remaining):frame.samples;
      this.parts.push(samples);this.partSamples+=samples.length;this.acceptedSamples+=samples.length;
      if(this.partSamples>=this.segmentSamples)this.flushSegment();
      if(this.acceptedSamples>=125*24000){this.flushSegment();this.setCollecting(false);break;}
    }
    // A stopped clock or unavailable events cannot create an unbounded buffer.
    if(this.frames.length>20)this.frames.splice(0,this.frames.length-20);
    this.onChange(this);
  }
  flushSegment(){
    if(this.partSamples<2400){this.parts=[];this.partSamples=0;return;}
    const blob=encodeWav(this.parts,24000),durationMs=this.partSamples/24;
    try{const item=this.queue.add(blob,durationMs);item.settings={...this.settings,eligible_ms:Math.round(durationMs)};item.attempts=0;this.parts=[];this.partSamples=0;this.flushQueue();}
    catch(error){this.onError(error);this.parts=[];this.partSamples=0;}
  }
  async flushQueue(){
    if(this.queue.running||!this.queue.items.length||this.queue.suspended)return;
    const saved=await this.queue.flush();
    if(!saved){const item=this.queue.items[0],error=item?.error;this.onError(error||new Error('audio_not_saved'));if(error?.retryable===false||item?.attempts>=3)this.queue.suspend();}
  }
  retry(){if(this.queue.hasPending&&!this.queue.suspended)this.flushQueue();}
  async release(){
    ++this.epoch;this.active=false;clearInterval(this.drainTimer);clearInterval(this.retryTimer);
    this.stream?.getTracks().forEach(track=>track.stop());this.stream=null;
    try{this.node?.port.postMessage('stop');this.source?.disconnect();this.node?.disconnect();}catch{}
    if(this.context&&this.context.state!=='closed')await this.context.close();
    this.source=null;this.node=null;this.context=null;this.frames=[];
  }
  async stop({save=true}={}){
    // Stop the microphone immediately, before any network or provider cleanup.
    if(!save)this.queue.suspend();
    await this.release();
    if(save){this.flushSegment();await this.queue.flush();}
    else{this.parts=[];this.partSamples=0;this.queue.items=[];}
    return !this.queue.hasPending;
  }
}
