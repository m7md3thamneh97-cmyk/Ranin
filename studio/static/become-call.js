export function normalizeProviderEvent(raw) {
  let message=raw?.data??raw;
  if(typeof message==='string'){try{message=JSON.parse(message);}catch{return null;}}
  if(message?.message&&typeof message.message==='object')message=message.message;
  if(!message||typeof message!=='object')return null;
  if(message.type==='speech-update'&&['user','assistant'].includes(message.role)&&['started','stopped'].includes(message.status))return {type:message.type,role:message.role,status:message.status};
  if(message.type==='transcript'&&message.role==='user'&&(message.transcriptType??message.transcript_type)==='final'&&typeof message.transcript==='string')return {type:'transcript',role:'user',transcript_type:'final',transcript:message.transcript.slice(0,3000)};
  if(message.type==='assistant.started'){
    const voice=message.new_assistant_voice??message.newAssistant?.voice??message.new_assistant?.voice??message.assistant?.voice;
    return {type:'assistant.started',...(voice&&typeof (voice.voiceId??voice.voice_id)==='string'?{new_assistant_voice:{provider:voice.provider,voice_id:voice.voiceId??voice.voice_id}}:{})};
  }
  return null;
}

export class BecomingCall {
  constructor({Daily=globalThis.DailyIframe,Stream=globalThis.MediaStream,Context=globalThis.AudioContext,createAudio=()=>document.createElement('audio'),onPlayback=()=>{},onConnected=()=>{},onEnded=()=>{},onError=()=>{},onSoundBlocked=()=>{}}={}){Object.assign(this,{Daily,Stream,Context,createAudio,onPlayback,onConnected,onEnded,onError,onSoundBlocked});this.audios=new Map();this.monitors=new Map();this.stopping=false;this.started=false;this.epoch=0;}
  async start({url,token,microphoneTrack}){
    if(this.started)throw new Error('call_already_started');
    const parsed=new URL(url);
    if(parsed.protocol!=='https:'||!parsed.hostname.endsWith('.daily.co')||parsed.username||parsed.password||(parsed.port&&parsed.port!=='443')||parsed.hash)throw new Error('invalid_call_room');
    if(!this.Daily||!microphoneTrack||microphoneTrack.kind!=='audio')throw new Error('call_unavailable');
    this.started=true;const epoch=++this.epoch;
    try{
      // Daily receives the already-consented microphone track. Remote tracks
      // are played separately and never enter the PCM capture graph.
      this.call=this.Daily.createCallObject({audioSource:microphoneTrack,videoSource:false,avoidEval:true});
      this.call.on('joined-meeting',()=>{if(!this.stopping&&epoch===this.epoch)this.onConnected();});
      this.call.on('left-meeting',()=>{if(!this.stopping){this.stop();this.onEnded();}});
      this.call.on('error',()=>{if(!this.stopping){this.onError(new Error('connection_lost'));this.stop();}});
      this.call.on('track-started',event=>{if(this.stopping||event.participant?.local||event.track?.kind!=='audio')return;if(this.audios.has(event.track.id))this.removeAudio(event.track.id);const audio=this.createAudio();audio.autoplay=true;audio.srcObject=new this.Stream([event.track]);this.audios.set(event.track.id,audio);this.monitorPlayback(event.track,audio.srcObject);audio.play().catch(()=>{if(!this.stopping)this.onSoundBlocked(true);});});
      this.call.on('track-stopped',event=>this.removeAudio(event.track?.id));
      await this.call.join({url:parsed.href,...(token?{token}:{})});
      if(this.stopping||epoch!==this.epoch){await this.stop();return false;}
      return true;
    }catch(error){await this.stop();throw error;}
  }
  monitorPlayback(track,stream){
    this.onPlayback(true);
    let context,source,node,timer;const monitor={active:true};this.monitors.set(track.id,monitor);
    try{
      // A separate transient graph measures remote playback only to veto
      // cloning frames. It has no worklet, output, recording or saved PCM.
      context=new this.Context();source=context.createMediaStreamSource(stream);node=context.createAnalyser();node.fftSize=512;source.connect(node);
      const samples=new Float32Array(node.fftSize);
      const update=()=>{if(this.stopping||monitor.released)return;try{if(context.state!=='running'){monitor.active=true;this.onPlayback(true);return;}node.getFloatTimeDomainData(samples);let energy=0;for(const sample of samples)energy+=sample*sample;monitor.active=Math.sqrt(energy/samples.length)>.003;this.onPlayback([...this.monitors.values()].some(item=>item.active));}catch{monitor.active=true;this.onPlayback(true);}};
      const activate=()=>{if(!this.stopping&&!monitor.released&&!timer)timer=setInterval(update,50);};
      monitor.resume=async()=>{await context.resume();activate();};
      Promise.resolve(monitor.resume()).catch(()=>{if(this.stopping||monitor.released)return;monitor.active=true;this.onPlayback(true);this.onSoundBlocked(true);});
      monitor.release=()=>{monitor.released=true;clearInterval(timer);try{source.disconnect();node.disconnect();}catch{}if(context.state!=='closed')context.close().catch(()=>{});};
    }catch{monitor.active=true;this.onPlayback(true);try{context?.close().catch(()=>{});}catch{}}
  }
  removeAudio(id){const audio=this.audios.get(id);if(audio){audio.pause();audio.srcObject=null;audio.remove?.();this.audios.delete(id);}this.monitors.get(id)?.release?.();this.monitors.delete(id);if(!this.stopping)this.onPlayback(!this.monitors.size||[...this.monitors.values()].some(item=>item.active));}
  async enableSound(){let blocked=false;for(const monitor of this.monitors.values())try{await monitor.resume?.();}catch{blocked=true;}for(const audio of this.audios.values())try{await audio.play();}catch{blocked=true;}this.onSoundBlocked(blocked);return !blocked;}
  async stop(){
    if(this.stopPromise)return this.stopPromise;
    this.stopping=true;++this.epoch;for(const id of [...this.audios.keys()])this.removeAudio(id);
    const call=this.call;this.call=null;
    const bounded=async(action)=>{let timer;try{await Promise.race([Promise.resolve().then(action),new Promise(resolve=>{timer=setTimeout(resolve,3000);})]);}catch{}finally{clearTimeout(timer);}};
    this.stopPromise=(async()=>{await bounded(()=>call?.leave());await bounded(()=>call?.destroy());})();
    return this.stopPromise;
  }
}
