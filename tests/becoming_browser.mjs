// Synthetic browser/PCM/Daily tests. No provider calls or physical microphone.
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {SpeakerGate,cleanFrame,BecomingCapture} from '../studio/static/become-capture.js';
import {BecomingCall,normalizeProviderEvent} from '../studio/static/become-call.js';
import {BecomingEventStream} from '../studio/static/become-events.js';

test('speaker gates exclude bootstrap audio, overlap, start edges, and assistant acoustic tails',()=>{
  const gate=new SpeakerGate();
  assert.equal(gate.permits(200,285),false);
  gate.update({type:'speech-update',role:'user',status:'started'},100);
  assert.equal(gate.permits(200,285),false);
  assert.equal(gate.permits(300,385),true);
  gate.update({type:'speech-update',role:'assistant',status:'started'},600);
  assert.equal(gate.permits(400,485),false,'holdback catches a late assistant event');
  assert.equal(gate.permits(700,785),false);
  gate.update({type:'speech-update',role:'assistant',status:'stopped'},1000);
  assert.equal(gate.permits(1300,1385),false);
  assert.equal(gate.permits(1500,1585),true);
  gate.update({type:'speech-update',role:'user',status:'stopped'},1540);
  assert.equal(gate.permits(1500,1585),false);
});

test('invalid, silent and clipped frames never accrue eligible speech',()=>{
  assert.equal(cleanFrame(new Float32Array(2048)),false);
  assert.equal(cleanFrame(new Float32Array(2048).fill(1)),false);
  assert.equal(cleanFrame(new Float32Array([NaN,.1])),false);
  assert.equal(cleanFrame(new Float32Array(2048).fill(.1)),true);
});

test('duration comes from encoded sample counts, not elapsed speech events or wall time',async()=>{
  let item;
  const capture=new BecomingCapture({sessionId:'synthetic',now:()=>20000,segmentSamples:2400,hash:async()=> 'synthetic-checksum',upload:async(value)=>{item=value;return {seq:value.seq};}});
  capture.active=true;capture.settings={source:'isolated_microphone',speaker:'user',assistant_overlap:false,speech_gate:'vapi-user-speech',sample_rate:24000};
  capture.event({type:'speech-update',role:'user',status:'started'},0);
  capture.ingest(new Float32Array(1200).fill(.1),1000,1050);
  capture.ingest(new Float32Array(1200).fill(.1),15000,15050);
  await capture.queue.running;
  assert.equal(capture.durationSeconds,.1);
  assert.equal(item.settings.eligible_ms,100);
  assert.equal(item.blob.size,44+2400*2);
  const bytes=new DataView(await item.blob.arrayBuffer());
  assert.equal(bytes.getUint32(24,true),24000);
  assert.equal(bytes.getUint16(22,true),1);
  assert.equal(item.settings.assistant_overlap,false);
  await capture.stop({save:false});
});

test('upload retries retain sequence, checksum and exactly the same microphone bytes',async()=>{
  const attempts=[];
  const capture=new BecomingCapture({sessionId:'synthetic',hash:async()=> 'one-checksum',upload:async(item)=>{attempts.push(item);if(attempts.length<3)throw new Error('temporary_network');return {seq:item.seq};}});
  capture.settings={source:'isolated_microphone'};capture.parts=[new Float32Array(4801).fill(.1)];capture.partSamples=4801;
  capture.flushSegment();await capture.queue.running;
  await capture.flushQueue();await capture.flushQueue();
  assert.equal(attempts.length,3);
  assert.ok(attempts.every(item=>item===attempts[0]&&item.seq===0&&item.checksum==='one-checksum'));
  assert.equal(attempts[0].settings.eligible_ms,Math.round(4801/24));
  assert.equal(capture.queue.hasPending,false);
});

test('three failed uploads bound retry pressure; authorization failures never retry',async()=>{
  for(const retryable of [true,false]){
    let attempts=0;
    const capture=new BecomingCapture({hash:async()=> 'synthetic-checksum',upload:async()=>{attempts++;const error=new Error('failed');error.retryable=retryable;throw error;}});
    capture.parts=[new Float32Array(4096).fill(.1)];capture.partSamples=4096;capture.settings={};capture.flushSegment();await capture.queue.running;
    await capture.flushQueue();await capture.flushQueue();await capture.flushQueue();
    assert.equal(attempts,retryable?3:1);assert.equal(capture.queue.suspended,true);
  }
});

