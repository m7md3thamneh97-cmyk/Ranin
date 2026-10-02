import {BecomingCapture} from './become-capture.js';
import {BecomingCall} from './become-call.js';
import {BecomingEventStream} from './become-events.js';

export const States=Object.freeze(Object.fromEntries(['IDLE','CONNECTING','CONVERSING_BOOTSTRAP','COLLECTING_VOICE','CLONE_ELIGIBLE','CLONING','CLONE_CREATED','CLONE_READY','SWITCHING_VOICE','CLONED_ACTIVE','CLONE_FAILED','CLONE_UNKNOWN','SYNTHESIS_FAILED','HANDOFF_FAILED','HANDOFF_UNKNOWN','CALL_FAILED','CALL_UNKNOWN','VERIFICATION_REQUIRED','ENDING','ENDED','REVOKED','FAILED'].map(value=>[value,value])));
const copy={
  ar:{brand:'رنين',eyebrow:'ممثّلك العقاري، بطريقتك',headline:'صوتك. أسلوبك.',intro:'اصنع ممثّلاً عقارياً بالذكاء الاصطناعي يتعلّم منك، بصوتك وأسلوب حديثك.',initial:'ابدأ محادثة. كن على طبيعتك.',detail:'كلّمه كما تكلّم عميلك. سيتعلّم صوتك، كلماتك، وطريقتك في الرد.',consent:'هذا صوتي. أوافق على تسجيله ومعالجته خارجياً واستنساخه لتجربة خاصة بالذكاء الاصطناعي.',create:'اصنع ممثّلي',privacy:'تبدأ بصوت مؤقّت، ثم يتحوّل إلى صوتك عند توفّر عيّنة مناسبة. يمكنك التوقّف وحذف الجلسة في أي وقت.',stop:'إنهاء المحادثة',revoke:'حذف الجلسة وصوتي',sound:'تشغيل الصوت',footer:'محادثة واحدة. يصبح أقرب إليك.',studio:'الاستوديو',liveHeadline:'خلّينا نتعرّف عليك.',liveIntro:'احكِ بطريقتك. علّمه كيف تتحدّث مع عملائك، وما يهمّك في شغلك.',connecting:'جاري توصيل المحادثة…',mic:'اسمح باستخدام الميكروفون لنبدأ.',listening:'رنين يستمع إليك',assistant:'رنين يتحدّث',waiting:'خذ وقتك. أنا معك.',collecting:'أتعلّم صوتك وأسلوبك',eligible:'توفّرت عيّنة أولية من صوتك',cloning:'جاري إنشاء صوتك. واصل الحديث.',ready:'صوتك جاهز للتجربة؛ نوصله بالمحادثة.',switching:'جاري توصيل صوتك بالمحادثة…',active:'رنين يتحدّث بصوتك',activeDetail:'هذا ممثّلك بالذكاء الاصطناعي. صحّح له بالكلام، وساعده يتعلّم أسلوبك.',progress:'{seconds} ثانية محفوظة من حديثك',ended:'انتهت المحادثة',endedDetail:'حُفظ ما تعلّمه من حديثك. يمكنك حذف الجلسة وصوتك من هنا.',endHeadline:'حديثك، بداية ممثّلك.',ending:'جاري إنهاء المحادثة…',revoked:'توقّف الاستخدام. طلب حذف الجلسة وصوتك قيد المعالجة.',revokedDetail:'توقّف الميكروفون والمحادثة. تتم متابعة حذف ملفات الصوت لدى الخدمات المستخدمة.',deleteConfirm:'هل تريد إيقاف المحادثة وحذف هذه الجلسة وصوتها المستنسخ؟',unavailable:'إنشاء الصوت غير متاح حالياً. حاول لاحقاً.',permission:'لم يُسمح باستخدام الميكروفون. فعّل إذن الميكروفون لهذا الموقع ثم حاول مجدداً.',unsupported:'تحتاج إلى متصفّح حديث يدعم الميكروفون، عبر HTTPS أو localhost.',connectError:'تعذّر توصيل المحادثة. يمكنك المحاولة مجدداً.',lost:'انقطع الاتصال. توقّف الميكروفون بأمان.',cloneError:'لم يكتمل إنشاء صوتك بعد. يمكنك متابعة المحادثة بالصوت المؤقّت.',switchError:'صوتك جاهز، لكن لم يُستخدم في المحادثة بعد. يمكنك متابعة الحديث.',verification:'يحتاج صوتك إلى تحقق إضافي لدى خدمة الصوت. يمكنك متابعة المحادثة.',saveError:'بعض حديثك لم يُحفظ بعد. المحادثة مستمرة، وجاري محاولة الحفظ.',saveStopped:'لم يُحفظ جزء من التسجيل. الجلسة لا تشمل هذا الجزء.',endError:'توقّف الميكروفون. تعذّر تأكيد إنهاء الاتصال لدى الخدمة؛ مدته محدودة تلقائياً.',deleteError:'توقّف الميكروفون. لم يُؤكّد طلب الحذف بعد؛ حاول مرة أخرى.',timeEnded:'انتهت المدة المسموحة لهذه المحادثة.',pollError:'تعذّر تحديث حالة صوتك مؤقتاً. المحادثة مستمرة.',tryAgain:'حاول مجدداً'},
  en:{brand:'Raneen',eyebrow:'Your real estate representative, your way',headline:'Your voice. Your way.',intro:'Create an AI real estate representative that learns from you, with your voice and way of speaking.',initial:'Start a conversation. Be yourself.',detail:'Talk as you would to a client. It learns your voice, your words, and how you respond.',consent:'This is my own voice. I consent to its recording, external processing, private cloning and AI preview.',create:'Create my agent',privacy:'We start with a temporary voice, then switch to yours when there is enough suitable speech. You can stop and delete the session at any time.',stop:'End conversation',revoke:'Delete my session and voice',sound:'Enable sound',footer:'One conversation. More like you.',studio:'Studio',liveHeadline:'Let’s get to know you.',liveIntro:'Speak naturally. Teach it how you talk to clients, and what matters in your work.',connecting:'Connecting your conversation…',mic:'Allow your microphone to begin.',listening:'Raneen is listening to you',assistant:'Raneen is speaking',waiting:'Take your time. I’m here.',collecting:'Learning your voice and speaking style',eligible:'An initial voice sample has been collected',cloning:'Creating your voice. Keep talking.',ready:'Your voice is ready to test; connecting it to your conversation.',switching:'Connecting your voice to this conversation…',active:'Raneen is speaking with your voice',activeDetail:'This is your AI representative. Correct it by speaking, and help it learn your approach.',progress:'{seconds} seconds of your speech saved',ended:'Conversation ended',endedDetail:'What it learned from your conversation has been saved. You can delete this session and voice here.',endHeadline:'Your conversation is a beginning.',ending:'Ending your conversation…',revoked:'Use has stopped. Your session and voice deletion is being processed.',revokedDetail:'Your microphone and conversation have stopped. Voice deletion is tracked with the services involved.',deleteConfirm:'Stop this conversation and delete this session and its cloned voice?',unavailable:'Voice creation is currently unavailable. Please try again later.',permission:'Microphone access was denied. Allow the microphone for this site, then try again.',unsupported:'Use a current browser with microphone support, on HTTPS or localhost.',connectError:'The conversation could not connect. You can try again.',lost:'The connection was lost. Your microphone has stopped safely.',cloneError:'Your voice could not be created yet. You can keep talking with the temporary voice.',switchError:'Your voice is ready, but it has not been used in the conversation yet. You can keep talking.',verification:'Your voice needs additional verification with the voice service. You can continue the conversation.',saveError:'Some speech has not been saved yet. Your conversation continues while we retry.',saveStopped:'Part of the recording could not be saved. It is not included in this session.',endError:'Your microphone has stopped. The service has not confirmed the call ended; its duration is automatically limited.',deleteError:'Your microphone has stopped. Deletion has not been confirmed yet; please try again.',timeEnded:'This conversation reached its time limit.',pollError:'Your voice status could not update temporarily. Your conversation continues.',tryAgain:'Try again'}
};

