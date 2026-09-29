const q=(s)=>document.querySelector(s);
const app=q("#app");
const notice=q("#notice");
const S={token:sessionStorage.getItem("raneen-token")||"",user:null,status:null,session:null,pc:null,dc:null,stream:null,recorder:null,segmentActive:false,segmentStart:0,timer:null,seq:0,uploads:new Set(),lastItem:null,transcript:""};

function esc(v){return String(v??"").replace(/[&<>"']/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function flash(m){notice.textContent=m;notice.classList.add("show");clearTimeout(flash.t);flash.t=setTimeout(()=>notice.classList.remove("show"),6500);}
function shell(h){app.innerHTML='<div class="shell"><div class="brand">RANEEN <span>VOICE ENROLLMENT</span></div>'+h+"</div>";}
async function api(path,body,method){
  const headers={Authorization:"Bearer "+S.token};
  let payload;
  if(body!==undefined){headers["Content-Type"]="application/json";payload=JSON.stringify(body);}
  const r=await fetch(path,{method:method||(body===undefined?"GET":"POST"),headers,body:payload,credentials:"omit"});
  if(!r.ok){let d="";try{d=(await r.json()).detail||"";}catch{}throw Error(d||("Request failed ("+r.status+")."));}
  return r.json();
}
function login(error){
  app.innerHTML='<section class="login"><div class="brand">RANEEN <span>VOICE ENROLLMENT</span></div><div class="hero"><h1>Teach by talking.</h1><p>No transcript editing or training forms. This owner-only build learns from a spoken interview.</p></div><div class="card">'+(error?'<p class="error">'+esc(error)+"</p>":"")+'<label for="token">Private owner token</label><input id="token" type="password" autocomplete="off"><button class="primary" id="login">Open enrollment</button></div></section>';
  q("#login").onclick=()=>{S.token=q("#token").value.trim();boot();};
}
async function boot(){
  try{
    S.user=await api("/api/me");
    if(S.user.role!=="admin")throw Error("Gate A is owner-only.");
    sessionStorage.setItem("raneen-token",S.token);
    S.status=await api("/api/enrollment/status");
    if(!S.status.enabled)return disabled();
    await startScreen();
  }catch(e){login(e.message);}
}
function disabled(){
  shell('<div class="hero"><h1>Voice enrollment is installed but off.</h1><p>This release is fail-closed. An operator must review and enable <code>RANEEN_VOICE_ENROLLMENT_ENABLED=1</code> before any provider call can occur.</p></div><div class="card"><div class="status"><div><span class="small muted">OpenAI</span><strong>'+(S.status.openai_configured?"set":"missing")+'</strong></div><div><span class="small muted">ElevenLabs</span><strong>'+(S.status.elevenlabs_configured?"set":"missing")+'</strong></div><div><span class="small muted">Vapi</span><strong>'+((S.status.vapi_private_configured&&S.status.vapi_public_configured)?"set":"missing")+'</strong></div></div><a href="/">Back to Teaching Studio</a></div>');
}
async function startScreen(){
  const c=await api("/api/enrollment/consent");
  shell('<div class="hero"><h1>Talk for 20–30 minutes.<br>Meet the agent it creates.</h1><p>The interviewer learns how you answer, what changes your decisions, and how you naturally phrase things. Your microphone is captured separately for a private voice clone.</p></div><div class="card"><h2>Before we start</h2><p class="small muted">'+esc(c.text)+'</p><label class="check"><input id="own" type="checkbox"><span>This is my own voice and I am choosing to enroll it.</span></label><label class="check"><input id="record" type="checkbox"><span>Record and store my microphone track for this private test.</span></label><label class="check"><input id="external" type="checkbox"><span>Use configured AI providers to interview, transcribe, clone, synthesize, and preview.</span></label><label class="check"><input id="clone" type="checkbox"><span>Create one private synthetic clone of my voice for this test.</span></label><label class="check"><input id="preview" type="checkbox"><span>Create a private AI preview agent using the clone. It must identify itself as AI.</span></label><p class="small warning">Use headphones. Speaker playback leaking into the microphone can contaminate voice samples.</p><button id="start" class="primary">Start voice enrollment</button></div><p class="small muted">No customer calls, phone numbers, CRM actions, or production Sura changes are part of this flow.</p>');
  q("#start").onclick=async()=>{
    try{
      const r=await api("/api/enrollment/sessions",{self_attestation:q("#own").checked,recording:q("#record").checked,external_processing:q("#external").checked,voice_cloning:q("#clone").checked,private_preview:q("#preview").checked});
      S.session=r.id;
      await interviewScreen();
    }catch(e){flash(e.message);}
  };
}
function fmt(ms){const s=Math.floor(ms/1000);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
async function state(){return api("/api/enrollment/sessions/"+S.session);}
async function interviewScreen(){
  const st=await state();
  S.seq=st.chunks.length?Math.max(...st.chunks.map((x)=>x.seq))+1:0;
  const confirmed=st.evidence.filter((x)=>x.status==="confirmed").length;
  const pct=Math.min(100,st.clean_ms/(20*60*1000)*100);
  shell('<div class="hero"><h1>Have the conversation.</h1><p>The interviewer adapts as you speak. Answer naturally. It will sometimes ask whether it understood a pattern correctly.</p></div><div class="card"><div id="orb" class="orb '+(S.pc?"live":"paused")+'">'+(S.pc?"LIVE":"READY")+'</div><div class="status"><div><span class="small muted">Mic captured</span><strong id="captured">'+fmt(st.clean_ms)+'</strong></div><div><span class="small muted">Confirmed patterns</span><strong>'+confirmed+'</strong></div><div><span class="small muted">Voice</span><strong>'+esc(st.voice_state)+'</strong></div></div><div class="progress"><span id="progress" style="width:'+pct+'%"></span></div><p class="small muted">20 minutes is the target interview length. Captured microphone time is not a quality score.</p><div class="row"><button id="connect" class="primary">'+(S.pc?"Interview running":"Start / resume interview")+'</button><button id="pause" class="secondary" '+(S.pc?"":"disabled")+'>Pause</button><button id="refresh" class="secondary">Refresh progress</button></div><h3>Latest words heard</h3><div id="transcript" class="transcript" dir="auto">'+esc(S.transcript||"Nothing transcribed yet.")+'</div></div><div class="card"><h2>Create the voice when enough microphone audio is available</h2><p class="small muted">The clone uses only your microphone chunks. Raneen then synthesizes new Arabic speech that was not recorded during enrollment.</p><button id="cloneBtn" class="primary">Create / check my private voice clone</button><div id="voiceResult"></div></div><div class="row"><button id="revoke" class="secondary danger">Revoke this enrollment</button><a href="/">Back to teaching studio</a></div>');
  q("#connect").onclick=()=>connectInterview().catch((e)=>flash(e.message));
  q("#pause").onclick=()=>pauseInterview().catch((e)=>flash(e.message));
  q("#refresh").onclick=()=>interviewScreen().catch((e)=>flash(e.message));
  q("#cloneBtn").onclick=()=>cloneVoice().catch((e)=>flash(e.message));
  q("#revoke").onclick=async()=>{
    if(!confirm("Revoke this enrollment and immediately block local use?"))return;
    await pauseInterview();
    const r=await api("/api/enrollment/sessions/"+S.session+"/revoke",{confirm:true});
    flash("Revoked. Provider cleanup: "+r.provider_cleanup+".");
    S.session=null;
    await startScreen();
  };
}
async function connectInterview(){
  if(S.pc)return;
  if(!navigator.mediaDevices?.getUserMedia||!window.RTCPeerConnection||!window.MediaRecorder)throw Error("Use a current HTTPS browser with microphone and WebRTC support.");
  const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
  S.stream=stream;
  startSegments();
  const pc=new RTCPeerConnection();
  S.pc=pc;
  const remote=document.createElement("audio");
  remote.autoplay=true;
  pc.ontrack=(e)=>{remote.srcObject=e.streams[0];};
  stream.getAudioTracks().forEach((track)=>pc.addTrack(track,stream));
  const dc=pc.createDataChannel("oai-events");
  S.dc=dc;
  dc.onopen=()=>{
    flash("Interview connected. Speak naturally.");
    dc.send(JSON.stringify({type:"response.create",response:{instructions:"Greet the contributor briefly, say this is a private AI enrollment interview, then ask one natural conversational question."}}));
  };
  dc.onmessage=(event)=>{let evt;try{evt=JSON.parse(event.data);}catch{return;}handleRealtime(evt).catch((e)=>flash(e.message));};
  dc.onerror=()=>flash("Realtime data channel error.");
  pc.onconnectionstatechange=()=>{if(["failed","closed","disconnected"].includes(pc.connectionState)&&S.pc===pc)flash("Interview connection ended. Saved microphone chunks remain on the server.");};
  const offer=await pc.createOffer();
  await pc.setLocalDescription(offer);
  const r=await fetch("/api/enrollment/sessions/"+S.session+"/webrtc",{method:"POST",credentials:"omit",body:offer.sdp,headers:{Authorization:"Bearer "+S.token,"Content-Type":"application/sdp"}});
  if(!r.ok){let d="";try{d=(await r.json()).detail||"";}catch{}await stopLocalMedia();pc.close();S.pc=null;throw Error(d||("Realtime WebRTC connection failed ("+r.status+")."));}
  await pc.setRemoteDescription({type:"answer",sdp:await r.text()});
  await interviewScreen();
}
async function handleRealtime(evt){
  if(evt.type==="conversation.item.input_audio_transcription.completed"){
    S.lastItem=evt.item_id;
    S.transcript=evt.transcript||"";
    await api("/api/enrollment/sessions/"+S.session+"/transcripts",{item_id:evt.item_id,transcript:S.transcript});
    if(q("#transcript"))q("#transcript").textContent=S.transcript;
    return;
  }
  if(evt.type==="response.done"&&Array.isArray(evt.response?.output)){
    for(const item of evt.response.output){
      if(item.type!=="function_call")continue;
      let args={};try{args=JSON.parse(item.arguments||"{}");}catch{continue;}
      const result=await api("/api/enrollment/sessions/"+S.session+"/tool",{call_id:item.call_id,name:item.name,arguments:args,source_item_id:S.lastItem});
      if(S.dc?.readyState==="open"){
        S.dc.send(JSON.stringify({type:"conversation.item.create",item:{type:"function_call_output",call_id:item.call_id,output:JSON.stringify(result)}}));
        S.dc.send(JSON.stringify({type:"response.create"}));
      }
    }
  }
  if(evt.type==="error")flash("Interviewer reported an error. Pause and retry if it persists.");
}
function mimeChoice(){for(const m of ["audio/webm;codecs=opus","audio/webm","audio/ogg;codecs=opus","audio/ogg"])if(MediaRecorder.isTypeSupported(m))return m;throw Error("This browser does not expose a supported microphone recording format.");}
function startSegments(){
  S.segmentActive=true;
  const run=()=>{
    if(!S.segmentActive||!S.stream?.active)return;
    if(S.uploads.size>=4){S.timer=setTimeout(run,700);return;}
    const mime=mimeChoice(),parts=[];
    const rec=new MediaRecorder(S.stream,{mimeType:mime,audioBitsPerSecond:64000});
    let resolveDone;
    S.segmentDone=new Promise((resolve)=>{resolveDone=resolve;});
    S.recorder=rec;S.segmentStart=performance.now();
    rec.ondataavailable=(e)=>{if(e.data?.size)parts.push(e.data);};
    rec.onstop=async()=>{
      const duration=Math.max(250,Math.round(performance.now()-S.segmentStart));
      const blob=new Blob(parts,{type:mime});
      if(S.segmentActive)run();
      try{
        if(!blob.size)return;
        const seq=S.seq++;
        const p=uploadChunk(seq,blob,duration).catch((e)=>{flash(e.message);S.segmentActive=false;throw e;}).finally(()=>S.uploads.delete(p));
        S.uploads.add(p);
        await p;
      }finally{resolveDone();}
    };
    rec.start();
    S.timer=setTimeout(()=>{if(rec.state==="recording")rec.stop();},3000);
  };
  run();
}
async function hash(blob){const h=await crypto.subtle.digest("SHA-256",await blob.arrayBuffer());return [...new Uint8Array(h)].map((x)=>x.toString(16).padStart(2,"0")).join("");}
async function uploadChunk(seq,blob,duration){
  if(blob.size>80*1024)throw Error("A microphone chunk exceeded the safe upload size; pause and retry.");
  const r=await fetch("/api/enrollment/sessions/"+S.session+"/chunks/"+seq,{method:"PUT",credentials:"omit",body:blob,headers:{Authorization:"Bearer "+S.token,"Content-Type":blob.type||"audio/webm","X-Speaker-Role":"contributor","X-Chunk-Sha256":await hash(blob),"X-Duration-Ms":String(duration)}});
  if(!r.ok){let d="";try{d=(await r.json()).detail||"";}catch{}throw Error(d||("Audio chunk "+seq+" failed ("+r.status+")."));}
  const st=await state();
  if(q("#captured"))q("#captured").textContent=fmt(st.clean_ms);
  if(q("#progress"))q("#progress").style.width=Math.min(100,st.clean_ms/(20*60*1000)*100)+"%";
}
async function stopLocalMedia(){
  S.segmentActive=false;clearTimeout(S.timer);
  const finalSegment=S.segmentDone;
  if(S.recorder?.state==="recording")S.recorder.stop();
  try{
    await Promise.race([
      finalSegment,
      new Promise((_,reject)=>setTimeout(()=>reject(Error("Final microphone chunk did not close cleanly.")),5000))
    ]);
  }finally{
    S.stream?.getTracks().forEach((t)=>t.stop());
    S.stream=null;
  }
}
async function pauseInterview(){
  try{await stopLocalMedia();}catch(e){flash(e.message);}
  if(S.session&&S.token){
    try{await api("/api/enrollment/sessions/"+S.session+"/webrtc-close",{});}catch(e){flash("Server could not confirm interview hangup; the hard timeout remains active.");}
  }
  if(S.dc?.readyState==="open")S.dc.close();
  S.pc?.close();S.pc=null;S.dc=null;
  await Promise.allSettled([...S.uploads]);S.uploads.clear();
  await interviewScreen();
}
async function cloneVoice(){
  const result=await api("/api/enrollment/sessions/"+S.session+"/clone",{approve:true});
  const target=q("#voiceResult");
  if(result.state==="verification_required"){target.innerHTML='<p class="warning">ElevenLabs requires speaker verification. Raneen will not bypass it.</p>';return;}
  if(result.state==="outcome_unknown"){target.innerHTML='<p class="warning">Provider outcome is uncertain. Automatic retry is blocked to avoid duplicates.</p>';return;}
  if(result.state!=="ready")throw Error("Voice clone is not ready.");
  target.innerHTML='<p class="ready">Voice clone created. Generating new speech…</p>';
  const clips=[];
  for(const kind of ["question","number","correction"]){
    const r=await fetch("/api/enrollment/sessions/"+S.session+"/preview",{method:"POST",credentials:"omit",headers:{Authorization:"Bearer "+S.token,"Content-Type":"application/json"},body:JSON.stringify({approve:true,kind})});
    if(!r.ok){let d="";try{d=(await r.json()).detail||"";}catch{}throw Error(d||("Preview "+kind+" failed."));}
    clips.push({kind,url:URL.createObjectURL(await r.blob())});
  }
  target.innerHTML='<p class="ready">Fresh synthesized Arabic speech:</p><div class="grid">'+clips.map((x)=>'<div class="audio-card"><strong>'+esc(x.kind)+'</strong><audio controls src="'+x.url+'"></audio></div>').join("")+'</div><div class="row" style="margin-top:16px"><button id="buildAgent" class="primary">Build my personalized test agent</button><button id="continueTeaching" class="secondary">Continue teaching</button></div>';
  q("#buildAgent").onclick=()=>buildAgent().catch((e)=>flash(e.message));
  q("#continueTeaching").onclick=()=>interviewScreen().catch((e)=>flash(e.message));
}
async function buildAgent(){
  const behavior=await api("/api/enrollment/sessions/"+S.session+"/behavior",{approve:true});
  const assistant=await api("/api/enrollment/sessions/"+S.session+"/assistant",{approve:true,behavior_id:behavior.id});
  const cfg=await api("/api/enrollment/sessions/"+S.session+"/preview-config");
  sessionStorage.removeItem("raneen-token");
  S.token="";
  shell('<div class="hero"><h1>Meet the test agent.</h1><p>This is AI using the private cloned voice and behavior version '+behavior.version+'. It is not the human speaker and has no production tools or phone number.</p></div><div class="card"><h2>Talk to the agent</h2><p class="small muted">For security, your owner token has been cleared before loading the third-party voice widget. Returning to teaching requires signing in again.</p><iframe id="previewFrame" title="Raneen private Vapi preview" sandbox="allow-scripts allow-same-origin allow-forms" allow="microphone" style="width:100%;height:430px;border:0;border-radius:14px"></iframe></div><div class="card"><h2>Want to change how it responds?</h2><p>End the preview, return to enrollment, sign in, and teach the correction by voice. The next behavior version reuses the same voice clone.</p><a class="primary" href="/enroll">Return to voice enrollment</a></div>');
  const frame=q("#previewFrame");
  frame.src="/vapi-frame#"+encodeURIComponent(JSON.stringify({assistant_id:assistant.assistant_id,public_key:cfg.public_key,max_duration_seconds:cfg.max_duration_seconds}));
}
window.addEventListener("beforeunload",(e)=>{if(S.pc||S.uploads.size){e.preventDefault();e.returnValue="";}});
if(S.token)boot();else login();