test('pausing PCM collection for clone handoff keeps the conversation microphone alive',()=>{
  let stopped=0;
  const capture=new BecomingCapture({upload:async item=>({seq:item.seq})});capture.active=true;capture.stream={getTracks:()=>[{stop:()=>stopped++}]};
  capture.setCollecting(false);capture.ingest(new Float32Array(2048).fill(.1),0,85);
  assert.equal(stopped,0);assert.equal(capture.active,true);assert.equal(capture.frames.length,0);
});

test('provider event normalization preserves Arabic final user evidence and clone routing observations',()=>{
  assert.equal(normalizeProviderEvent({data:{type:'transcript',role:'assistant',transcriptType:'final',transcript:'Never clone this'}}),null);
  assert.equal(normalizeProviderEvent({data:{type:'transcript',role:'user',transcriptType:'partial',transcript:'partial'}}),null);
  assert.deepEqual(normalizeProviderEvent({data:JSON.stringify({type:'transcript',role:'user',transcriptType:'final',transcript:'شوف، لا تستعجل بالقرار.'})}),{type:'transcript',role:'user',transcript_type:'final',transcript:'شوف، لا تستعجل بالقرار.'});
  assert.deepEqual(normalizeProviderEvent({data:{message:{type:'assistant.started',newAssistant:{voice:{provider:'11labs',voiceId:'synthetic-voice'}}}}}),{type:'assistant.started',new_assistant_voice:{provider:'11labs',voice_id:'synthetic-voice'}});
});

function fakeDaily(){
  const handlers=new Map(),calls=[];
  const instance={on:(name,fn)=>handlers.set(name,fn),join:async()=>{handlers.get('joined-meeting')?.();},leave:async()=>calls.push('leave'),destroy:async()=>calls.push('destroy')};
  return {sdk:{createCallObject:settings=>{calls.push(settings);return instance;}},calls,emit:(name,value)=>handlers.get(name)?.(value)};
}
test('one Daily room uses only the consenting microphone track; remote output never feeds capture',async()=>{
  const daily=fakeDaily(),played=[];const track={kind:'audio',id:'mic'};
  const call=new BecomingCall({Daily:daily.sdk,Stream:class{constructor(tracks){this.tracks=tracks;}},createAudio:()=>({play:async function(){played.push(this.srcObject.tracks[0]);},pause(){},remove(){}})});
  await call.start({url:'https://raneen.daily.co/synthetic-room',token:'synthetic-room-token',microphoneTrack:track});
  assert.equal(daily.calls[0].audioSource,track);assert.equal(daily.calls[0].videoSource,false);
  daily.emit('track-started',{participant:{local:true},track});assert.equal(played.length,0);
  const remote={kind:'audio',id:'remote'};daily.emit('track-started',{participant:{local:false},track:remote});await Promise.resolve();assert.deepEqual(played,[remote]);
  daily.emit('app-message',{data:{type:'assistant.started',newAssistant:{voice:{provider:'11labs',voiceId:'synthetic-voice'}}}});
  assert.equal(normalizeProviderEvent({data:{type:'unsafe-control-config',controlUrl:'private',server:{headers:{Authorization:'private'}}}}),null,'native Daily config messages are ignored');
  assert.equal(daily.calls.filter(item=>typeof item==='object').length,1,'a handoff must not create a second web call');
  await call.stop();assert.equal(call.audios.size,0);assert.deepEqual(daily.calls.slice(1),['leave','destroy']);
});

test('Daily rejects non-provider destinations and repeated call starts',async()=>{
  for(const url of ['http://raneen.daily.co/room','https://daily.co.example.com/room','https://raneen.daily.co:8443/room','https://user@raneen.daily.co/room','https://raneen.daily.co/room#fragment']){
    const call=new BecomingCall({Daily:fakeDaily().sdk});await assert.rejects(call.start({url,microphoneTrack:{kind:'audio'}}),/invalid_call_room/);
  }
  const call=new BecomingCall({Daily:fakeDaily().sdk});await call.start({url:'https://raneen.daily.co/room',microphoneTrack:{kind:'audio'}});await assert.rejects(call.start({url:'https://raneen.daily.co/room',microphoneTrack:{kind:'audio'}}),/call_already_started/);await call.stop();
});