const $=id=>document.getElementById(id);
let language='ar',state=States.IDLE,readiness=null,session=null,capture=null,call=null,events=null,relayHealthy=false,server=null,epoch=0,pollTimer=null,durationTimer=null,pollRunning=false,observedVoice=null,firstClonedSpeechReported=false,speaking=null,noticeKey=null,lastFailureStage=null,lastFailureName=null,lastFailureCode=null;
const debugMode=new URLSearchParams(location.search).get('debug')==='1';
const safeErrorNames=new Set(['Error','TypeError','NotAllowedError','PermissionDeniedError','NotFoundError','NotReadableError','NotSupportedError','SecurityError','AbortError','InvalidStateError']);
const safeFrontendFailureCodes=new Set(['recording_unsupported','sample_rate_unsupported','capture_already_started','invalid_call_room','call_unavailable','invalid_session','event_stream_already_started','request_failed']);
const t=key=>copy[language][key]??key;
copy.ar.savedVoice='استمع إلى عيّنة جديدة بصوتك';copy.en.savedVoice='Listen to a fresh sample with your voice';
copy.ar.retention='يبقى صوتك وعيّنته متاحين في هذا المتصفّح لمدة {days} أيام، ثم يُجدول حذفهما تلقائياً. يمكنك طلب الحذف قبل ذلك.';
copy.en.retention='Your saved voice and sample stay available in this browser for {days} days; deletion is then scheduled automatically. You can delete them sooner.';
copy.ar.eventPaused='توقّف جمع صوتك مؤقتاً حتى يتأكد الاتصال. يمكنك متابعة المحادثة بالصوت المؤقّت.';
copy.en.eventPaused='Voice capture is paused until the connection is confirmed. You can continue with the temporary voice.';
const storageKey='raneen-becoming-session';
function rememberSession(id){try{if(id)localStorage.setItem(storageKey,id);else localStorage.removeItem(storageKey);}catch{}}
function isConfigured(){return readiness?.configured===true||(readiness?.configured&&typeof readiness.configured==='object'&&Object.keys(readiness.configured).length>0&&Object.values(readiness.configured).every(value=>value===true));}

