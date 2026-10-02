import {normalizeProviderEvent} from './become-call.js';

// Provider configurations, control URLs and webhook credentials never travel
// through Daily app-messages. Only this server-filtered, cookie-scoped stream
// carries conversation events to the browser.
export class BecomingEventStream {
  constructor({Source=globalThis.EventSource,onEvent=()=>{},onOpen=()=>{},onError=()=>{},onClosed=()=>{},now=()=>Date.now(),monotonic=()=>performance.now(),maxReconnects=3,maximumLagMs=1000}={}){
    Object.assign(this,{Source,onEvent,onOpen,onError,onClosed,now,monotonic,maxReconnects,maximumLagMs});this.source=null;this.closed=false;this.reconnects=0;this.seen=new Set();this.speechTimes=new Map();this.lastSignal=0;this.healthy=false;
  }
  start(id){
    if(this.source||this.closed)throw new Error('event_stream_already_started');
    if(!this.Source)throw new Error('recording_unsupported');
    if(typeof id!=='string'||!/^[0-9a-f]{32}$/.test(id))throw new Error('invalid_session');
    this.source=new this.Source('/api/becoming/sessions/'+id+'/events-stream',{withCredentials:true});
    this.source.onopen=()=>{if(this.closed)return;this.lastSignal=this.now();this.healthy=true;this.onOpen();};
    this.source.onerror=()=>{if(this.closed)return;this.healthy=false;this.onError(new Error('event_stream_disconnected'));if(++this.reconnects>=this.maxReconnects)this.stop();};
    this.source.addEventListener('provider_event',event=>this.provider(event));
    this.source.addEventListener('heartbeat',()=>{if(this.closed)return;this.lastSignal=this.now();if(!this.healthy){this.healthy=true;this.onOpen();}});
    this.source.addEventListener('closed',event=>{let data={};try{data=JSON.parse(event.data);}catch{}this.stop();this.onClosed(data.state);});
    this.watchdog=setInterval(()=>{if(!this.closed&&this.healthy&&this.now()-this.lastSignal>3000){this.healthy=false;this.onError(new Error('event_stream_stalled'));}},1000);
  }
  provider(event){
    if(this.closed)return;
    let raw;try{raw=JSON.parse(event.data);}catch{return;}
    const message=normalizeProviderEvent(raw);if(!message)return;
    const current=this.now(),eventAt=Number(raw.event_at_ms),relayAt=Number(raw.relay_at_ms);
    // Reconnected backlog cannot reopen the current microphone gate. Epoch
    // timestamps map to the browser's monotonic audio-capture clock.
    if(!Number.isFinite(eventAt)||!Number.isFinite(relayAt)||current-eventAt>this.maximumLagMs||eventAt-current>500||current-relayAt>this.maximumLagMs||relayAt-current>500){this.healthy=false;this.onError(new Error('event_stream_stale'));return;}
    const cursor=event.lastEventId||raw.event_id;
    if(typeof cursor==='string'){if(this.seen.has(cursor))return;this.seen.add(cursor);if(this.seen.size>256)this.seen.delete(this.seen.values().next().value);}
    if(message.type==='speech-update'){if(eventAt<(this.speechTimes.get(message.role)??-Infinity))return;this.speechTimes.set(message.role,eventAt);}
    this.lastSignal=current;
    if(!this.healthy){this.healthy=true;this.onOpen();}
    this.onEvent({...message,call_id:raw.call_id,event_id:raw.event_id,event_time_source:raw.event_time_source},this.monotonic()+Math.min(0,eventAt-current));
  }
  stop(){if(this.closed)return;this.closed=true;clearInterval(this.watchdog);this.source?.close();this.source=null;this.healthy=false;}
}