function browserFixture({permissionDenied=false,configured=true,serverStates=['COLLECTING_VOICE'],savedSession=null,retentionDays=7}={}){
  const elements=new Map(),requests=[],listeners=new Map(),daily=fakeDaily(),sources=[];let stopped=0,closed=0,gets=0;const currentId=savedSession??'b'.repeat(32);
  const element=id=>{if(!elements.has(id))elements.set(id,{id,dataset:{},textContent:'',hidden:false,checked:false,disabled:false,setAttribute(){},getAttribute(name){return this[name];},removeAttribute(name){delete this[name];},pause(){},querySelector(){return element(id+'Label');}});return elements.get(id);};
  const track={kind:'audio',stop:()=>stopped++,getSettings:()=>({sampleRate:48000,echoCancellation:true})};
  element('retention').dataset.copy='retention';
  globalThis.document={documentElement:{},getElementById:element,querySelectorAll:()=>[element('retention')],createElement:()=>({play:async()=>{},pause(){},remove(){}})};
  globalThis.window={confirm:()=>true,addEventListener:(name,fn)=>listeners.set(name,fn)};
  globalThis.location={search:'',origin:'https://synthetic.example'};
  const storage=new Map(savedSession?[['raneen-becoming-session',savedSession]]:[]);globalThis.localStorage={setItem:(key,value)=>storage.set(key,value),getItem:key=>storage.get(key)??null,removeItem:key=>storage.delete(key)};
  if(!globalThis.crypto)Object.defineProperty(globalThis,'crypto',{configurable:true,value:webcrypto});globalThis.DailyIframe=daily.sdk;globalThis.MediaStream=class{constructor(tracks){this.tracks=tracks;}};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>{if(permissionDenied){const error=new Error('denied');error.name='NotAllowedError';throw error;}return {getTracks:()=>[track],getAudioTracks:()=>[track]};}}}});
  globalThis.AudioContext=class{constructor(){this.sampleRate=24000;this.currentTime=0;this.state='running';this.audioWorklet={addModule:async()=>{}};this.destination={};}async resume(){}createMediaStreamSource(){return {connect(){},disconnect(){}};}createAnalyser(){return {fftSize:512,getFloatTimeDomainData(samples){samples.fill(0);},disconnect(){}};}async close(){closed++;this.state='closed';}};
  globalThis.AudioWorkletNode=class{constructor(){this.port={postMessage(){},onmessage:null};}connect(){}disconnect(){}};
  globalThis.EventSource=class{constructor(url,options){this.url=url;this.options=options;this.handlers=new Map();this.closed=false;sources.push(this);queueMicrotask(()=>this.onopen?.());}addEventListener(name,fn){this.handlers.set(name,fn);}emit(name,data){this.handlers.get(name)?.({data:JSON.stringify({...data,event_at_ms:Date.now(),relay_at_ms:Date.now(),received_at_ms:Date.now(),call_id:'synthetic-call'})});}close(){this.closed=true;}};
  globalThis.fetch=async(url,options={})=>{
    requests.push({url,options});let value={};
    if(url.endsWith('/readiness'))value={enabled:true,configured:configured?{vapi:true,elevenlabs:true,template:true}:{vapi:true,elevenlabs:false,template:true},minimum_speech_seconds:30,max_duration_seconds:90,retention_days:retentionDays};
    else if(url==='/api/becoming/sessions')value={id:currentId,capability:'synthetic-private-capability',state:'IDLE'};
    else if(url.endsWith('/call'))value={call_id:'synthetic-call',web_call_url:'https://raneen.daily.co/synthetic-room',call_token:'synthetic-room-token',max_duration_seconds:90};
    else if(url==='/api/becoming/sessions/'+currentId&&options.method==='GET')value={id:currentId,state:serverStates[Math.min(gets++,serverStates.length-1)],eligible_audio_seconds:31,voice_id:'synthetic-voice',voice_ready:true};
    else if(url.endsWith('/end'))value={state:'ENDED'};
    else if(url.endsWith('/revoke'))value={state:'REVOKED'};
    return {ok:true,status:200,json:async()=>value};
  };
  return {element,requests,daily,sources,listeners,storage,stopped:()=>stopped,closed:()=>closed,async load(){await import('../studio/static/become.js?test='+Math.random());await new Promise(resolve=>setTimeout(resolve,5));},async create(){element('consent').checked=true;element('consent').onchange();await element('create').onclick();},async cleanup(){listeners.get('pagehide')?.();await new Promise(resolve=>setTimeout(resolve,5));}};
}

test('microphone denial creates no paid call or anonymous session',async()=>{
  const fixture=browserFixture({permissionDenied:true});await fixture.load();await fixture.create();
  assert.equal(fixture.element('experience').dataset.state,'FAILED');
  assert.ok(!fixture.requests.some(request=>request.url==='/api/becoming/sessions'||request.url.endsWith('/call')));
  assert.equal(fixture.element('notice').hidden,false);await fixture.cleanup();
});

test('configured-provider map must be fully ready before create can run',async()=>{
  const fixture=browserFixture({configured:false});await fixture.load();await fixture.create();
  assert.equal(fixture.element('create').disabled,true);assert.equal(fixture.element('experience').dataset.state,'IDLE');assert.equal(fixture.requests.length,1);await fixture.cleanup();
});