function notice(key){noticeKey=key;$('notice').hidden=!key;$('notice').textContent=key?t(key):'';}
function textStatus(){
  if(state===States.IDLE)return 'initial';
  if(state===States.CONNECTING)return 'connecting';
  if(state===States.ENDING)return 'ending';
  if(state===States.ENDED||state===States.FAILED)return 'ended';
  if(state===States.REVOKED)return 'revoked';
  if(speaking==='user')return 'listening';
  if(speaking==='assistant')return state===States.CLONED_ACTIVE?'active':'assistant';
  return ({COLLECTING_VOICE:'collecting',CLONE_ELIGIBLE:'eligible',CLONING:'cloning',CLONE_CREATED:'cloning',CLONE_READY:'ready',SWITCHING_VOICE:'switching',CLONED_ACTIVE:'active'})[state]??'waiting';
}
function render(){
  document.documentElement.lang=language;document.documentElement.dir=language==='ar'?'rtl':'ltr';document.title=language==='ar'?'رنين · صوتك، أسلوبك':'Raneen · Your voice, your way';
  const configuredDays=Number(readiness?.retention_days),retentionDays=Number.isInteger(configuredDays)&&configuredDays>0?configuredDays:7;
  document.querySelectorAll('[data-copy]').forEach(element=>{element.textContent=t(element.dataset.copy).replace('{days}',String(retentionDays));});
  $('language').textContent=language==='ar'?'English':'العربية';$('language').lang=language==='ar'?'en':'ar';
  $('experience').dataset.state=state;$('experience').dataset.speaking=speaking??'';$('experience').setAttribute('aria-busy',state===States.CONNECTING?'true':'false');
  const idle=state===States.IDLE||(state===States.FAILED&&!session),ended=[States.ENDED,States.REVOKED].includes(state)||(state===States.FAILED&&session);
  $('startControls').hidden=!idle;$('liveControls').hidden=idle;
  $('create').disabled=!$('consent').checked||!readiness?.enabled||!isConfigured();
  if(state===States.FAILED)$('create').querySelector('[data-copy]').textContent=t('tryAgain');
  $('stop').hidden=ended;$('stop').disabled=state===States.ENDING;
  $('revoke').hidden=state===States.REVOKED||!session;$('revoke').disabled=state===States.ENDING;
  const savedVoice=state===States.ENDED&&server?.voice_ready===true&&session;
  $('savedVoice').hidden=!savedVoice;
  if(savedVoice&&!$('voicePreview').getAttribute('src'))$('voicePreview').src='/api/becoming/sessions/'+session.id+'/voice-check';
  if(!savedVoice){$('voicePreview').pause();$('voicePreview').removeAttribute('src');}
  $('headline').textContent=idle?t('headline'):ended?t('endHeadline'):t('liveHeadline');
  $('intro').textContent=idle?t('intro'):ended?'':t('liveIntro');
  $('status').textContent=t(textStatus());
  $('detail').textContent=state===States.REVOKED?t('revokedDetail'):state===States.ENDED?t('endedDetail'):state===States.CLONED_ACTIVE?t('activeDetail'):state===States.CONNECTING?t('mic'):idle?t('detail'):t('detail');
  const saved=Math.floor(Number(server?.eligible_audio_seconds)||0);
  $('progress').textContent=saved>0?t('progress').replace('{seconds}',String(saved)):'';
  $('diagnostics').hidden=!debugMode;
  if(debugMode)$('debug').textContent=JSON.stringify({state,server_state:server?.state??null,last_failure_stage:lastFailureStage,last_failure_name:lastFailureName,last_failure_code:lastFailureCode,saved_speech_seconds:Number(server?.eligible_audio_seconds)||0,minimum_speech_seconds:server?.minimum_speech_seconds??readiness?.minimum_speech_seconds??null,capture_eligible_seconds:Math.round((capture?.durationSeconds||0)*100)/100,pending_chunks:capture?.queue.items.length||0,provider_relay_confirmed:relayHealthy,provider_observed_voice:observedVoice?{provider:observedVoice.provider,voice_id:observedVoice.voice_id}:null,telemetry:server?.telemetry??null,failure:server?.failure?.code??null},null,2);
  if(noticeKey)notice(noticeKey);
}
function setState(value){if(Object.values(States).includes(value))state=value;render();}
async function api(path,{method='GET',body,headers={},signal,keepalive=false,timeoutMs=30000}={}){
  const controller=signal?null:new AbortController();let timer;
  if(controller&&!keepalive)timer=setTimeout(()=>controller.abort(),timeoutMs);
  let response,data={};
  try{response=await fetch('/api/becoming'+path,{method,credentials:'same-origin',headers:{...(session?.capability?{Authorization:'Bearer '+session.capability}:{}),...(body!==undefined&&!(body instanceof Blob)?{'Content-Type':'application/json'}:{}),...headers},...(body!==undefined?{body:body instanceof Blob?body:JSON.stringify(body)}:{}),signal:signal??controller?.signal,keepalive});try{data=await response.json();}catch(error){if(error.name==='AbortError')throw error;}}finally{clearTimeout(timer);}
  if(!response.ok){const error=new Error('request_failed');error.status=response.status;error.code=data.detail?.code??data.code;error.retryable=response.status===429||response.status>=500;throw error;}
  return data;
}
function providerEvent(message,at){
  if(!relayHealthy||(session?.call_id&&message.call_id!==session.call_id))return;
  capture?.event(message,at);
  if(message.type==='speech-update'){
    if(message.status==='started')speaking=message.role;
    else if(speaking===message.role)speaking=null;
  }
  if(message.type==='assistant.started'){observedVoice=message.new_assistant_voice??null;firstClonedSpeechReported=false;}
  render();
}
function captureError(error){if([States.ENDED,States.REVOKED,States.FAILED].includes(state))return;notice('saveError');render();}
async function poll(){
  if(pollRunning||!session||[States.REVOKED,States.FAILED].includes(state))return;
  const current=session,currentEpoch=epoch;pollRunning=true;
  try{
    const value=await api('/sessions/'+current.id);
    if(session!==current||epoch!==currentEpoch)return;
    server=value;
    if(value.state==='ENDED'){if(state!==States.ENDED)await end({remote:true});return;}
    if(value.state==='REVOKED'){if(state!==States.REVOKED)await end({revoke:true,remote:true});return;}
    if(['CLONING','CLONE_CREATED','CLONE_READY','SWITCHING_VOICE','CLONED_ACTIVE'].includes(value.state))capture?.setCollecting(false);
    else if(relayHealthy&&['COLLECTING_VOICE','CONVERSING_BOOTSTRAP'].includes(value.state)&&!value.voice_ready&&!value.voice_id)capture?.setCollecting(true);
    if(![States.ENDING,States.ENDED].includes(state))setState(value.state);
    if(['CLONE_FAILED','CLONE_UNKNOWN','SYNTHESIS_FAILED'].includes(value.state)||['clone','synthesis'].includes(value.failure?.stage))notice('cloneError');
    else if(['HANDOFF_FAILED','HANDOFF_UNKNOWN'].includes(value.state)||value.failure?.stage==='handoff')notice('switchError');
    else if(value.state==='VERIFICATION_REQUIRED')notice('verification');
    else if(state===States.CLONED_ACTIVE&&['cloneError','switchError','pollError'].includes(noticeKey))notice(null);
    render();
  }catch{if(epoch===currentEpoch&&!noticeKey)notice('pollError');}
  finally{pollRunning=false;}
}
async function create(){
  if(state!==States.IDLE&&state!==States.FAILED)return;
  if(!$('consent').checked||!readiness?.enabled||!isConfigured())return;
  const currentEpoch=++epoch;notice(null);speaking=null;observedVoice=null;firstClonedSpeechReported=false;server=null;setState(States.CONNECTING);
  let stage='microphone';lastFailureStage=null;lastFailureName=null;lastFailureCode=null;
  try{
    // Request the microphone first so a denied permission cannot start a paid
    // call or leave a live room running in the background.
    capture=new BecomingCapture({upload:async(item,signal)=>{const response=await api('/sessions/'+session.id+'/chunks/'+item.seq,{method:'PUT',body:item.blob,signal,headers:{'Content-Type':'audio/wav','X-Chunk-SHA256':item.checksum,'X-Capture-Settings':JSON.stringify(item.settings)}});return {...response,seq:response.seq??response.sequence};},onError:captureError,onAck:()=>poll(),onChange:()=>{if(debugMode)render();}});
    capture.suspendGate();relayHealthy=false;
    await capture.start();
    if(epoch!==currentEpoch){await capture.stop({save:false});return;}
    stage='session';
    const created=await api('/sessions',{method:'POST',body:{consent_version:'becoming-v1',own_voice:true,recording:true,external_processing:true,voice_cloning:true,private_preview:true,language}});
    session=created;
    rememberSession(created.id);
    if(epoch!==currentEpoch){await api('/sessions/'+session.id+'/end',{method:'POST',body:{}});return;}
    capture.queue.sessionId=created.id;
    events=new BecomingEventStream({onEvent:(message,at)=>{if(epoch===currentEpoch)providerEvent(message,at);},onOpen:()=>{if(epoch!==currentEpoch)return;relayHealthy=true;if(!['CLONING','CLONE_CREATED','CLONE_READY','SWITCHING_VOICE','CLONED_ACTIVE'].includes(state))capture?.setCollecting(true);if(noticeKey==='eventPaused')notice(null);},onError:()=>{if(epoch!==currentEpoch)return;relayHealthy=false;capture?.suspendGate();speaking=null;if(!['CLONE_READY','SWITCHING_VOICE','CLONED_ACTIVE'].includes(state))notice('eventPaused');render();},onClosed:terminal=>{if(epoch!==currentEpoch)return;relayHealthy=false;capture?.suspendGate();if(terminal==='REVOKED')end({revoke:true,remote:true});else end({remote:true});}});
    events.start(created.id);
    stage='call';
    const room=await api('/sessions/'+session.id+'/call',{method:'POST',body:{}});
    session.call_id=room.call_id;
    if(epoch!==currentEpoch){await api('/sessions/'+session.id+'/end',{method:'POST',body:{}});return;}
    call=new BecomingCall({onPlayback:active=>{if(epoch===currentEpoch)capture?.playback(active);},onConnected:()=>{if(epoch!==currentEpoch)return;setState(States.CONVERSING_BOOTSTRAP);poll();},onEnded:()=>end(),onError:()=>{notice('lost');end();},onSoundBlocked:blocked=>{$('sound').hidden=!blocked;}});
    stage='join';
    await call.start({url:room.web_call_url,token:room.call_token,microphoneTrack:capture.stream.getAudioTracks()[0]});
    if(epoch!==currentEpoch)return;
    pollTimer=setInterval(poll,1500);
    durationTimer=setTimeout(()=>{end().then(()=>notice('timeEnded'));},Math.max(1,Number(room.max_duration_seconds)||Number(readiness.max_duration_seconds)||1800)*1000);
    render();
  }catch(error){
    if(epoch!==currentEpoch)return;
    lastFailureStage=stage;lastFailureName=safeErrorNames.has(error.name)?error.name:'UnknownError';lastFailureCode=safeFrontendFailureCodes.has(error.message)?error.message:null;
    clearInterval(pollTimer);clearTimeout(durationTimer);
    events?.stop();relayHealthy=false;
    await capture?.stop({save:false});await call?.stop();
    if(session)await api('/sessions/'+session.id+'/end',{method:'POST',body:{}}).catch(()=>{});
    setState(States.FAILED);
    notice(['NotAllowedError','PermissionDeniedError'].includes(error.name)?'permission':['NotFoundError','NotReadableError'].includes(error.name)||(stage==='microphone'&&error.name==='NotSupportedError')||['recording_unsupported','sample_rate_unsupported'].includes(error.message)?'unsupported':'connectError');
  }
}
async function end({revoke=false,remote=false}={}){
  if(state===States.ENDING||state===States.REVOKED)return;
  ++epoch;clearInterval(pollTimer);clearTimeout(durationTimer);speaking=null;setState(States.ENDING);
  events?.stop();relayHealthy=false;
  // Capture.stop releases local tracks synchronously before its first await.
  const saving=capture?.stop({save:!revoke});const leaving=call?.stop();
  $('sound').hidden=true;
  let saved=true;
  try{saved=(await saving)??true;}catch{saved=false;}
  await leaving;
  if(session&&!remote){try{const result=await api('/sessions/'+session.id+(revoke?'/revoke':'/end'),{method:'POST',body:revoke?{confirm:true}:{}});server={...server,...result};}catch{setState(States.ENDED);notice(revoke?'deleteError':'endError');return;}}
  setState(revoke?States.REVOKED:States.ENDED);
  if(revoke)rememberSession(null);
  if(!saved)notice('saveStopped');else if(revoke)notice(null);
}
async function init(){
  $('language').onclick=()=>{language=language==='ar'?'en':'ar';render();};
  $('consent').onchange=render;$('create').onclick=create;$('stop').onclick=()=>end();
  $('sound').onclick=()=>call?.enableSound();
  $('revoke').onclick=()=>{if(window.confirm(t('deleteConfirm')))end({revoke:true});};
  window.addEventListener('pagehide',()=>{
    ++epoch;clearInterval(pollTimer);clearTimeout(durationTimer);capture?.queue.suspend();capture?.release();call?.stop();
    events?.stop();relayHealthy=false;
    $('voicePreview').pause();$('voicePreview').removeAttribute('src');
    if(session&&!['ENDED','REVOKED'].includes(state))api('/sessions/'+session.id+'/end',{method:'POST',body:{},keepalive:true}).catch(()=>{});
  });
  render();
  try{readiness=await api('/readiness');if(!readiness.enabled||!isConfigured())notice('unavailable');}
  catch{notice('unavailable');}
  let previousId;try{previousId=localStorage.getItem(storageKey);}catch{}
  if(typeof previousId==='string'&&/^[0-9a-f]{32}$/.test(previousId)){
    session={id:previousId};
    try{
      server=await api('/sessions/'+previousId);
      if(server.state==='REVOKED'){setState(States.REVOKED);rememberSession(null);}
      else{
        // Refresh restores a saved session through its HttpOnly cookie. It
        // never starts another call, requests the microphone, or clones again.
        if(server.state!=='ENDED'){
          try{const result=await api('/sessions/'+previousId+'/end',{method:'POST',body:{}});server={...server,...result};}
          catch{notice('endError');}
        }
        setState(States.ENDED);
      }
    }catch(error){
      if([401,403,404,410].includes(error.status)){session=null;server=null;rememberSession(null);}
      else{setState(States.ENDED);notice('pollError');}
    }
  }
  render();
}
if(typeof document!=='undefined')init();
