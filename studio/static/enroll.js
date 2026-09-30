import {InterviewCapture} from './enrollment-capture.js';
import {copy} from './enrollment-copy.js';
// Retire the earlier enrollment credential cache. This flow keeps access in memory.
try { sessionStorage.removeItem('raneen-token'); } catch {}
const q=(s)=>document.querySelector(s), app=q('#app');
const S={lang:'ar',token:'',page:'login',sessions:[],session:null,journey:null,capture:null,transcript:'',lastItem:null,micEpoch:0,eventChain:Promise.resolve()};
try { if(localStorage.getItem('raneen-language')==='en') S.lang='en'; } catch {}
const remote=new Audio(); remote.autoplay=true;
const t=(key)=>copy[S.lang][key]||key;
const esc=(v)=>String(v??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(ms)=>{const s=Math.floor((ms||0)/1000);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');};
function notice(message,error=false){const n=q('#notice');n.textContent=message;n.className='show'+(error?' error-notice':'');clearTimeout(notice.timer);notice.timer=setTimeout(()=>n.className='',9000);}
function explain(e){
  const names={NotAllowedError:'denied',NotFoundError:'missing',NotReadableError:'busyMic'};
  const codes={recording_unsupported:'unsupported',save_audio_first:'saveFirst',connection_lost:'disconnected',audio_backpressure:'bufferFull',audio_not_saved:'saveFailed',audio_buffer_full:'bufferFull',final_audio_pending:'tailUnknown',recording_failed:'tailUnknown',upload_timeout:'saveFailed',audio_ack_missing:'saveFailed',request_timeout:'requestTimeout'};
  return t(names[e?.name]||codes[e?.message]||({401:'accessExpired',403:'accessExpired',503:'unavailable',409:'conflict',413:'tooLarge'}[e?.status])||'failed');
}
const report=(e)=>notice(explain(e),true);
async function request(url,options={}){
  const token=S.token;
  const ctrl=new AbortController(),external=options.signal,abort=()=>ctrl.abort();
  external?.addEventListener('abort',abort,{once:true});if(external?.aborted)ctrl.abort();
  const timer=setTimeout(abort,options.timeout||15000);
  try{
    const r=await fetch(url,{...options,headers:{Authorization:'Bearer '+token,...options.headers},credentials:'omit',signal:ctrl.signal});
    if(!r.ok)throw Object.assign(Error('request_failed'),{status:r.status});
    const result=options.text?await r.text():await r.json();
    if(token!==S.token)throw Object.assign(Error('access_changed'),{status:401});
    return result;
  }catch(e){if(e.name==='AbortError')throw Error('request_timeout');throw e;}
  finally{clearTimeout(timer);external?.removeEventListener('abort',abort);}
}
const post=(url,body={})=>request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const path=(suffix='')=>'/api/enrollment/sessions/'+encodeURIComponent(S.session)+suffix;
function bind(id,handler){const el=q(id);if(el)el.onclick=()=>Promise.resolve().then(handler).catch(report);}
function shell(content,withSteps=false){
  document.documentElement.lang=S.lang;document.documentElement.dir=S.lang==='ar'?'rtl':'ltr';document.title=t('title');
  app.innerHTML=`<div class="shell"><header class="topbar"><a class="brand" href="/enroll"><span class="brand-mark" aria-hidden="true">ر</span><span>${t('brand')}<small>${t('privateSpace')}</small></span></a><div class="header-actions"><button id="language" class="text-button" lang="${S.lang==='ar'?'en':'ar'}">${S.lang==='ar'?'English':'العربية'}</button>${S.token?`<button id="signout" class="text-button">${t('signout')}</button>`:''}</div></header>${withSteps?`<ol class="steps" aria-label="${t('journey')}">${['talk','prepare','test'].map((k,i)=>`<li ${i===0?'aria-current="step" class="current"':''}><span>${i+1}</span>${t(k)}</li>`).join('')}</ol>`:''}${content}<footer>${t('footer')}</footer></div>`;
  bind('#language',()=>{stopMic();S.lang=S.lang==='ar'?'en':'ar';try{localStorage.setItem('raneen-language',S.lang);}catch{}render();});
  bind('#signout',signout);
  q('.brand').onclick=(e)=>{e.preventDefault();if(!S.token)login();else leave().then((ok)=>ok&&home()).catch(report);};
}
function hero(title,text,eyebrow=''){return `<section class="welcome"><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p class="lead">${text}</p></section>`;}
function render(){({login,home:renderHome,consent:renderConsent,microphone:renderMic,interview:renderInterview}[S.page]||login)();}
function login(error=''){
  S.page='login';shell(hero(t('welcome'),t('intro'))+`<form id="loginForm" class="card login-card"><h2>${t('access')}</h2><p>${t('accessText')}</p>${error?`<p class="error" role="alert">${esc(error)}</p>`:''}<label for="token">${t('code')}</label><input id="token" type="password" required autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="codeNote"><p id="codeNote" class="small muted">${t('codeNote')}</p><button class="primary" id="login" type="submit">${t('continue')}</button></form>`);
  q('#loginForm').onsubmit=async(e)=>{e.preventDefault();const b=q('#login');if(b.disabled)return;b.disabled=true;S.token=q('#token').value.trim();try{const u=await request('/api/me');if(u.role!=='admin')throw Object.assign(Error(),{status:403});await home();}catch(e){S.token='';login(explain(e));}};
}
async function home(){stopMic();clearInterval(S.poll);const r=await request('/api/enrollment/sessions');S.sessions=r.sessions;S.enabled=r.enabled;S.page='home';renderHome();}
function renderHome(){
  S.page='home';const latest=S.sessions.find((s)=>!s.revoked&&!['complete','failed'].includes(s.state));
  shell(hero(latest?t('welcomeBack'):t('homeTitle'),t('homeText'))+`<section class="card feature-card"><div><span class="pill">${t('owner')}</span><h2>${latest?t('savedTitle'):t('newTitle')}</h2><p>${latest?`${t('saved')}: <strong dir="ltr">${fmt(latest.saved_audio_ms)}</strong>`:t('newText')}</p>${!S.enabled?`<p class="status-note">${t('disabled')}</p>`:''}</div><button id="mainAction" class="primary" ${!latest&&!S.enabled?'disabled':''}>${latest?(S.enabled?t('resume'):t('view')):t('begin')}</button></section><div class="journey-cards">${[['talk','talkText'],['prepare','prepareText'],['test','testText']].map(([title,txt],i)=>`<div><span>0${i+1}</span><h3>${t(title)}</h3><p>${t(txt)}</p>${i?`<span class="small muted">${t('later')}</span>`:''}</div>`).join('')}</div>${S.sessions.length?`<details class="card"><summary>${t('history')}</summary><ul class="session-list">${S.sessions.map((s,i)=>`<li><div><strong>${t('interview')} ${i+1}</strong><p class="small muted">${s.revoked?t('revoked'):t('saved')} · <span dir="ltr">${fmt(s.saved_audio_ms)}</span></p></div><button class="secondary" data-session="${esc(s.id)}">${t('open')}</button></li>`).join('')}</ul></details>`:''}`);
  bind('#mainAction',()=>latest?openSession(latest.id):consent());
  app.querySelectorAll('[data-session]').forEach((b)=>b.onclick=()=>openSession(b.dataset.session).catch(report));
}
async function consent(){S.consent=await request('/api/enrollment/consent');S.page='consent';renderConsent();}
function renderConsent(){
  shell(hero(t('consentTitle'),t('consentIntro'),t('before'))+`<form class="card" id="consentForm"><fieldset><legend>${t('consentLegend')}</legend>${['own','record','external','clone','preview'].map((k)=>`<label class="check"><input id="${k}" type="checkbox" required><span>${t(k)}</span></label>`).join('')}</fieldset><p class="status-note">${t('withdrawNote')}</p><details class="consent-details"><summary>${t('fullConsent')}</summary><p dir="auto" lang="en">${esc(S.consent.text)}</p></details><button id="accept" class="primary" type="submit">${t('accept')}</button><button id="back" type="button" class="text-button">${t('back')}</button></form>`,true);
  bind('#back',home);q('#consentForm').onsubmit=async(e)=>{e.preventDefault();const b=q('#accept');if(b.disabled)return;b.disabled=true;try{const r=await post('/api/enrollment/sessions',{self_attestation:q('#own').checked,recording:q('#record').checked,external_processing:q('#external').checked,voice_cloning:q('#clone').checked,private_preview:q('#preview').checked});await openSession(r.id,true);}catch(e){b.disabled=false;report(e);}};
}
async function openSession(id,mic=false){
  if(S.session!==id&&S.capture?.hasPending){notice(t('saveFirst'),true);return;}
  S.session=id;const journey=await request('/api/enrollment/sessions/'+encodeURIComponent(id)+'/journey');
  if(S.session!==id)return;S.journey=journey;
  if(!S.capture||S.capture.sessionId!==id)createCapture();
  S.capture.queue.syncNextSeq(S.journey.next_seq);S.page=mic&&S.journey.can_resume?'microphone':'interview';render();
  clearInterval(S.poll);S.poll=setInterval(()=>{if(S.page==='interview')refresh().catch(()=>{});},10000);
}
function renderMic(){
  S.page='microphone';shell(hero(t('micTitle'),t('micIntro'))+`<section class="card mic-card"><div class="mic-symbol" aria-hidden="true">♪</div><h2 id="micStatus">${t('micReady')}</h2><p>${t('headphones')}</p><div class="mic-meter" role="meter" aria-label="${t('micLevel')}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="micLevel"></span></div><p class="small muted">${t('micPrivacy')}</p><div class="actions"><button id="checkMic" class="primary">${t('checkMic')}</button><button id="micContinue" class="primary" hidden>${t('micContinue')}</button><button id="micBack" class="secondary">${t('back')}</button></div><p id="micTip" class="small" role="status"></p></section>`,true);
  bind('#checkMic',checkMic);const done=()=>{stopMic();S.page='interview';renderInterview();};bind('#micContinue',done);bind('#micBack',done);
}
function stopMic(){++S.micEpoch;cancelAnimationFrame(S.micFrame);S.micStream?.getTracks().forEach((x)=>x.stop());S.micStream=null;S.micContext?.close().catch(()=>{});S.micContext=null;}
async function checkMic(){
  const b=q('#checkMic');if(b.disabled)return;b.disabled=true;const epoch=++S.micEpoch;q('#micStatus').textContent=t('micAllow');
  try{
    if(!navigator.mediaDevices?.getUserMedia)throw Error('recording_unsupported');
    const stream=await navigator.mediaDevices.getUserMedia({audio:true,video:false});
    if(epoch!==S.micEpoch){stream.getTracks().forEach((x)=>x.stop());return;}
    S.micStream=stream;q('#micStatus').textContent=t('micSpeak');q('#micTip').textContent=t('micTip');b.hidden=true;q('#micContinue').hidden=false;
    const Context=window.AudioContext||window.webkitAudioContext;if(!Context)return;
    const ctx=new Context();S.micContext=ctx;await ctx.resume();if(epoch!==S.micEpoch)return;
    const analyser=ctx.createAnalyser();analyser.fftSize=256;ctx.createMediaStreamSource(stream).connect(analyser);const samples=new Uint8Array(analyser.fftSize);
    const tick=()=>{if(epoch!==S.micEpoch)return;analyser.getByteTimeDomainData(samples);const rms=Math.sqrt(samples.reduce((n,x)=>n+((x-128)/128)**2,0)/samples.length),level=Math.min(100,Math.round(rms*500));q('#micLevel').style.width=level+'%';q('.mic-meter').setAttribute('aria-valuenow',String(level));S.micFrame=requestAnimationFrame(tick);};tick();
  }catch(e){if(epoch===S.micEpoch){stopMic();renderMic();report(e);}}
}
function createCapture(){
  const id=S.session,base='/api/enrollment/sessions/'+encodeURIComponent(id);
  S.capture=new InterviewCapture({sessionId:id,nextSeq:S.journey.next_seq,
    upload:(item,signal)=>request(base+'/chunks/'+item.seq,{method:'PUT',signal,body:item.blob,headers:{'Content-Type':item.blob.type||'audio/webm','X-Speaker-Role':'contributor','X-Chunk-Sha256':item.checksum,'X-Duration-Ms':String(item.durationMs)}}),
    openConnection:(sdp)=>request(base+'/webrtc',{method:'POST',body:sdp,headers:{'Content-Type':'application/sdp'},text:true,timeout:35000}),
    closeConnection:async()=>{const r=await post(base+'/webrtc-close');if(S.session===id)await refresh();return r;},
    onState:update,onError:report,onAck:()=>{if(S.session===id)refresh().catch(()=>{});},
    onRemote:(stream)=>{remote.srcObject=stream;if(stream)remote.play().catch(()=>notice(t('blockedAudio'),true));},
    onEvent:(event,capture)=>{S.eventChain=S.eventChain.then(()=>realtime(event,capture)).catch(report);return S.eventChain;},
  });S.transcript='';S.lastItem=null;
}
function renderInterview(){
  S.page='interview';const j=S.journey;
  shell(hero(j.revoked?t('revokedTitle'):t('interviewTitle'),j.revoked?t('revokedText'):t('interviewIntro'),t('privateInterview'))+`<section class="card interview-card"><div class="interview-top"><span id="connectionStatus" class="pill" role="status">${t('off')}</span><span class="small muted">${t('target')}</span></div><div id="orb" class="orb" aria-hidden="true">ر</div><h2 id="interviewHint">${t('ready')}</h2><p id="interviewSupport">${t('reassurance')}</p><div class="saved-progress"><strong id="savedMinutes" dir="ltr">${fmt(j.saved_audio_ms)}</strong><span>${t('saved')}</span></div><p class="small muted">${t('durationNote')}</p><div id="uploadState" class="save-state" role="status"></div><p id="serverState" class="status-note" hidden></p><div class="actions"><button id="connect" class="primary">${t('start')}</button><button id="pause" class="secondary" hidden>${t('pause')}</button><button id="retryUploads" class="primary" hidden>${t('retry')}</button><button id="ackTail" class="secondary" hidden>${t('acknowledgeTail')}</button><button id="endPrevious" class="secondary" hidden>${t('endPrevious')}</button><button id="micCheck" class="text-button">${t('checkSound')}</button></div><p class="small muted">${t('headphones')}</p></section><details class="card"><summary>${t('details')}</summary><h3>${t('latest')}</h3><div id="transcript" class="transcript" dir="auto">${esc(S.transcript||t('emptyTranscript'))}</div><p class="small muted">${t('transcriptNote')}</p><p>${t('patterns')}: <strong id="patterns">${j.confirmed_patterns||0}</strong></p><button id="refresh" class="secondary">${t('refresh')}</button></details><section class="card next-stage"><div class="next-icon" aria-hidden="true">2</div><div><h2>${t('nextTitle')}</h2><p>${t('nextText')}</p><span class="pill neutral">${t('notAvailable')}</span></div></section><div class="bottom-actions"><button id="backHome" class="text-button">${t('backHome')}</button>${j.revoked?`<button id="cleanup" class="secondary">${t('cleanup')}</button>`:`<button id="revoke" class="text-button danger">${t('revoke')}</button>`}</div>${j.revoked?`<p class="small muted">${t('cleanupNote')}</p>`:''}`,true);
  bind('#connect',async()=>{try{if(S.journey.can_resume)await S.capture.start();}finally{update();}});
  bind('#pause',async()=>{await S.capture.pause();await refresh();});
  bind('#retryUploads',async()=>{const ok=await S.capture.retry();notice(ok?t('allSaved'):t('saveFailed'),!ok);await refresh();});
  bind('#ackTail',()=>{if(confirm(t('acknowledgeConfirm')))S.capture.acknowledgeMissingTail();});
  bind('#endPrevious',async()=>{q('#endPrevious').disabled=true;try{await post(path('/webrtc-close'));await refresh();}finally{if(q('#endPrevious'))q('#endPrevious').disabled=false;}});
  bind('#micCheck',async()=>{if(await leave()){S.page='microphone';renderMic();}});
  bind('#refresh',refresh);bind('#backHome',async()=>{if(await leave())await home();});bind('#revoke',revoke);
  bind('#cleanup',async()=>{await post(path('/cleanup'));notice(t('cleanupNote'));await refresh();});update();
}
function update(){
  if(S.page!=='interview'||!S.capture||!q('#connect'))return;
  const c=S.capture,j=S.journey,live=c.state==='live',connecting=c.state==='connecting',pending=c.hasPending;
  const previous=!live&&!connecting&&['open','close_unknown'].includes(j.interview_call_state);
  const missingOnly=c.uncertainTail&&!c.queue.hasPending&&!c.unsavedFinal&&!c.pausing;
  const oversize=c.queue.items.some((x)=>x.blob.size>80*1024)||c.unsavedFinal?.blob.size>80*1024;
  q('#connectionStatus').textContent=t(j.revoked?'revoked':live?'active':connecting?'connecting':'off');q('#orb').classList.toggle('live',live);
  q('#interviewHint').textContent=t(j.revoked?'revoked':live?'listening':connecting?'connectingHint':pending?'saving':j.can_resume?'ready':'off');
  q('#interviewSupport').textContent=t(live?'liveNote':connecting?'permissionWait':'reassurance');
  q('#connect').hidden=live||connecting||pending||previous||j.revoked;q('#connect').disabled=!j.can_resume||Boolean(c.starting||c.pausing);q('#connect').textContent=t(j.chunk_count?'resumeTalk':'start');
  q('#pause').hidden=!(live||connecting);q('#pause').textContent=t(connecting?'cancel':'pause');
  q('#retryUploads').hidden=!pending||live||connecting||j.revoked||missingOnly||oversize;q('#retryUploads').disabled=Boolean(c.queue.running||c.pausing);
  q('#ackTail').hidden=!missingOnly||j.revoked;q('#endPrevious').hidden=!previous;
  q('#micCheck').disabled=live||connecting||pending||j.revoked;
  q('#uploadState').textContent=j.revoked?t('revokedText'):oversize?t('tooLarge'):missingOnly?t('lostTail'):c.uncertainTail||c.unsavedFinal?t('tailUnknown'):pending?`${t('waiting')} ${c.queue.items.length}. ${t('keepOpen')}`:j.chunk_count?t('allSaved'):t('noAudio');
  q('#uploadState').classList.toggle('unsaved',pending);
  const key=previous?(j.interview_call_state==='close_unknown'?'closeUnknown':'previousOpen'):j.revoked&&['manual_reconciliation','pending'].includes(j.cleanup_state)?'cleanupNote':j.revoked?'':!j.enabled?'disabled':!j.can_resume&&!live&&!connecting?'reviewNeeded':'';
  q('#serverState').hidden=!key;q('#serverState').textContent=key?t(key):'';
}
async function refresh(){
  const id=S.session;if(!id)return;const j=await request(path('/journey'));if(id!==S.session)return;
  S.journey=j;S.capture?.queue.syncNextSeq(j.next_seq);
  if(j.revoked&&(S.capture?.stream||S.capture?.starting))S.capture.pause({save:false}).catch(report);
  if(q('#savedMinutes'))q('#savedMinutes').textContent=fmt(j.saved_audio_ms);if(q('#patterns'))q('#patterns').textContent=j.confirmed_patterns||0;update();
}
async function realtime(event,capture){
  if(capture!==S.capture||S.journey?.revoked)return;
  const base='/api/enrollment/sessions/'+encodeURIComponent(capture.sessionId);
  if(event.type==='raneen.connected')capture.send({type:'response.create',response:{instructions:'Introduce yourself briefly as Raneen’s private AI interviewer. Ask in Arabic which language and dialect the contributor prefers, respect their spoken choice, and ask one natural question at a time. Do not claim a finished clone.'}});
  else if(event.type==='conversation.item.input_audio_transcription.completed'){
    await post(base+'/transcripts',{item_id:event.item_id,transcript:event.transcript||''});
    if(capture!==S.capture||S.journey?.revoked)return;
    capture.lastItem=event.item_id;S.transcript=event.transcript||'';if(q('#transcript'))q('#transcript').textContent=S.transcript;
  }else if(event.type==='response.done'&&Array.isArray(event.response?.output)){
    for(const item of event.response.output){if(item.type!=='function_call')continue;let args;try{args=JSON.parse(item.arguments||'{}');}catch{continue;}
      if(capture!==S.capture||S.journey?.revoked)return;
      const result=await post(base+'/tool',{call_id:item.call_id,name:item.name,arguments:args,source_item_id:capture.lastItem||null});
      if(capture!==S.capture||S.journey?.revoked)return;
      capture.send({type:'conversation.item.create',item:{type:'function_call_output',call_id:item.call_id,output:JSON.stringify(result)}});capture.send({type:'response.create'});
    }
  }else if(event.type==='error')notice(t('interviewerError'),true);
}
async function leave(){stopMic();const capture=S.capture;if(capture)await capture.pause();if(capture!==S.capture)return false;if(capture?.hasPending){notice(t('keepOpen'),true);return false;}return true;}
async function revoke(){
  if(!confirm(t('revokeConfirm')))return;const id=S.session,capture=S.capture;stopMic();capture?.pause({save:false}).catch(report);q('#revoke').disabled=true;
  try{await post('/api/enrollment/sessions/'+encodeURIComponent(id)+'/revoke',{confirm:true});if(S.session!==id||S.capture!==capture)return;S.capture=null;await openSession(id);notice(t('revokedText'));}catch(e){if(S.session===id&&q('#revoke'))q('#revoke').disabled=false;report(e);}
}
async function signout(){if(!await leave())return;clearInterval(S.poll);S.token='';S.session=null;S.capture=null;S.journey=null;S.sessions=[];S.transcript='';login();}
window.addEventListener('beforeunload',(e)=>{if(S.capture?.hasPending||S.capture?.stream||S.capture?.starting){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',()=>{stopMic();S.capture?.stopLocal();});
window.addEventListener('offline',()=>{if(S.capture?.stream||S.capture?.starting)S.capture.pause().then(()=>notice(t('disconnected'),true)).catch(report);});
login();