test('handoff acceptance stays SWITCHING; remote revoke stops local tracks and one existing call',async()=>{
  const fixture=browserFixture({serverStates:['SWITCHING_VOICE','REVOKED']});await fixture.load();await fixture.create();await new Promise(resolve=>setTimeout(resolve,10));
  fixture.daily.emit('app-message',{data:{type:'assistant.started',newAssistant:{voice:{provider:'11labs',voiceId:'synthetic-voice'}}}});
  fixture.daily.emit('app-message',{data:{type:'speech-update',role:'assistant',status:'started'}});
  fixture.sources[0].emit('provider_event',{type:'assistant.started',new_assistant_voice:{provider:'11labs',voice_id:'synthetic-voice'}});
  fixture.sources[0].emit('provider_event',{type:'speech-update',role:'assistant',status:'started'});
  assert.equal(fixture.element('experience').dataset.state,'SWITCHING_VOICE','client event never manufactures active state');
  await new Promise(resolve=>setTimeout(resolve,1550));
  assert.equal(fixture.element('experience').dataset.state,'REVOKED');assert.ok(fixture.stopped()>0);assert.ok(fixture.closed()>0);
  assert.equal(fixture.daily.calls.filter(item=>typeof item==='object').length,1);assert.ok(fixture.daily.calls.includes('destroy'));
  assert.equal(fixture.sources.length,1);assert.equal(fixture.sources[0].closed,true);assert.equal(fixture.sources[0].options.withCredentials,true);assert.ok(!fixture.sources[0].url.includes('?'));
  assert.ok(fixture.requests.filter(request=>request.url.endsWith('/events')).every(request=>!request.options.body.includes('synthetic-private-capability')));
  await fixture.cleanup();
});

test('normal end promptly releases the microphone and asks the server to close the call',async()=>{
  const fixture=browserFixture({serverStates:['CLONED_ACTIVE']});await fixture.load();await fixture.create();await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(fixture.element('experience').dataset.state,'CLONED_ACTIVE');
  const ending=fixture.element('stop').onclick();assert.ok(fixture.stopped()>0,'track release precedes network cleanup');await ending;
  assert.equal(fixture.element('experience').dataset.state,'ENDED');assert.ok(fixture.requests.some(request=>request.url.endsWith('/end')));await fixture.cleanup();
});

test('refresh restores only an id through its cookie and never starts another paid call or clone',async()=>{
  const id='a'.repeat(32),fixture=browserFixture({savedSession:id,serverStates:['SWITCHING_VOICE']});
  await fixture.load();
  assert.equal(fixture.element('experience').dataset.state,'ENDED');assert.equal(fixture.stopped(),0);assert.equal(fixture.daily.calls.length,0);
  assert.ok(!fixture.requests.some(request=>request.url==='/api/becoming/sessions'||request.url.endsWith('/call')||request.url.endsWith('/process')));
  assert.ok(fixture.requests.some(request=>request.url.endsWith('/'+id+'/end')));
  assert.deepEqual([...fixture.storage.values()],[id]);
  assert.ok(fixture.requests.every(request=>!request.options.headers.Authorization));
  assert.equal(fixture.element('savedVoice').hidden,false);assert.equal(fixture.element('voicePreview').src,'/api/becoming/sessions/'+id+'/voice-check');await fixture.cleanup();
});

test('subminimum final PCM fragments are excluded rather than uploaded as misleading duration',async()=>{
  let uploads=0;const capture=new BecomingCapture({upload:async item=>{uploads++;return {seq:item.seq};},hash:async()=> 'checksum'});
  capture.parts=[new Float32Array(2048).fill(.1)];capture.partSamples=2048;await capture.stop();
  assert.equal(uploads,0);assert.equal(capture.partSamples,0);
});

test('retention copy uses configured days in Arabic and English and promises scheduled deletion',async()=>{
  const fixture=browserFixture({retentionDays:14});await fixture.load();
  assert.ok(fixture.element('retention').textContent.includes('14 أيام'));assert.ok(fixture.element('retention').textContent.includes('يُجدول'));
  fixture.element('language').onclick();
  assert.ok(fixture.element('retention').textContent.includes('14 days'));assert.ok(fixture.element('retention').textContent.includes('scheduled automatically'));assert.ok(fixture.element('retention').textContent.includes('delete them sooner'));await fixture.cleanup();
});

