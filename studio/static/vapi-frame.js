/* Join only the server-created, duration-bounded Daily room. This same-origin
 * trusted application frame receives no owner credential or Vapi API key. */
(()=>{
  let call=null,nonce='',starting=false,stopping=false,lang='ar';
  const audios=new Map(),localTracks=new Set(),status=document.querySelector('#status'),sound=document.querySelector('#enableSound'),end=document.querySelector('#endCall');
  const text={ar:{connecting:'جاري توصيل المحادثة…',live:'المحادثة شغّالة. احكِ بطبيعتك.',ended:'انتهت المحادثة.',failed:'تعذّر توصيل المحادثة.',sound:'تشغيل الصوت',end:'إنهاء المحادثة'},en:{connecting:'Connecting your conversation…',live:'You’re connected. Speak naturally.',ended:'The conversation has ended.',failed:'The conversation could not connect.',sound:'Enable sound',end:'End conversation'}};
  const notify=(type)=>parent.postMessage({type,nonce},location.origin);
  function stopTracks(){for(const track of localTracks)track.stop();localTracks.clear();for(const audio of audios.values()){audio.pause();audio.srcObject=null;audio.remove();}audios.clear();}
  async function stop(){if(stopping)return;stopping=true;stopTracks();try{await call?.leave();}catch{}try{await call?.destroy();}catch{}call=null;status.textContent=text[lang].ended;notify('raneen-call-ended');}
  end.onclick=stop;
  sound.onclick=async()=>{let blocked=false;for(const audio of audios.values())try{await audio.play();}catch{blocked=true;}sound.hidden=!blocked;};
  window.addEventListener('message',async(event)=>{
    if(event.source!==parent||event.origin!==location.origin)return;
    const data=event.data||{};
    if(data.type==='raneen-call-stop'&&nonce&&data.nonce===nonce){await stop();return;}
    if(data.type!=='raneen-call-start'||starting||nonce||typeof data.nonce!=='string')return;
    let url;try{url=new URL(data.url);}catch{return;}
    if(url.protocol!=='https:'||!url.hostname.endsWith('.daily.co')||url.username||url.password)return;
    nonce=data.nonce;starting=true;lang=data.lang==='en'?'en':'ar';document.documentElement.lang=lang;document.documentElement.dir=lang==='ar'?'rtl':'ltr';status.textContent=text[lang].connecting;sound.textContent=text[lang].sound;end.textContent=text[lang].end;
    try{
      if(!window.DailyIframe)throw Error('sdk_unavailable');
      call=window.DailyIframe.createCallObject({audioSource:true,videoSource:false,avoidEval:true});
      call.on('joined-meeting',()=>{if(stopping)return;status.textContent=text[lang].live;notify('raneen-call-connected');});
      call.on('left-meeting',()=>{stopTracks();if(!stopping)stop();});
      call.on('error',()=>{status.textContent=text[lang].failed;stopTracks();notify('raneen-call-error');});
      call.on('track-started',(event)=>{
        const track=event.track;if(!track||track.kind!=='audio')return;
        if(event.participant?.local){localTracks.add(track);if(stopping)track.stop();return;}
        if(stopping)return;
        const audio=document.createElement('audio');audio.autoplay=true;audio.srcObject=new MediaStream([track]);audios.set(track.id,audio);document.body.appendChild(audio);audio.play().catch(()=>{sound.hidden=false;});
      });
      call.on('track-stopped',(event)=>{const audio=audios.get(event.track?.id);if(audio){audio.pause();audio.srcObject=null;audio.remove();audios.delete(event.track.id);}});
      await call.join({url:url.href,...(typeof data.token==='string'&&data.token?{token:data.token}:{})});
      if(stopping)await stop();
    }catch{stopTracks();status.textContent=text[lang].failed;notify('raneen-call-error');}
  });
  window.addEventListener('pagehide',()=>{stopTracks();call?.destroy().catch(()=>{});});
})();
