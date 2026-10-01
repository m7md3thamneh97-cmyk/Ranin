import {InterviewCapture} from './enrollment-capture.js';
import {copy} from './enrollment-copy.js';
// Retire the earlier enrollment credential cache. This flow keeps access in memory.
try { sessionStorage.removeItem('raneen-token'); } catch {}
const q=(s)=>document.querySelector(s), app=q('#app');
const S={lang:'ar',token:'',page:'login',sessions:[],session:null,journey:null,capture:null,transcript:'',lastItem:null,micEpoch:0,eventChain:Promise.resolve(),workflow:null,busy:false,phase:'',samples:{},listened:new Set(),correction:false,call:null,callNonce:'',callState:'idle',workflowEpoch:0,resuming:false,refreshSeq:0};
try { if(localStorage.getItem('raneen-language')==='en') S.lang='en'; } catch {}
const remote=new Audio(); remote.autoplay=true;
const t=(key)=>copy[S.lang][key]??key;
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
    if(!r.ok){let detail='';try{detail=(await r.json()).detail||'';}catch{}throw Object.assign(Error('request_failed'),{status:r.status,detail});}
    const result=options.blob?await r.blob():options.text?await r.text():await r.json();
    if(token!==S.token)throw Object.assign(Error('access_changed'),{status:401});
    return result;
  }catch(e){if(e.name==='AbortError')throw Error('request_timeout');throw e;}
  finally{clearTimeout(timer);external?.removeEventListener('abort',abort);}
}
const post=(url,body={},extra={})=>request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),...extra});
const path=(suffix='')=>'/api/enrollment/sessions/'+encodeURIComponent(S.session)+suffix;
function bind(id,handler){const el=q(id);if(el)el.onclick=()=>Promise.resolve().then(handler).catch(report);}
function shell(content,withSteps=false){
  document.documentElement.lang=S.lang;document.documentElement.dir=S.lang==='ar'?'rtl':'ltr';document.title=t('title');
  app.innerHTML=`<div class="shell"><header class="topbar"><a class="brand" href="/enroll"><span class="brand-mark" aria-hidden="true">ر</span><span>${t('brand')}<small>${t('privateSpace')}</small></span></a><div class="header-actions"><button id="language" class="text-button" lang="${S.lang==='ar'?'en':'ar'}">${S.lang==='ar'?'English':'العربية'}</button>${S.token?`<button id="signout" class="text-button">${t('signout')}</button>`:''}</div></header>${withSteps?`<ol class="steps" aria-label="${t('journey')}">${['talk','prepare','test'].map((k,i)=>`<li ${i===(['prepare'].includes(S.page)?1:S.page==='agent'?2:0)?'aria-current="step" class="current"':''}><span>${i+1}</span>${t(k)}</li>`).join('')}</ol>`:''}${content}<footer>${t('footer')}</footer></div>`;
  bind('#language',()=>{if(S.busy||S.call){notice(t(S.call?'callAway':'preparingBackground'),true);return;}stopMic();S.lang=S.lang==='ar'?'en':'ar';try{localStorage.setItem('raneen-language',S.lang);}catch{}render();});
  bind('#signout',signout);
  q('.brand').onclick=(e)=>{e.preventDefault();if(!S.token)login();else leave().then((ok)=>ok&&home()).catch(report);};
}
function hero(title,text,eyebrow=''){return `<section class="welcome">${eyebrow?`<p class="eyebrow">${eyebrow}</p>`:''}<h1>${title}</h1>${text?`<p class="lead">${text}</p>`:''}</section>`;}
function render(){({login,home:renderHome,consent:renderConsent,microphone:renderMic,interview:renderInterview,prepare:renderPrepare,agent:renderAgent}[S.page]||login)();}
function login(error=''){
  S.page='login';shell(hero(t('welcome'),t('intro'))+`<div class="journey-cards login-journey">${[['talk','talkText'],['prepare','prepareText'],['test','testText']].map(([title,txt],i)=>`<div><span>0${i+1}</span><h3>${t(title)}</h3><p>${t(txt)}</p></div>`).join('')}</div><form id="loginForm" class="card login-card"><h2>${t('access')}</h2><p>${t('accessText')}</p>${error?`<p class="error" role="alert">${esc(error)}</p>`:''}<label for="token">${t('code')}</label><input id="token" type="password" required autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="codeNote"><p id="codeNote" class="small muted">${t('codeNote')}</p><button class="primary" id="login" type="submit">${t('continue')}</button></form>`);
  q('#loginForm').onsubmit=async(e)=>{e.preventDefault();const b=q('#login');if(b.disabled)return;b.disabled=true;S.token=q('#token').value.trim();q('#token').value='';try{const u=await request('/api/me');if(u.role!=='admin')throw Object.assign(Error(),{status:403});await home();}catch(e){S.token='';login(explain(e));}};
}
async function home(){stopMic();clearInterval(S.poll);const r=await request('/api/enrollment/sessions');S.sessions=r.sessions;S.enabled=r.enabled;const readiness=await request('/api/enrollment/readiness');S.config={interview:readiness.interview_configured,voice:readiness.voice_configured,agent:readiness.agent_configured};S.page='home';renderHome();}
function renderHome(){
  S.page='home';const latest=S.sessions.find((s)=>!s.revoked&&!['complete','failed'].includes(s.state));
  shell(hero(latest?t('welcomeBack'):t('homeTitle'),t('homeText'))+`<section class="card feature-card"><div><span class="pill">${t('owner')}</span><h2>${latest?t('savedTitle'):t('newTitle')}</h2><p>${latest?`${t('saved')}: <strong dir="ltr">${fmt(latest.saved_audio_ms)}</strong>`:t('newText')}</p>${!S.enabled?`<p class="status-note">${t('disabled')}</p>`:''}</div><button id="mainAction" class="primary" ${!latest&&!S.enabled?'disabled':''}>${latest?(S.enabled?t('resume'):t('view')):t('begin')}</button></section><div class="journey-cards">${[['talk','talkText'],['prepare','prepareText'],['test','testText']].map(([title,txt],i)=>`<div><span>0${i+1}</span><h3>${t(title)}</h3><p>${t(txt)}</p></div>`).join('')}</div>${configurationCard(S.config)}${S.sessions.length?`<details class="card"><summary>${t('history')}</summary><ul class="session-list">${S.sessions.map((s,i)=>`<li><div><strong>${t('interview')} ${i+1}</strong><p class="small muted">${s.revoked?t('revoked'):t('saved')} · <span dir="ltr">${fmt(s.saved_audio_ms)}</span></p></div><button class="secondary" data-session="${esc(s.id)}">${t('open')}</button></li>`).join('')}</ul></details>`:''}`);
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
  if(S.busy||S.call){notice(t(S.call?'callAway':'preparingBackground'),true);return;}
  if(S.session!==id){clearSamples();S.workflow=null;S.correction=false;}
  S.session=id;const journey=await request('/api/enrollment/sessions/'+encodeURIComponent(id)+'/journey');
  if(S.session!==id)return;S.journey=journey;
  if(!S.capture||S.capture.sessionId!==id)createCapture();
  S.capture.queue.syncNextSeq(S.journey.next_seq);
  S.workflow=await request('/api/enrollment/sessions/'+encodeURIComponent(id)+'/workflow');if(S.session!==id)return;
  if(['open','close_unknown','dispatching','outcome_unknown'].includes(S.workflow.preview_call_state))S.call={sessionId:id,recovered:true};
  S.page=mic&&S.journey.can_resume?'microphone':S.journey.revoked?'interview':S.workflow.stage==='agent_ready'?'agent':['preparing','voice_review','blocked'].includes(S.workflow.stage)?'prepare':'interview';render();
  clearInterval(S.poll);S.poll=setInterval(()=>{if(['interview','prepare','agent'].includes(S.page))refresh().catch(()=>{});},10000);
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
    openConnection:(sdp)=>request(base+'/webrtc',{method:'POST',body:sdp,headers:{'Content-Type':'application/sdp'},text:true,timeout:60000}),
    closeConnection:async()=>{const r=await post(base+'/webrtc-close');if(S.session===id)await refresh();return r;},
    onState:update,onError:report,onAck:()=>{if(S.session===id)refresh().catch(()=>{});},
    onRemote:(stream)=>{remote.srcObject=stream;if(stream)remote.play().catch(()=>notice(t('blockedAudio'),true));},
    onEvent:(event,capture)=>{S.eventChain=S.eventChain.then(()=>realtime(event,capture)).catch(report);return S.eventChain;},
  });S.transcript='';S.lastItem=null;
}
function renderInterview(){
  S.page='interview';const j=S.journey;
  shell(hero(j.revoked?t('revokedTitle'):t(S.correction?'correctionTitle':'interviewTitle'),j.revoked?t('revokedText'):t(S.correction?'correctionIntro':'interviewIntro'))+`<section class="card interview-card"><div class="interview-top"><span id="connectionStatus" class="pill" role="status">${t('off')}</span><span id="savedMinutes" class="small" dir="ltr">${fmt(j.saved_audio_ms)}</span></div><div id="orb" class="orb" aria-hidden="true">ر</div><h2 id="interviewHint">${t('ready')}</h2><p id="interviewSupport">${t('reassurance')}</p><div id="uploadState" class="save-state" role="status"></div><p id="serverState" class="status-note" hidden></p><div class="actions"><button id="connect" class="primary">${t('start')}</button><button id="newInterview" class="primary" hidden>${t('newInterview')}</button><button id="pause" class="secondary" hidden>${t('pause')}</button><button id="finish" class="secondary">${t(S.correction?'updateAgent':'finish')}</button></div><p class="small muted">${t('target')}</p></section><details class="card"><summary>${t('details')}</summary><p>${t('headphones')}</p><p class="small muted">${t('durationNote')}</p><button id="retryUploads" class="secondary" hidden>${t('retry')}</button><button id="ackTail" class="secondary" hidden>${t('acknowledgeTail')}</button><button id="endPrevious" class="secondary" hidden>${t('endPrevious')}</button><button id="micCheck" class="text-button">${t('checkSound')}</button><h3>${t('latest')}</h3><div id="transcript" class="transcript" dir="auto">${esc(S.transcript||t('emptyTranscript'))}</div><p>${t('patterns')}: <strong id="patterns">${j.confirmed_patterns||0}</strong></p><button id="refresh" class="text-button">${t('refresh')}</button></details><div class="bottom-actions"><button id="backHome" class="text-button">${t('backHome')}</button>${j.revoked?`<button id="cleanup" class="secondary">${t('cleanup')}</button>`:`<button id="revoke" class="text-button danger">${t('revoke')}</button>`}</div>${j.revoked?`<p class="small muted">${t('cleanupNote')}</p>`:''}`,true);
  bind('#finish',finishInterview);bind('#connect',resumeInterview);bind('#newInterview',async()=>{if(S.resuming||S.busy)return;S.resuming=true;update();try{if(await settleInterview()&&!S.journey.provider_pending)await consent();}finally{S.resuming=false;update();}});
  bind('#pause',async()=>{await S.capture.pause();await refresh();});
  bind('#retryUploads',async()=>{const ok=await S.capture.retry();notice(ok?t('allSaved'):t('saveFailed'),!ok);await refresh();});
  bind('#ackTail',()=>{if(confirm(t('acknowledgeConfirm')))S.capture.acknowledgeMissingTail();});
  bind('#endPrevious',async()=>{await post(path('/webrtc-close'),{},{timeout:30000});await refresh();});
  bind('#micCheck',async()=>{if(await leave()){S.page='microphone';renderMic();}});
  bind('#refresh',refresh);bind('#backHome',async()=>{if(await leave())await home();});bind('#revoke',revoke);
  bind('#cleanup',async()=>{await post(path('/cleanup'));notice(t('cleanupNote'));await refresh();});update();
}
// One contributor action settles saved bytes and a known previous call before
// opening another. Uncertain provider creation and unavailable audio stay explicit.
async function settleInterview(){
  const c=S.capture,id=S.session,epoch=S.workflowEpoch,page=S.page,token=S.token,same=()=>S.capture===c&&S.session===id&&S.workflowEpoch===epoch&&S.page===page&&S.token===token&&!S.busy&&!S.journey.revoked;
  if(!c||S.busy||S.call||!same())return false;
  if(c.stream||c.starting)await c.pause();else if(c.pausing)await c.pausing;
  if(!same())return false;
  if(c.queue.hasPending||c.unsavedFinal)await c.retry();
  if(!same())return false;
  if(c.queue.hasPending||c.unsavedFinal){notice(t('saveFailed'),true);return false;}
  if(c.uncertainTail){if(!confirm(t('acknowledgeConfirm')))return false;c.acknowledgeMissingTail();}
  await refresh();if(!same())return false;
  if(['open','close_unknown'].includes(S.journey.interview_call_state)){
    await post('/api/enrollment/sessions/'+encodeURIComponent(id)+'/webrtc-close',{},{timeout:30000});
    await refresh();if(!same())return false;
  }
  if(['dispatching','outcome_unknown'].includes(S.journey.interview_call_state)){notice(t('outcomePending'),true);return false;}
  return !c.hasPending;
}
async function resumeInterview(){
  if(S.resuming||S.busy)return;S.resuming=true;update();
  try{
    if(!await settleInterview())return;
    if(!S.journey.can_resume){notice(t(S.journey.resume_limit?'interviewLimit':'reviewNeeded'),true);return;}
    await S.capture.start();
  }finally{S.resuming=false;update();}
}
function update(){
  if(S.page!=='interview'||!S.capture||!q('#connect'))return;
  const c=S.capture,j=S.journey,live=c.state==='live',connecting=c.state==='connecting',pending=c.hasPending;
  const previous=!live&&!connecting&&['open','close_unknown'].includes(j.interview_call_state);
  const missingOnly=c.uncertainTail&&!c.queue.hasPending&&!c.unsavedFinal&&!c.pausing;
  const oversize=c.queue.items.some((x)=>x.blob.size>256*1024)||c.unsavedFinal?.blob.size>256*1024;
  q('#connectionStatus').textContent=t(j.revoked?'revoked':live?'active':connecting?'connecting':'off');q('#orb').classList.toggle('live',live);
  q('#interviewHint').textContent=t(j.revoked?'revoked':live?'listening':connecting?'connectingHint':pending?'saving':j.can_resume?'ready':'off');
  q('#interviewSupport').textContent=t(live?'liveNote':connecting?'permissionWait':'reassurance');
  q('#newInterview').hidden=!j.resume_limit||live||connecting||j.revoked;q('#newInterview').disabled=!j.enabled||S.resuming||S.busy;
  q('#connect').hidden=live||connecting||j.revoked||Boolean(j.resume_limit);q('#connect').disabled=!j.enabled||S.busy||S.resuming||Boolean(c.starting)||Boolean(j.resume_limit)||(!j.can_resume&&!previous&&!pending);q('#connect').textContent=t(j.chunk_count?'resumeTalk':'start');
  q('#pause').hidden=!(live||connecting);q('#pause').textContent=t(connecting?'cancel':'pause');
  q('#retryUploads').hidden=!pending||live||connecting||j.revoked||missingOnly||oversize;q('#retryUploads').disabled=Boolean(c.queue.running||c.pausing);
  q('#ackTail').hidden=!missingOnly||j.revoked;q('#endPrevious').hidden=!previous;
  q('#micCheck').disabled=live||connecting||pending||j.revoked;
  if(q('#finish'))q('#finish').disabled=!j.enabled||(!j.chunk_count&&!c.queue.hasPending)||connecting||S.busy||S.resuming||j.revoked;
  if(S.workflow?.config&&!S.workflow.config.interview){q('#connect').disabled=true;q('#interviewSupport').textContent=t('notConfigured');}
  q('#uploadState').textContent=j.revoked?t('revokedText'):oversize?t('tooLarge'):missingOnly?t('lostTail'):c.uncertainTail||c.unsavedFinal?t('tailUnknown'):pending?`${t('waiting')} ${c.queue.items.length}. ${t('keepOpen')}`:j.chunk_count?t('allSaved'):t('noAudio');
  q('#uploadState').classList.toggle('unsaved',pending);
  const key=previous?(j.interview_call_state==='close_unknown'?'closeUnknown':'previousOpen'):j.revoked&&['manual_reconciliation','pending'].includes(j.cleanup_state)?'cleanupNote':j.revoked?'':!j.enabled?'disabled':j.resume_limit&&!live&&!connecting?'interviewLimit':!j.can_resume&&!live&&!connecting?'reviewNeeded':'';
  q('#serverState').hidden=!key;q('#serverState').textContent=key?t(key):'';
}
async function refresh(){
  const id=S.session;if(!id)return;const seq=++S.refreshSeq,j=await request(path('/journey'));if(id!==S.session||seq!==S.refreshSeq)return;
  S.journey=j;S.capture?.queue.syncNextSeq(j.next_seq);
  if(S.page!=='interview'){const workflow=await request('/api/enrollment/sessions/'+encodeURIComponent(id)+'/workflow');if(id!==S.session||seq!==S.refreshSeq)return;S.workflow=workflow;if(j.revoked){++S.workflowEpoch;S.busy=false;stopPreviewLocal();S.call=null;clearSamples();S.page='interview';renderInterview();return;}}
  if(j.revoked&&(S.capture?.stream||S.capture?.starting))S.capture.pause({save:false}).catch(report);
  if(q('#savedMinutes'))q('#savedMinutes').textContent=fmt(j.saved_audio_ms);if(q('#patterns'))q('#patterns').textContent=j.confirmed_patterns||0;update();
}
async function realtime(event,capture){
  if(capture!==S.capture||S.journey?.revoked)return;
  const base='/api/enrollment/sessions/'+encodeURIComponent(capture.sessionId);
  if(event.type==='raneen.connected')capture.send({type:'response.create',response:{instructions:(S.correction?'The contributor has just tested their private personalized AI agent and wants to teach a correction. Ask what it said, what they would say instead, and why. Preserve the existing approved voice. Confirm the corrected response pattern aloud and record confirmed evidence. ':'')+(S.journey.chunk_count?'Welcome the contributor back briefly and continue from their previously confirmed examples. Avoid repeating introductions or setup instructions. Ask one natural question at a time. Do not claim a finished clone.':'Introduce yourself briefly as Raneen’s private AI interviewer. Ask in Arabic which language and dialect the contributor prefers, respect their spoken choice, and ask one natural question at a time. Do not claim a finished clone.')}});
  else if(event.type==='conversation.item.input_audio_transcription.completed'){
    // Provider sideband owns durable transcription and confirmation.
    if(capture!==S.capture||S.journey?.revoked)return;
    capture.lastItem=event.item_id;S.transcript=event.transcript||'';if(q('#transcript'))q('#transcript').textContent=S.transcript;
  }else if(event.type==='error')notice(t('interviewerError'),true);
}
async function leave(){if(S.resuming){notice(t('saving'));return false;}if(S.busy){notice(t('preparingBackground'),true);return false;}if(S.call){notice(t('callAway'),true);return false;}stopSamples();stopMic();const capture=S.capture;if(capture)await capture.pause();if(capture!==S.capture)return false;if(capture?.hasPending){notice(t('keepOpen'),true);return false;}return true;}
async function revoke(){
  if(!confirm(t('revokeConfirm')))return;const id=S.session,capture=S.capture;++S.workflowEpoch;S.busy=true;stopMic();stopPreviewLocal();clearSamples();capture?.pause({save:false}).catch(report);q('#revoke').disabled=true;
  try{await post('/api/enrollment/sessions/'+encodeURIComponent(id)+'/revoke',{confirm:true});if(S.session!==id||S.capture!==capture)return;S.call=null;S.capture=null;S.busy=false;await openSession(id);notice(t('revokedText'));}catch(e){S.busy=false;if(S.session===id&&q('#revoke'))q('#revoke').disabled=false;report(e);}
}
async function signout(){if(!await leave())return;clearInterval(S.poll);S.token='';S.session=null;S.capture=null;S.journey=null;S.sessions=[];S.transcript='';S.workflow=null;S.correction=false;clearSamples();login();}
// Preparation only starts after an explicit click. Reloading reads server state;
// it never repeats an uncertain provider create operation.
function clearSamples(){stopSamples();for(const url of Object.values(S.samples))URL.revokeObjectURL(url);S.samples={};S.listened.clear();}
function stopSamples(){app.querySelectorAll('audio').forEach((audio)=>audio.pause());}
function configurationCard(config=S.workflow?.config){
  if(!config)return '';
  return `<details class="card connection-card" ${Object.values(config).some((v)=>!v)?'open':''}><summary>${t('setupStatus')}</summary><ul class="connection-list">${[['interview','interviewService'],['voice','voiceService'],['agent','agentService']].map(([key,label])=>`<li><span>${t(label)}</span><span class="pill ${config[key]?'':'neutral'}">${t(config[key]?'setupReady':'setupMissing')}</span></li>`).join('')}</ul>${Object.values(config).some((v)=>!v)?`<p class="small muted">${t('configurationNote')}</p>`:''}</details>`;
}
function workflowMessage(w){
  if(S.workflowError)return S.workflowError;
  if(w.voice_state==='verification_required')return t('verificationPending');
  if(w.stage==='blocked'||w.operations?.some((o)=>['outcome_unknown','dispatching'].includes(o.state)))return t('outcomePending');
  if(!S.journey.enabled)return t('processingPaused');
  if(w.config&&(!w.config.voice||(w.voice_approved&&!w.config.agent)))return t('configurationNote');
  return '';
}
async function fetchWorkflow(){const id=S.session;const w=await request('/api/enrollment/sessions/'+encodeURIComponent(id)+'/workflow');if(id!==S.session)return null;S.workflow=w;return w;}
function workflowFailure(e){
  const detail=String(e.detail||'').toLowerCase();
  if(e.message==='request_timeout')return t('operationTimeout');
  if(detail.includes('confirmed')||detail.includes('evidence'))return t('noExamples');
  if(detail.includes('speech')||detail.includes('audio')||detail.includes('sample')||detail.includes('decode'))return t('moreSpeech');
  if(detail.includes('verification'))return t('verificationPending');
  if(detail.includes('unknown')||detail.includes('dispatching')||detail.includes('reconcile'))return t('outcomePending');
  if(e.status===503)return t('notConfigured');
  return explain(e);
}
async function finishInterview(){
  if(S.busy||S.resuming)return;
  const id=S.session,epoch=S.workflowEpoch,capture=S.capture;
  S.resuming=true;update();
  try{
    if(!await settleInterview())return;
    if(S.capture?.hasPending){notice(t('saveFirst'),true);return;}
    if(S.journey.revoked)return;
    if(!S.journey.chunk_count){notice(t('noSpeechSaved'),true);return;}
    await fetchWorkflow();
    if(S.session!==id||S.capture!==capture||S.workflowEpoch!==epoch||S.journey.revoked||S.busy)return;
    S.workflowError='';S.page='prepare';renderPrepare();await prepareAgent();
  }finally{S.resuming=false;update();}
}
function renderPrepare(){
  S.page='prepare';const w=S.workflow||{},blocked=w.stage==='blocked'||w.voice_state==='verification_required'||w.operations?.some((o)=>['outcome_unknown','dispatching'].includes(o.state));
  const samplesReady=['question','number','correction'].every((kind)=>S.samples[kind]);
  const readyForApproval=samplesReady&&S.listened.size===3;
  const review=samplesReady&&!w.voice_approved;const status=workflowMessage(w);
  shell(hero(t(review?'voiceReview':'prepareTitle'),t(review?'voiceReviewText':'prepareIntro'))+`<section class="card preparation-card">${S.busy?`<div class="working-mark" aria-hidden="true">ر</div><h2 role="status">${t(S.phase||'prepareWorking')}</h2><p>${t('prepareWorkingNote')}</p>`:review?`<div class="sample-list">${[['question','questionSample'],['number','numberSample'],['correction','correctionSample']].map(([kind,label])=>`<div class="sample-card"><div><h3>${t(label)}</h3><span id="heard-${kind}" class="small muted">${t(S.listened.has(kind)?'listened':'listen')}</span></div><audio id="sample-${kind}" controls preload="metadata" src="${esc(S.samples[kind])}" aria-label="${t(label)}"></audio></div>`).join('')}</div><p class="small muted">${t('approvalNote')}</p><button id="approveVoice" class="primary" ${!readyForApproval?'disabled':''}>${t('approveVoice')}</button>`:`<div class="preparation-steps"><p><span>1</span>${t('checkingEvidence')}</p><p><span>2</span>${t('creatingVoice')}</p><p><span>3</span>${t('makingSamples')}</p></div><button id="prepareAgent" class="primary" ${blocked||!S.journey.enabled||!w.config?.voice||(w.voice_approved&&!w.config?.agent)?'disabled':''}>${t(S.correction?'updateAgent':w.voice_approved?'continueSetup':'prepareStart')}</button>`}${status?`<p class="status-note" role="status">${esc(status)}</p>`:''}</section>${configurationCard()}<div class="bottom-actions">${S.call?`<button id="stopTest" class="secondary">${t('stopTest')}</button>`:''}<button id="returnInterview" class="text-button" ${S.busy||S.call?'disabled':''}>${t('addSpeech')}</button><button id="refreshWorkflow" class="secondary" ${S.busy?'disabled':''}>${t('refreshProgress')}</button><button id="revoke" class="text-button danger">${t('revoke')}</button></div>`,true);
  for(const kind of ['question','number','correction']){const audio=q('#sample-'+kind);if(audio){audio.onplay=()=>app.querySelectorAll('audio').forEach((other)=>{if(other!==audio)other.pause();});audio.onended=()=>{S.listened.add(kind);q('#heard-'+kind).textContent=t('listened');q('#approveVoice').disabled=S.listened.size!==3;};}}
  bind('#prepareAgent',prepareAgent);bind('#approveVoice',approveVoice);bind('#stopTest',endPreview);
  bind('#refreshWorkflow',async()=>{S.workflowError='';await fetchWorkflow();if(S.workflow.stage==='agent_ready'&&!S.correction){S.page='agent';renderAgent();}else renderPrepare();});
  bind('#returnInterview',async()=>{stopSamples();await refresh();S.page='interview';renderInterview();});bind('#revoke',revoke);
}
async function prepareAgent(){
  if(S.busy)return;const id=S.session,base=path(),epoch=++S.workflowEpoch;const active=()=>S.session===id&&epoch===S.workflowEpoch&&!S.journey.revoked;S.busy=true;S.workflowError='';S.phase='checkingEvidence';renderPrepare();
  try{
    if(!S.workflow.voice_approved){
      S.phase='creatingVoice';renderPrepare();
      const voice=await post(base+'/clone',{approve:true,final_seq:S.journey.next_seq-1},{timeout:120000});if(!active())return;
      if(['verification_required','outcome_unknown','failed'].includes(voice.state)){await fetchWorkflow();return;}
      S.phase='makingSamples';renderPrepare();
      for(const kind of ['question','number','correction']){
        const blob=await post(base+'/preview',{approve:true,kind},{blob:true,timeout:90000});
        if(!active())return;if(!blob.size)throw Error('empty_preview');
        if(S.samples[kind])URL.revokeObjectURL(S.samples[kind]);S.samples[kind]=URL.createObjectURL(blob);
      }
      await fetchWorkflow();
    }else{
      const behavior=await post(base+'/behavior',{approve:true},{timeout:30000});if(!active())return;
      S.phase='buildingAgent';renderPrepare();await post(base+'/assistant',{approve:true,behavior_id:behavior.id},{timeout:90000});
      if(!active())return;await fetchWorkflow();S.correction=false;S.page='agent';
    }
  }catch(e){if(active()){S.workflowError=workflowFailure(e);try{await fetchWorkflow();}catch{}notice(S.workflowError,true);}}
  finally{if(epoch===S.workflowEpoch)S.busy=false;if(active()){S.phase='';render();}}
}
async function approveVoice(){
  if(S.busy||S.listened.size!==3)return;const id=S.session,base=path(),epoch=++S.workflowEpoch;const active=()=>S.session===id&&epoch===S.workflowEpoch&&!S.journey.revoked;stopSamples();S.busy=true;S.phase='buildingAgent';S.workflowError='';renderPrepare();
  try{
    await post(base+'/voice-approval',{approve:true});if(!active())return;const w=await fetchWorkflow();if(!active()||!w)return;
    const behavior=w.behavior_id?{id:w.behavior_id}:await post(base+'/behavior',{approve:true});
    if(!active())return;await post(base+'/assistant',{approve:true,behavior_id:behavior.id},{timeout:90000});
    if(!active())return;await fetchWorkflow();S.correction=false;S.page='agent';
  }catch(e){if(active()){S.workflowError=workflowFailure(e);try{await fetchWorkflow();}catch{}notice(S.workflowError,true);}}
  finally{if(epoch===S.workflowEpoch)S.busy=false;if(active()){S.phase='';render();}}
}
function renderAgent(){
  S.page='agent';const active=Boolean(S.call);
  shell(hero(t('agentTitle'),t('agentIntro'),t('privateTest'))+`<section class="card agent-card"><span class="pill">AI · ${t('owner')}</span><div class="orb ${active?'live':''}" aria-hidden="true">ر</div><h2 id="callStatus" role="status">${t(active?(S.call?.recovered?'pendingClose':'callConnecting'):'callReady')}</h2><p>${t('agentDisclosure')}</p><p class="small muted">${t('callLimit')}</p><div id="callMount"></div><div class="actions"><button id="startTest" class="primary" ${active||!S.journey.enabled||!S.workflow?.preview_allowed?'disabled':''}>${t('startTest')}</button><button id="stopTest" class="secondary" ${!active?'hidden':''}>${t('stopTest')}</button></div>${!S.journey.enabled?`<p class="status-note">${t('processingPaused')}</p>`:''}</section><section class="card correction-card"><h2>${t('correctionTitle')}</h2><p>${t('correctionNote')}</p><button id="teachCorrection" class="secondary" ${active?'disabled':''}>${t('teachCorrection')}</button></section>${configurationCard()}<div class="bottom-actions"><button id="backHome" class="text-button">${t('backHome')}</button><button id="revoke" class="text-button danger">${t('revoke')}</button></div>`,true);
  bind('#startTest',startPreview);bind('#stopTest',endPreview);bind('#teachCorrection',async()=>{if(S.call)return;S.correction=true;await refresh();S.page='interview';renderInterview();});bind('#backHome',async()=>{if(await leave())await home();});bind('#revoke',revoke);
}
async function startPreview(){
  if(S.call||S.busy)return;const id=S.session,base=path(),epoch=++S.workflowEpoch;const active=()=>S.session===id&&epoch===S.workflowEpoch&&!S.journey.revoked;S.busy=true;q('#startTest').disabled=true;q('#callStatus').textContent=t('callConnecting');
  try{
    const call=await post(base+'/preview-call',{approve:true},{timeout:45000});
    if(!active()){await post(base+'/preview-call/close').catch(()=>{});return;}
    const url=new URL(call.web_call_url);if(url.protocol!=='https:'||!url.hostname.endsWith('.daily.co'))throw Error('invalid_call_url');
    S.call={...call,sessionId:id};S.callNonce=crypto.randomUUID();S.callState='connecting';
    const frame=document.createElement('iframe');frame.id='agentFrame';frame.title=t('agentTitle');frame.src='/vapi-frame';frame.sandbox='allow-scripts allow-same-origin';frame.allow='microphone';frame.referrerPolicy='no-referrer';
    frame.onload=()=>{if(S.call?.sessionId===id)frame.contentWindow.postMessage({type:'raneen-call-start',nonce:S.callNonce,url:call.web_call_url,token:call.call_token||null,lang:S.lang},location.origin);};
    q('#callMount').appendChild(frame);q('#stopTest').hidden=false;q('#teachCorrection').disabled=true;
    S.callTimer=setTimeout(()=>endPreview().catch(report),(Math.min(call.max_duration_seconds||180,180)+5)*1000);
  }catch(e){if(active()){notice(workflowFailure(e),true);if(q('#callStatus'))q('#callStatus').textContent=t('callFailed');try{const result=await post(base+'/preview-call/close');if(result.state!=='closed'){S.call={sessionId:id,recovered:true};if(q('#stopTest'))q('#stopTest').hidden=false;}}catch{S.call={sessionId:id,recovered:true};if(q('#stopTest'))q('#stopTest').hidden=false;}if(q('#startTest'))q('#startTest').disabled=Boolean(S.call);}}
  finally{if(epoch===S.workflowEpoch)S.busy=false;}
}
function stopPreviewLocal(){clearTimeout(S.callTimer);q('#agentFrame')?.contentWindow?.postMessage({type:'raneen-call-stop',nonce:S.callNonce},location.origin);q('#agentFrame')?.remove();S.callState='idle';}
async function endPreview(){
  if(S.ending)return S.ending;
  const call=S.call;if(!call)return;stopPreviewLocal();if(q('#callStatus'))q('#callStatus').textContent=t('pendingClose');if(q('#stopTest'))q('#stopTest').disabled=true;
  S.ending=(async()=>{
    try{const result=await post('/api/enrollment/sessions/'+encodeURIComponent(call.sessionId)+'/preview-call/close');if(S.call!==call)return;if(result.state!=='closed')throw Error('close_unknown');S.call=null;await fetchWorkflow();if(S.page==='agent')renderAgent();if(S.page==='prepare')renderPrepare();notice(t('callClosed'));}
    catch(e){if(q('#stopTest'))q('#stopTest').hidden=false;notice(t('pendingClose'),true);throw e;}
    finally{S.ending=null;if(q('#stopTest'))q('#stopTest').disabled=false;}
  })();
  return S.ending;
}
window.addEventListener('message',(event)=>{
  const frame=q('#agentFrame');if(!frame||event.source!==frame.contentWindow||event.origin!==location.origin||event.data?.nonce!==S.callNonce)return;
  if(event.data.type==='raneen-call-connected'){S.callState='live';q('#callStatus').textContent=t('callLive');}
  else if(event.data.type==='raneen-call-ended'){endPreview().catch(report);}
  else if(event.data.type==='raneen-call-error'){notice(t('callFailed'),true);endPreview().catch(report);}
});

window.addEventListener('beforeunload',(e)=>{if(S.capture?.hasPending||S.capture?.stream||S.capture?.starting||S.call||S.busy){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',()=>{stopMic();stopPreviewLocal();stopSamples();S.capture?.stopLocal();});
window.addEventListener('offline',()=>{if(S.call)endPreview().catch(report);if(S.capture?.stream||S.capture?.starting)S.capture.pause().then(()=>notice(t('disconnected'),true)).catch(report);});
login();