function relayFixture(){
  const received=[],errors=[],sources=[];let opens=0;
  class Source{constructor(url,options){this.url=url;this.options=options;this.handlers=new Map();sources.push(this);}addEventListener(name,fn){this.handlers.set(name,fn);}emit(name,value,cursor=''){this.handlers.get(name)?.({data:JSON.stringify(value),lastEventId:cursor});}close(){this.closed=true;}}
  const stream=new BecomingEventStream({Source,now:()=>10000,monotonic:()=>2000,onEvent:(message,at)=>received.push({message,at}),onError:error=>errors.push(error.message),onOpen:()=>opens++});
  stream.start('a'.repeat(32));sources[0].onopen();
  return {stream,source:sources[0],sources,received,errors,opens:()=>opens,send:(value,cursor)=>sources[0].emit('provider_event',{relay_at_ms:10000,received_at_ms:10000,event_time_source:'provider_timestamp',call_id:'synthetic-call',...value},cursor)};
}

test('safe SSE uses scoped cookies and rejects stale, duplicate and reordered speaker events',()=>{
  const fixture=relayFixture();
  assert.equal(fixture.source.url,'/api/becoming/sessions/'+'a'.repeat(32)+'/events-stream');assert.deepEqual(fixture.source.options,{withCredentials:true});assert.ok(!fixture.source.url.includes('?'));
  fixture.send({type:'speech-update',role:'user',status:'started',event_at_ms:9800},'1');
  fixture.send({type:'speech-update',role:'user',status:'stopped',event_at_ms:9900},'2');
  fixture.send({type:'speech-update',role:'user',status:'started',event_at_ms:9850},'3');
  fixture.send({type:'speech-update',role:'user',status:'stopped',event_at_ms:9900},'2');
  fixture.send({type:'speech-update',role:'user',status:'started',event_at_ms:8000},'4');
  assert.equal(fixture.received.length,2);assert.deepEqual(fixture.received.map(value=>value.at),[1800,1900]);assert.ok(fixture.errors.includes('event_stream_stale'));
  fixture.stream.stop();assert.equal(fixture.source.closed,true);
});

test('SSE loss pauses capture and bounds reconnect attempts without constructing another stream or call',()=>{
  const fixture=relayFixture();fixture.source.onerror();fixture.source.onopen();fixture.source.onerror();fixture.source.onopen();fixture.source.onerror();
  assert.equal(fixture.errors.length,3);assert.equal(fixture.sources.length,1);assert.equal(fixture.source.closed,true);assert.equal(fixture.stream.closed,true);
});

test('local playback veto remains independent of delayed provider assistant-stop events',()=>{
  const gate=new SpeakerGate();gate.update({type:'speech-update',role:'user',status:'started'},0);
  gate.playback(true,600);gate.update({type:'speech-update',role:'assistant',status:'stopped'},700);
  assert.equal(gate.permits(1000,1085),false,'an SSE stop cannot override audible remote playback');
  gate.playback(false,1300);assert.equal(gate.permits(1600,1685),false,'acoustic tail is excluded');assert.equal(gate.permits(1800,1885),true);
  gate.reset();assert.equal(gate.permits(2000,2085),false,'a lost stream invalidates a previous user-start gate');
});

test('remote analyser only vetoes capture; remote PCM is never connected to a worklet or output',async()=>{
  const daily=fakeDaily(),connections=[],vetoes=[];let level=0,closed=0;
  class Context{constructor(){this.state='running';}createMediaStreamSource(stream){return {connect:node=>connections.push({stream,node}),disconnect(){}};}createAnalyser(){return {kind:'analyser',fftSize:512,getFloatTimeDomainData(samples){samples.fill(level);},disconnect(){}};}async resume(){}async close(){this.state='closed';closed++;}}
  const call=new BecomingCall({Daily:daily.sdk,Context,Stream:class{constructor(tracks){this.tracks=tracks;}},createAudio:()=>({play:async()=>{},pause(){},remove(){}}),onPlayback:active=>vetoes.push(active)});
  await call.start({url:'https://raneen.daily.co/room',microphoneTrack:{kind:'audio',id:'mic'}});
  daily.emit('track-started',{participant:{local:false},track:{kind:'audio',id:'remote'}});
  await new Promise(resolve=>setTimeout(resolve,65));assert.equal(vetoes.at(-1),false);
  level=.2;await new Promise(resolve=>setTimeout(resolve,65));assert.equal(vetoes.at(-1),true);
  assert.equal(connections.length,1);assert.equal(connections[0].stream.tracks[0].id,'remote');assert.equal(connections[0].node.kind,'analyser');
  await call.stop();assert.equal(closed,1);assert.equal(call.monitors.size,0);
});
